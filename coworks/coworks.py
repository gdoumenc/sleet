import base64
import io
import itertools
import logging
import os
import sys
import traceback
import typing as t
from functools import partial
from inspect import isfunction
from pathlib import Path

import boto3
from flask import Blueprint as FlaskBlueprint
from flask import Flask
from flask import current_app
from flask import json
from flask.blueprints import BlueprintSetupState
from flask.testing import FlaskClient
from werkzeug.datastructures import ImmutableDict
from werkzeug.datastructures import MultiDict
from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import Forbidden
from werkzeug.exceptions import InternalServerError
from werkzeug.exceptions import Unauthorized
from werkzeug.routing import Rule

from .globals import request
from .utils import BIZ_BUCKET_HEADER_KEY
from .utils import BIZ_KEY_HEADER_KEY
from .utils import DEFAULT_LOCAL_STAGE
from .utils import HTTP_METHODS
from .utils import add_coworks_routes
from .utils import get_app_stage
from .utils import load_dotenv
from .utils import trim_underscores
from .wrappers import CoworksMapAdapter
from .wrappers import CoworksRequest
from .wrappers import CoworksResponse
from .wrappers import TokenResponse


#
# Decorators
#


def entry(fun: t.Callable = None, binary_headers: t.Dict[str, str] = None,
          stage: t.Union[str, t.Iterable[str]] = None,
          no_auth: bool = False, no_cors: bool = True) -> t.Callable:
    """Decorator to create a microservice entry point from function name.

    :param fun: the entry function.
    :param binary_headers: force default content-type.
    :param stage: entry defined only for this stage(s).
    :param no_auth: set authorizer by default.
    :param no_cors: set CORS by default.
    """
    if fun is None:
        return partial(entry, binary_headers=binary_headers, stage=stage, no_auth=no_auth, no_cors=no_cors)

    def get_path(start):
        name_ = fun.__name__[start:]
        name_ = trim_underscores(name_)  # to allow several functions with d, Falseifferent args
        return name_.replace('_', '/')

    name = fun.__name__.upper()
    for method in HTTP_METHODS:
        if name == method:
            path = ''
            break
        if name.startswith(f'{method}_'):
            path = get_path(len(f'{method}_'))
            break
    else:
        method = 'POST'
        path = get_path(0)

    stage = [] if stage is None else stage

    fun.__CWS_METHOD = method
    fun.__CWS_PATH = path
    fun.__CWS_BINARY_HEADERS = binary_headers
    fun.__CWS_NO_AUTH = no_auth
    fun.__CWS_STAGES = stage if type(stage) == list else [stage]
    fun.__CWS_NO_CORS = no_cors

    return fun


#
# Classes
#

class CoworksClient(FlaskClient):
    """Creates environment and complete request.
    """

    def __init__(self, *args: t.Any, aws_event=None, aws_context=None, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)

        path_info = aws_event['path']
        headers = aws_event['headers']
        request_context = aws_event['requestContext']

        method = request_context['httpMethod']
        scheme = headers['x-forwarded-proto']
        host_name = headers['x-forwarded-host'] if 'x-forwarded-host' in headers else request_context['domainName']
        entry_path = request_context['entryPath']
        stage = request_context['stage']
        entry_path_parameters = aws_event['entryPathParameters']
        query_string = MultiDict(aws_event['multiValueQueryStringParameters'])

        is_encoded = aws_event.get('isBase64Encoded', False)
        body = aws_event['body']
        if body and is_encoded:
            body = base64.b64decode(body)

        self.aws_environ = {
            "wsgi.input": io.BytesIO(b""),
            "wsgi.url_scheme": scheme,
            "REQUEST_SCHEME": scheme,
            "REQUEST_METHOD": method,
            "SERVER_NAME": host_name,
            "PATH_INFO": entry_path,

            'aws_event': aws_event,
            'aws_context': aws_context,
            'aws_stage': stage,
            "aws_entry_path": entry_path,
            "aws_entry_path_parameters": entry_path_parameters,
            'aws_query_string': query_string,
            'aws_body': body,
        }

    def open(self, *args, **kwargs) -> CoworksResponse:
        req = CoworksRequest(self.aws_environ, populate_request=False, shallow=True)
        return t.cast(CoworksResponse, super().open(req))

    def _copy_environ(self, other) -> dict:
        return {**other}


class Blueprint(FlaskBlueprint):
    """ Represents a blueprint, list of routes that will be added to microservice when registered.

    See :ref:`Blueprint <blueprint>` for more information.
    """

    def __init__(self, name: str = None, **kwargs):
        """Initialize a blueprint.

        :param kwargs: Other Flask blueprint parameters.
        """
        import_name = self.__class__.__name__.lower()
        super().__init__(name or import_name, import_name, **kwargs)

    def init_app(self, app):
        """".. deprecated:: 0.8.0"""
        ...

    def init_cli(self, app):
        """.. deprecated:: 0.8.0"""
        ...

    @property
    def logger(self) -> logging.Logger:
        return current_app.logger

    def make_setup_state(self, app: "TechMicroService", options: t.Dict, *args) -> BlueprintSetupState:
        """Stores creation state for deferred initialization."""
        state = super().make_setup_state(app, options, *args)

        # Defer blueprint route initialization.
        if not options.get('hide_routes', False):
            func = partial(add_coworks_routes, state.app, state)
            app.deferred_init_routes_functions = itertools.chain(app.deferred_init_routes_functions, (func,))

        return state


class TechMicroService(Flask):
    """Simple tech microservice.

    See :ref:`tech` for more information.

    .. versionadded:: 0.8.0
       The `stage_prefixed` parameter was added.
    """

    # Maximume content length for writing response content in debug
    size_max_for_debug = 2000

    def __init__(self, name: str = None, stage_prefixed: bool = True, **kwargs) -> None:
        """ Initialize a technical microservice.
        :param name: Name used to identify the microservice.
        :param stage_prefixed: if accessed with stage or not.
        :param kwargs: Other Flask parameters.
        """
        stage = get_app_stage()
        load_dotenv(stage)

        self.default_config = ImmutableDict({
            **self.default_config,
            "FLASK_SKIP_DOTENV": True,
        })

        name = name or self.__class__.__name__.lower()
        super().__init__(import_name=name, static_folder=None, **kwargs)

        self.request_class = CoworksRequest
        self.response_class = CoworksResponse

        self.deferred_init_routes_functions: t.Iterable[t.Callable] = []
        self._cws_app_initialized = False
        self._cws_conf_updated = False
        self.__aws_url_map = None
        self.__stage_prefixed = stage_prefixed

        @self.before_request
        def before():
            self._check_token()

    def init_app(self):
        """Called to finalize the application initialization.
        Mainly to get external variables defined specifically for a workspace.

        .. deprecated:: 0.8.0
        """

    def init_cli(self):
        """Called only on cli command.
        Mainly externalized to allow specific import not needed on deployed implementation.

        .. deprecated:: 0.8.0
        """

    def app_context(self):
        """Override to initialize coworks microservice.
        """
        self._init_app(False)
        return super().app_context()

    def create_url_adapter(self, _request: t.Optional[CoworksRequest]):
        if _request and _request.aws_event:
            self.subdomain_matching = True
            return CoworksMapAdapter(_request.environ, self.url_map, self.aws_url_map, self.__stage_prefixed)
        return super().create_url_adapter(_request)

    def cws_client(self, aws_event, aws_context):
        """CoWorks client used by the lambda call.
        """
        return CoworksClient(self, CoworksResponse, use_cookies=True, aws_event=aws_event, aws_context=aws_context)

    @property
    def aws_url_map(self) -> t.Dict[str, t.List[Rule]]:
        if self.__aws_url_map is None:
            self.__aws_url_map = {}
            for rule in self.url_map.iter_rules():
                entry_path = rule.rule.replace('<', '{').replace('>', '}')
                if entry_path in self.__aws_url_map:
                    self.__aws_url_map[entry_path].append(rule)
                else:
                    self.__aws_url_map[entry_path] = [rule]
        return self.__aws_url_map

    @property
    def routes(self) -> t.List[str]:
        """Returns the list of routes defined in the microservice.
        """
        return [k for k in self.aws_url_map]

    def token_authorizer(self, token: str) -> t.Union[bool, str]:
        """Defined the authorization process.

        If the returned value is False, all routes for all stages are denied.
        If the returned value is True, all routes for all stages are accepted.
        If the returned value is a string, then it must be a stage name and all routes are accepted for this stage.

        By default, just using the token authentification process (defined from terraform template).
        """

        return token == os.getenv('TOKEN')

    def auto_find_instance_path(self):
        """Instance path may be redefined by an environment variable.
        """
        instance_relative_path = os.getenv('INSTANCE_RELATIVE_PATH', '')
        path = Path(self.root_path) / instance_relative_path
        return path.as_posix()

    def store_response(self, resp: t.Union[dict, bytes], request_headers):
        """Store microservice response in S3 for biz task sequence.

        May be redefined for another storage in asynchronous call.
        """

        bucket = key = ''
        try:
            bucket = request_headers.get(self.config['X-CWS-S3Bucket'].lower())
            key = request_headers.get(self.config['X-CWS-S3Key'].lower())
            if bucket and key:
                content = json.dumps(resp).encode() if type(resp) is dict else resp
                buffer = io.BytesIO(content)
                buffer.seek(0)
                aws_s3_session = boto3.session.Session()
                aws_s3_session.client('s3').upload_fileobj(buffer, bucket, key)
                self.logger.debug(f"Response stored in {bucket}/{key}")
            else:
                self.logger.error("Cannot store response as no bucket or key")
        except Exception as e:
            self.logger.error(f"Exception when storing response in {bucket}/{key} : {str(e)}")

    def _init_app(self, in_lambda):
        """Finalize the app initialization.
        """
        if not self._cws_app_initialized:
            self._update_config(in_lambda=in_lambda)
            add_coworks_routes(self)
            for fun in self.deferred_init_routes_functions:
                fun()

            if os.getenv("FLASK_RUN_FROM_CLI"):
                self.init_cli()

            self._cws_app_initialized = True

    def __call__(self, arg1, arg2) -> dict:
        """Main microservice entry point.
        """
        in_flask = isfunction(arg2)
        self._init_app(not in_flask)

        # Lambda event call or Flask call
        if in_flask:
            res = self._flask_handler(arg1, arg2)
        else:
            res = self._lambda_handler(arg1, arg2)

        return res

    def _lambda_handler(self, event: t.Dict[str, t.Any], context: t.Dict[str, t.Any]):
        """Lambda handler.
        """
        if event.get('type') == 'TOKEN':
            return self._token_handler(event, context)
        return self._api_handler(event, context)

    def _token_handler(self, aws_event: t.Dict[str, t.Any], aws_context: t.Dict[str, t.Any]) -> dict:
        """Authorization token handler.
        """
        self.logger.warning(f"Calling {self.name} for authorization : {aws_event}")

        try:
            res = self.token_authorizer(aws_event['authorizationToken'])
            self.logger.warning(f"Token authorizer return is : {res}")
            return TokenResponse(res, aws_event['methodArn']).json
        except Exception as e:
            self.logger.error(f"Error in token handler for {self.name} : {e}")
            self.logger.error(getattr(e, '__traceback__'))
            return TokenResponse(False, aws_event['methodArn']).json

    def _api_handler(
            self, aws_event: t.Dict[str, t.Any], aws_context: t.Dict[str, t.Any]
    ) -> t.Optional[t.Union[dict, str]]:
        """API handler.
        """
        self.logger.warning(f"Calling {self.name} by api : {aws_event}")

        # Transforms as simple client call and manage exception if needed
        try:
            with self.cws_client(aws_event, aws_context) as c:

                # Get Flask return as dict or binary content
                resp = self._convert_to_lambda_response(c.open())

                # Strores response in S3 if asynchronous call
                invocation_type = aws_event['headers'].get('invocationtype')
                if invocation_type == 'Event':
                    self.store_response(resp, aws_event['headers'])

                # Encodes binary content
                if type(resp) is not dict:
                    resp = base64.b64encode(resp).decode('ascii')
                    content_length = len(resp) if self.logger.getEffectiveLevel() == logging.DEBUG else "N/A"
                    self.logger.warning(f"API returns binary content [length: {content_length}]")
                    return resp

                # Adds trace
                self.logger.warning(f"API returns code {resp.get('statusCode')} and headers {resp.get('headers')}")
                self.logger.warning(f"API body: {str(resp.get('body'))[:self.size_max_for_debug]}")

                return resp

        except Exception as e:
            self.logger.error(f"Exception in api handler for {self.name} : {e}")
            parts = ["Traceback (most recent call last):\n"]
            parts.extend(traceback.format_stack(limit=15)[:-2])
            parts.extend(traceback.format_exception(*sys.exc_info())[1:])
            self.logger.error(f"Traceback: {parts}")
            headers = {'content_type': "application/json"}
            return self._aws_payload(str(e), InternalServerError.code, headers)

    def _flask_handler(self, environ: t.Dict[str, t.Any], start_response: t.Callable[[t.Any], None]):
        """Flask handler.
        """
        return self.wsgi_app(environ, start_response)

    def _update_config(self, *, in_lambda: bool):
        if not self._cws_conf_updated:
            workspace = get_app_stage()

            # Set predefined environment variables
            self.config['X-CWS-S3Bucket'] = BIZ_BUCKET_HEADER_KEY
            self.config['X-CWS-S3Key'] = BIZ_KEY_HEADER_KEY

            self._cws_conf_updated = True

    def _check_token(self):
        """Simulates the authorization process of lambda if not in lambda context."""
        if not request.in_lambda_context:

            # No token check on local
            if get_app_stage() == DEFAULT_LOCAL_STAGE:
                return

            # Get no_auth option for this entry
            no_auth = False
            if request.url_rule:
                view_function = self.view_functions.get(request.url_rule.endpoint, None)
                if view_function:
                    no_auth = getattr(view_function, '__CWS_NO_AUTH', False)

            # Checks token if authorization needed
            if not no_auth:
                token = request.headers.get('Authorization', self.config.get('DEFAULT_TOKEN'))
                if token is None:
                    raise Unauthorized(www_authenticate=WWWAuthenticate(auth_type="basic"))
                try:
                    valid = self.token_authorizer(token)
                    if not valid:
                        raise Forbidden()
                except Exception as e:
                    current_app.logger.exception(e)
                    raise Forbidden()

    def _convert_to_lambda_response(self, resp: CoworksResponse) -> t.Union[dict, bytes]:
        """Convert Lambda response (dict for JSON or text result, bytes for binary content)."""

        # returns JSON structure
        if resp.is_json:
            try:
                return self._aws_payload(resp.json, resp.status_code, resp.headers)
            except (Exception,):
                resp.mimetype = "text/plain"

        # returns simple string JSON structure
        if resp.mimetype and resp.mimetype.startswith('text'):
            try:
                return self._aws_payload(resp.get_data(True), resp.status_code, resp.headers)
            except ValueError:
                pass

        # returns direct payload
        data = resp.get_data()
        if not isinstance(data, bytes):
            msg = f'Expected bytes type for body with binary Content-Type. Got {type(data)} type body instead.'
            raise ValueError(msg)
        return data

    def _aws_payload(self, body, status_code, headers):
        return {
            "statusCode": status_code,
            "headers": {k: v for k, v in headers.items()},
            "body": body,
            "isBase64Encoded": False,
        }
