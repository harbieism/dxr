from functools import partial
from logging import StreamHandler
from os.path import isdir, isfile, join, basename
from sys import stderr
from time import time
from urllib import quote_plus

from flask import (Blueprint, Flask, send_from_directory, current_app,
                   send_file, request, redirect, jsonify, render_template,
                   url_for)
from pyelasticsearch import ElasticSearch
from werkzeug.exceptions import NotFound

from dxr.build import linked_pathname
from dxr.exceptions import BadTerm
from dxr.filters import FILE
from dxr.mime import icon
from dxr.query import Query, filter_menu_items
from dxr.utils import non_negative_int, search_url, TEMPLATE_DIR, decode_es_datetime


# Look in the 'dxr' package for static files, etc.:
dxr_blueprint = Blueprint('dxr_blueprint',
                          'dxr',
                          template_folder=TEMPLATE_DIR,
                          # static_folder seems to register a "static" route
                          # with the blueprint so the url_prefix (set later)
                          # takes effect for static files when found through
                          # url_for('static', ...).
                          static_folder='static')


def make_app(instance_path):
    """Return a DXR application which looks in the given folder for
    configuration.

    Also set up the static and template folder.

    """
    app = Flask('dxr', instance_path=instance_path)

    # Load the special config file generated by dxr-build:
    app.config.from_pyfile(join(app.instance_path, 'config.py'))

    app.register_blueprint(dxr_blueprint, url_prefix=app.config['WWW_ROOT'])

    # Log to Apache's error log in production:
    app.logger.addHandler(StreamHandler(stderr))

    # Make an ES connection pool shared among all threads:
    app.es = ElasticSearch(app.config['ES_HOSTS'])

    return app


@dxr_blueprint.route('/')
def index():
    config = current_app.config
    return redirect(url_for('.browse', tree=config['DEFAULT_TREE']))


@dxr_blueprint.route('/<tree>/search')
def search(tree):
    """Normalize params, and dispatch between JSON- and HTML-returning
    searches, based on Accept header.

    """
    # Normalize querystring params:
    config = current_app.config
    if tree not in config['TREES']:
        raise NotFound('No such tree as %s' % tree)
    req = request.values
    query_text = req.get('q', '')
    offset = non_negative_int(req.get('offset'), 0)
    limit = min(non_negative_int(req.get('limit'), 100), 1000)
    is_case_sensitive = req.get('case') == 'true'

    # Make a Query:
    query = Query(partial(current_app.es.search,
                          index=config['ES_ALIASES'][tree]),
                  query_text,
                  is_case_sensitive=is_case_sensitive)

    # Fire off one of the two search routines:
    searcher = _search_json if _request_wants_json() else _search_html
    return searcher(query, tree, query_text, is_case_sensitive, offset, limit, config)


def _search_json(query, tree, query_text, is_case_sensitive, offset, limit, config):
    """Do a normal search, and return the results as JSON."""
    try:
        # Convert to dicts for ease of manipulation in JS:
        results = [{'icon': icon,
                    'path': path,
                    'lines': [{'line_number': nb, 'line': l} for nb, l in lines]}
                   for icon, path, lines in query.results(offset, limit)]
    except BadTerm as exc:
        return jsonify({'error_html': exc.reason, 'error_level': 'warning'}), 400

    return jsonify({
        'wwwroot': config['WWW_ROOT'],
        'tree': tree,
        'results': results,
        'tree_tuples': _tree_tuples(config['TREES'], tree, query_text, is_case_sensitive)})


def _search_html(query, tree, query_text, is_case_sensitive, offset, limit, config):
    """Search a few different ways, and return the results as HTML.

    Try a "direct search" (for exact identifier matches, etc.). If that
    doesn't work, fall back to a normal search.

    """
    should_redirect = request.values.get('redirect') == 'true'

    # Try for a direct result:
    if should_redirect:  # always true in practice?
        result = query.direct_result()
        if result:
            path, line = result
            # TODO: Does this escape query_text properly?
            return redirect(
                '%s/%s/source/%s?from=%s%s#%i' %
                (config['WWW_ROOT'],
                 tree,
                 path,
                 query_text,
                 '&case=true' if is_case_sensitive else '',
                 line))

    # Try a normal search:
    template_vars = {
            'filters': filter_menu_items(),
            'generated_date': config['GENERATED_DATE'],
            'google_analytics_key': config['GOOGLE_ANALYTICS_KEY'],
            'is_case_sensitive': is_case_sensitive,
            'query': query_text,
            'search_url': url_for('.search',
                                  tree=tree,
                                  q=query_text,
                                  redirect='false'),
            'tree': tree,
            'tree_tuples': _tree_tuples(config['TREES'], tree, query_text, is_case_sensitive),
            'wwwroot': config['WWW_ROOT']}

    try:
        results = list(query.results(offset, limit))
    except BadTerm as exc:
        return render_template('error.html',
                               error_html=exc.reason,
                               **template_vars), 400

    return render_template('search.html', results=results, **template_vars)


def _tree_tuples(trees, tree, query_text, is_case_sensitive):
    return [(t,
             url_for('.search',
                     tree=t,
                     q=query_text,
                     **({'case': 'true'} if is_case_sensitive else {})),
             description)
            for t, description in trees.iteritems()]


@dxr_blueprint.route('/<tree>/source/')
@dxr_blueprint.route('/<tree>/source/<path:path>')
def browse(tree, path=''):
    """Show a directory listing or a single file from one of the trees."""
    tree_folder = _tree_folder(tree)
    try:
        return send_from_directory(tree_folder, _html_file_path(tree_folder, path))
    except NotFound as exc:  # It was a folder or a path not found on disk.
        config = current_app.config

        # It's a folder (or nonexistent), not a file. Serve it out of ES.
        # Eventually, we want everything to be in ES.
        files_and_folders = [x['_source'] for x in current_app.es.search(
            {
                'query': {
                    'filtered': {
                        'query': {
                            'match_all': {}
                        },
                        'filter': {
                            'term': {'folder': path}
                        }
                    }
                },
                'sort': [{'is_folder': 'desc'}, 'name']
            },
            index=config['ES_ALIASES'][tree],
            doc_type=FILE,
            size=10000)['hits']['hits']]

        if not files_and_folders:
            raise NotFound

        # Create path data for each of the breadcrumbs in the html to be
        # rendered.
        data_paths = path.split('/')
        appended_paths = []
        for paths in data_paths:
            if not appended_paths:
                appended_paths.append(paths)
            else:
                appended_paths.append(appended_paths[-1] + "/" + paths)

        return render_template(
            'folder.html',
            # Common template variables:
            wwwroot=config['WWW_ROOT'],
            tree=tree,
            tree_tuples=[
                (t_name,
                 url_for('.parallel', tree=t_name, path=path),
                 t_description)
                for t_name, t_description in config['TREES'].iteritems()],
            generated_date=config['GENERATED_DATE'],
            google_analytics_key=config['GOOGLE_ANALYTICS_KEY'],
            paths_and_names=linked_pathname(path, tree),
            filters=filter_menu_items(),
            # Autofocus only at the root of each tree:
            should_autofocus_query=path == '',

            # Folder template variables:
            name=basename(path) or tree,
            path=path,
            data_path=appended_paths,
            files_and_folders=[
                ('folder' if f['is_folder'] else icon(f['name']),
                 f['name'],
                 decode_es_datetime(f['modified']) if 'modified' in f else None,
                 f.get('size'),
                 url_for('.browse', tree=tree, path=f['path'][0]))
                for f in files_and_folders])


@dxr_blueprint.route('/<tree>/')
@dxr_blueprint.route('/<tree>')
def tree_root(tree):
    """Redirect requests for the tree root instead of giving 404s."""
    return redirect(tree + '/source/')


@dxr_blueprint.route('/<tree>/parallel/')
@dxr_blueprint.route('/<tree>/parallel/<path:path>')
def parallel(tree, path=''):
    """If a file or dir parallel to the given path exists in the given tree,
    redirect to it. Otherwise, redirect to the root of the given tree.

    We do this with the future in mind, in which pages may be rendered at
    request time. To make that fast, we wouldn't want to query every one of 50
    other trees, when drawing the Switch Tree menu, to see if a parallel file
    or folder exists. So we use this controller to put off the querying until
    the user actually choose another tree.

    """
    tree_folder = _tree_folder(tree)
    try:
        disk_path = _html_file_path(tree_folder, path)
    except NotFound:
        disk_path = None  # A folder was found.
    www_root = current_app.config['WWW_ROOT']
    if disk_path is None or isfile(join(tree_folder, disk_path)):
        return redirect('{root}/{tree}/source/{path}'.format(
            root=www_root,
            tree=tree,
            path=path))
    else:
        return redirect('{root}/{tree}/source/'.format(
            root=www_root,
            tree=tree))


def _tree_folder(tree):
    """Return the on-disk path to the root of the given tree's folder in the
    instance."""
    return join(current_app.instance_path, 'trees', tree)


def _html_file_path(tree_folder, url_path):
    """Return the on-disk path, relative to the tree folder, of the HTML file
    that should be served when a certain path is browsed to. If a path to a
    folder, raise NotFound.

    :arg tree_folder: The on-disk path to the tree's folder in the instance
    :arg url_path: The URL path browsed to, rooted just inside the tree

    If you provide a path to a non-existent file or folder, I will happily
    return a path which has no corresponding FS entity.

    """
    if isdir(join(tree_folder, url_path)):
        # It's a bare directory. We generate these listings at request time now.
        raise NotFound
    else:
        # It's a file. Add the .html extension:
        return url_path + '.html'


def _request_wants_json():
    """Return whether the current request prefers JSON.

    Why check if json has a higher quality than HTML and not just go with the
    best match? Because some browsers accept on */* and we don't want to
    deliver JSON to an ordinary browser.

    """
    # From http://flask.pocoo.org/snippets/45/
    best = request.accept_mimetypes.best_match(['application/json',
                                                'text/html'])
    return (best == 'application/json' and
            request.accept_mimetypes[best] >
                    request.accept_mimetypes['text/html'])
