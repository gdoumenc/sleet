import traceback

import importlib
import inspect
import os
import platform
import sys
from flask import make_response as make_flask_response
from flask.blueprints import BlueprintSetupState
from functools import partial
from functools import update_wrapper

from .globals import request

HTTP_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS']


def add_coworks_routes(app, bp_state: BlueprintSetupState = None) -> None:
    """ Creates all routes for a microservice.
    :param app the app microservice
    :param bp_state the blueprint state
    """

    # Adds entrypoints
    scaffold = bp_state.blueprint if bp_state else app
    method_members = inspect.getmembers(scaffold.__class__, lambda x: inspect.isfunction(x))
    methods = [fun for _, fun in method_members if hasattr(fun, '__CWS_METHOD')]
    for fun in methods:
        if getattr(fun, '__CWS_HIDDEN', False):
            continue

        method = getattr(fun, '__CWS_METHOD')
        entry_path = path_join(getattr(fun, '__CWS_PATH'))

        # Get parameters
        args = inspect.getfullargspec(fun).args[1:]
        defaults = inspect.getfullargspec(fun).defaults
        varkw = inspect.getfullargspec(fun).varkw
        if defaults:
            len_defaults = len(defaults)
            for index, arg in enumerate(args[:-len_defaults]):
                entry_path = path_join(entry_path, f"/<{arg}>")
            kwarg_keys = args[-len_defaults:]
        else:
            for index, arg in enumerate(args):
                entry_path = path_join(entry_path, f"/<{arg}>")
            kwarg_keys = {}

        proxy = _create_rest_proxy(scaffold, fun, kwarg_keys, args, varkw)

        # Creates the entry
        url_prefix = bp_state.url_prefix if bp_state else ''
        rule = make_absolute(entry_path, url_prefix)

        name_prefix = f"{bp_state.name_prefix}_" if bp_state else ''
        endpoint = f"{name_prefix}{proxy.__name__}"

        app.add_url_rule(rule=rule, view_func=proxy, methods=[method], endpoint=endpoint)


def _create_rest_proxy(scaffold, func, kwarg_keys, args, varkw):
    def proxy(**kwargs):
        try:
            # Adds kwargs parameters
            def check_param_expected_in_lambda(param_name):
                """Alerts when more parameters than expected are defined in request."""
                if param_name not in kwarg_keys and varkw is None:
                    _err_msg = f"TypeError: got an unexpected keyword argument '{param_name}'"
                    return _err_msg, 400

            def as_fun_params(values: dict, flat=True):
                """Set parameters as simple value or list of values if multiple defined.
               :param values: Dict of values.
               :param flat: If set to True the list values of lenth 1 is retrun as single value.
                """
                params = {}
                for k, v in values.items():
                    check_param_expected_in_lambda(k)
                    params[k] = v[0] if flat and len(v) == 1 else v
                return params

            # get keyword arguments from request
            if kwarg_keys or varkw:

                # adds parameters from query parameters
                if request.method == 'GET':
                    data = request.values.to_dict(False)
                    kwargs = dict(**kwargs, **as_fun_params(data))

                # adds parameters from body parameter
                elif request.method in ['POST', 'PUT']:
                    try:
                        if request.is_json:
                            data = request.json
                            if type(data) is dict:
                                kwargs = dict(**kwargs, **as_fun_params(data, False))
                            else:
                                kwargs[kwarg_keys[0]] = data
                        elif request.is_multipart:
                            data = request.form.to_dict(False)
                            files = request.files.to_dict(False)
                            kwargs = dict(**kwargs, **as_fun_params(data), **as_fun_params(files))
                        elif request.is_form_urlencoded:
                            data = request.form.to_dict(False)
                            kwargs = dict(**kwargs, **as_fun_params(data))
                        else:
                            data = request.values.to_dict(False)
                            kwargs = dict(**kwargs, **as_fun_params(data))
                    except Exception as e:
                        scaffold.logger.error(traceback.print_exc())
                        scaffold.logger.debug(e)
                        return str(e), 400

                else:
                    err_msg = f"Keyword arguments are not permitted for {request.method} method."
                    return err_msg, 400

            else:
                if not args:
                    if request.content_length is not None:
                        err_msg = f"TypeError: got an unexpected arguments (body: {request.json})"
                        return err_msg, 400
                    if request.query_string:
                        err_msg = f"TypeError: got an unexpected arguments (query: {request.query_string})"
                        return err_msg, 400

            resp = func(scaffold, **kwargs)
            return make_response(resp)
        except TypeError as e:
            return str(e), 400
        except Exception as e:
            scaffold.logger.error(f"Exception: {str(e)}")
            scaffold.logger.error(traceback.print_exc())
            return str(e), 500

    return update_wrapper(proxy, func)


def import_attr(module, attr: str, cwd='.'):
    if type(attr) is not str:
        raise AttributeError(f"{attr} is not a string.")
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    app_module = importlib.import_module(module)
    if "PYTEST_CURRENT_TEST" in os.environ:
        # needed as Chalice local server change class
        app_module = importlib.reload(app_module)
    return getattr(app_module, attr)


def class_auth_methods(obj):
    """Returns the auth method from the class if exists."""
    methods = inspect.getmembers(obj.__class__, lambda x: inspect.isfunction(x))

    for name, func in methods:
        if name == 'auth':
            function_is_static = isinstance(inspect.getattr_static(obj.__class__, func.__name__), staticmethod)
            if function_is_static:
                return func
            return partial(func, obj)
    return None


def class_attribute(obj, name: str = None, defaut=None):
    """Returns the list of attributes from the class or the attribute if name parameter is defined
    or default value if not found."""
    attributes = inspect.getmembers(obj.__class__, lambda x: not inspect.isroutine(x))

    if not name:
        return attributes

    filtered = [a[1] for a in attributes if a[0] == name]
    return filtered[0] if filtered else defaut


def path_join(*args):
    """ Joins given arguments into an entry route.
    Slashes are stripped for each argument.
    """

    reduced = [x.lstrip('/').rstrip('/') for x in args if x]
    return '/'.join([x for x in reduced if x])


def make_absolute(route, url_prefix):
    if not route.startswith('/'):
        route = '/' + route
    if url_prefix:
        route = '/' + url_prefix.lstrip('/').rstrip('/') + route
    return route


def trim_underscores(name):
    while name.startswith('_'):
        name = name[1:]
    while name.endswith('_'):
        name = name[:-1]
    return name


def as_list(var):
    if var is None:
        return []
    if type(var) is list:
        return var
    return [var]


def make_response(resp):
    headers = {}
    if type(resp) is tuple:
        if len(resp) == 2 and type(resp[1]) is dict:
            headers = resp[1]
        elif len(resp) == 3:
            headers = resp[2]

    resp = make_flask_response(resp)

    accept = request.accept_mimetypes
    if 'Content-Type' not in headers:
        if not accept.provided or accept.accept_json:
            resp.headers['Content-Type'] = 'application/json'
        else:
            resp.headers['Content-Type'] = 'text/plain'
    return resp


def get_system_info():
    from flask import __version__ as flask_version

    flask_info = f"flask {flask_version}"
    python_info = f"python {sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
    platform_system = platform.system().lower()
    platform_release = platform.release()
    platform_info = f"{platform_system} {platform_release}"
    return f"{flask_info}, {python_info}, {platform_info}"


class FileParam:

    def __init__(self, file, mime_type):
        self.file = file
        self.mime_type = mime_type

    def __repr__(self):
        if self.mime_type:
            return f'FileParam({self.file.name}, {self.mime_type})'
        return f'FileParam({self.file.name})'
