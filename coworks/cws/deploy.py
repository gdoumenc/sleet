import inspect
import os
import subprocess
import sys
import typing as t
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from shutil import ExecError
from subprocess import CompletedProcess

import boto3
import click
import dotenv
from flask.cli import pass_script_info
from flask.cli import with_appcontext
from jinja2 import BaseLoader
from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import select_autoescape
from werkzeug.routing import Rule

from coworks.utils import get_app_stage
from coworks.utils import get_env_filenames
from .command import CwsCommand
from .utils import progressbar
from .utils import show_stage_banner
from .zip import zip_command

UID_SEP = '_'


class TerraformContext:

    def __init__(self, info):
        self.app = info.load_app()

        # Transform flask app import path into module import path
        if info.app_import_path and '/' in info.app_import_path:
            msg = f"Cannot deploy or destroy a project with handler not on project folder : {info.app_import_path}.\n"
            msg += f"Add option -p {'/'.join(info.app_import_path.split('/')[:-1])} to resolve this."""
            raise ModuleNotFoundError()
        self.app_import_path = info.app_import_path.replace(':', '.') if info.app_import_path else "app.app"


@dataclass
class TerraformResource:
    parent_uid: str
    path: str
    rules: t.List[Rule] = None

    @cached_property
    def uid(self) -> str:

        if self.is_root:
            return ''

        uid = self.path.replace('{', '').replace('}', '')

        if self.parent_is_root:
            return uid

        parent_uid = self.parent_uid if len(self.parent_uid) < 80 else id(self.parent_uid)
        return f"{parent_uid}{UID_SEP}{uid}" if self.path else parent_uid

    @cached_property
    def is_root(self) -> bool:
        return self.path is None

    @cached_property
    def parent_is_root(self) -> bool:
        return self.parent_uid == ''

    # noinspection PyUnresolvedReferences
    @cached_property
    def no_cors_methods(self) -> t.Iterator[t.Optional[str]]:
        return (rule.methods for rule in self.rules if rule.cws_no_cors)

    def __repr__(self):
        return f"{self.uid}:{self.rules}"


class Terraform:
    """Terraform class to manage local terraform commands."""
    TIMEOUT = 600

    def __init__(self, app_context: TerraformContext, bar, terraform_dir, refresh, stage=None):
        self.app_context = app_context
        self.bar = bar
        self.terraform_dir = Path(terraform_dir)
        self.refresh = refresh
        self.stage = stage

    def init(self):
        self._execute(['init', '-input=false'])

    def apply(self) -> None:
        cmd = ['apply', '-auto-approve', '-parallelism=1']
        if not self.refresh:
            cmd.append('-refresh=false')
        self._execute(cmd)

    def output(self):
        values = self._execute(['output']).stdout
        return values.decode("utf-8").strip()

    @property
    def api_resources(self):
        """Returns the list of flatten path (prev_uid, last, rule)."""
        resources: t.Dict[str, TerraformResource] = {}

        def add_rule(previous: t.Optional[str], path: t.Optional[str], rule_: t.Optional[Rule]):
            """Add a method rule in a resource."""
            # todo : may use now aws_url_map
            path = None if path is None else path.replace('<', '{').replace('>', '}')
            resource = TerraformResource(previous, path)
            if rule_:
                view_function = self.app_context.app.view_functions.get(rule_.endpoint)
                rule_.cws_binary_headers = getattr(view_function, '__CWS_BINARY_HEADERS')
                rule_.cws_no_auth = getattr(view_function, '__CWS_NO_AUTH')
                rule_.cws_no_cors = getattr(view_function, '__CWS_NO_CORS')

            # Creates terraform ressources if it doesn't exist.
            uid = resource.uid
            if uid not in resources:
                resources[uid] = resource

            resource = resources[uid]
            if rule_:
                if resources[uid].rules is None:
                    resources[uid].rules = [rule_]
                else:
                    resources[uid].rules.append(rule_)
            return uid

        for rule in self.app_context.app.url_map.iter_rules():
            route = rule.rule
            previous_uid = ''
            if route.startswith('/'):
                route = route[1:]
            splited_route = route.split('/')

            # special root case
            if splited_route == ['']:
                add_rule(None, None, rule)
                continue

            # creates intermediate resources
            for prev in splited_route[:-1]:
                previous_uid = add_rule(previous_uid, prev, None)

            # set entry keys for last entry
            add_rule(previous_uid, splited_route[-1:][0], rule)

        return resources

    @property
    def logger(self):
        return self.app_context.app.logger

    @property
    def template_loader(self) -> BaseLoader:
        return PackageLoader(sys.modules[__name__].__package__)

    @property
    def jinja_env(self) -> Environment:
        return Environment(loader=self.template_loader, autoescape=select_autoescape(['html', 'xml']))

    def get_context_data(self, **options) -> dict:
        workspace = get_app_stage()

        # Microservice context data
        app = self.app_context.app
        data = {
            'api_resources': self.api_resources,
            'app': app,
            'app_import_path': self.app_context.app_import_path,
            'debug': app.debug,
            'description': inspect.getdoc(app) or "",
            'environment_variables': load_dotvalues(workspace),
            'ms_name': app.name,
            'now': datetime.now().isoformat(),
            'resource_name': app.name,
            'workspace': workspace,
            **options
        }

        if self.stage:
            data['stage'] = self.stage

        # AWS context data
        profile_name = options.get('profile_name')
        if profile_name:
            aws_session = boto3.Session(profile_name=profile_name)
            aws_region = aws_session.region_name
            data['aws_region'] = aws_region
            aws_account = aws_session.client("sts").get_caller_identity()["Account"]
            data['aws_account'] = aws_account

        return data

    def _execute(self, cmd_args: t.List[str]) -> CompletedProcess:
        self.logger.debug(f"Terraform arguments : {' '.join(cmd_args)}")
        p = subprocess.run(["terraform", *cmd_args], capture_output=True, cwd=self.terraform_dir,
                           timeout=self.TIMEOUT)
        if p.returncode != 0:
            msg = p.stderr.decode('utf-8')
            if not msg:
                msg = p.stdout.decode('utf-8')
            raise ExecError(msg)
        return p

    def generate_file(self, template_filename, output_filename, **options) -> None:
        """Generates stage terraform files."""
        template = self.jinja_env.get_template(template_filename)
        with (self.terraform_dir / output_filename).open("w") as f:
            data = self.get_context_data(**options)
            f.write(template.render(**data))


class TerraformBackend:
    """Terraform class to manage remote deployements."""

    def __init__(self, app_context, bar, terraform_dir, terraform_refresh, **options):
        """The remote terraform class correspond to the terraform command interface for workspaces.
        """
        self.app_context = app_context
        self.bar = bar

        # Creates terraform dir if needed
        self.working_dir = Path(terraform_dir)
        self.working_dir.mkdir(exist_ok=True)
        self.stage = get_app_stage()

        self.terraform_class = Terraform
        self.terraform_refresh = terraform_refresh
        self._api_terraform = self._stage_terraform = None

    def process_terraform(self, ctx, command_template, deploy=True, **options):
        root_command_params = ctx.find_root().params

        # Set default options calculated value
        app = self.app_context.app
        options['hash'] = True
        options['ignore'] = options.get('ignore') or ['.*', 'terraform']
        options['key'] = options.get('key') or f"{app.__module__}-{app.name}/archive.zip"
        dry = options.get('dry')

        # Transfert zip file to S3 (to be done on each service)
        options['deploy'] = deploy
        if deploy:
            self.bar.update(msg=f"Copy source files")
            app.logger.debug('Call zip command')
            zip_options = {zip_param.name: options[zip_param.name] for zip_param in zip_command.params}
            ctx.invoke(zip_command, **zip_options)
            click.secho("Source files on S3", fg="green")

        # Generates common terraform files
        app.logger.debug('Generate terraform common files')
        self.api_terraform.generate_file("terraform.j2", "terraform.tf", **root_command_params, **options)
        self.stage_terraform.generate_file("terraform.j2", "terraform.tf", **root_command_params, **options)

        # Generates terraform files and copy environment variable files in terraform working dir for provisionning
        output_filename = f"{app.name}.tech.tf"
        app.logger.debug(f'Generate terraform {output_filename} file')
        self.api_terraform.generate_file(command_template, output_filename, **root_command_params, **options)
        self.stage_terraform.generate_file(command_template, output_filename, **root_command_params, **options)

        # Apply to terraform if not dry
        if not dry:
            self.bar.update(msg="Create or update API")
            self.api_terraform.apply()
            if options.get('api'):
                return

            self.bar.update(msg=f"Deploy staged lambda ({self.stage})")
            self.stage_terraform.apply()

            # Traces output
            self.bar.terminate()
            click.echo()
            echo_output(self.api_terraform)
            click.secho("Microservice deployed", fg="green")
        else:
            self.bar.update(msg=f"Nothing deployed (dry mode)")

    @property
    def api_terraform(self):
        if self._api_terraform is None:
            self.app_context.app.logger.debug(f"Create common terraform instance using {self.terraform_class}")
            self._api_terraform = self.terraform_class(self.app_context, self.bar,
                                                       terraform_dir=self.working_dir,
                                                       refresh=self.terraform_refresh)
        return self._api_terraform

    @property
    def stage_terraform(self):
        if self._stage_terraform is None:
            self.app_context.app.logger.debug(f"Create {self.stage} terraform instance using {self.terraform_class}")
            terraform_dir = f"{self.working_dir}_{self.stage}"
            self._stage_terraform = self.terraform_class(self.app_context, self.bar,
                                                         terraform_dir=terraform_dir,
                                                         refresh=self.terraform_refresh,
                                                         stage=self.stage)
        return self._stage_terraform


@click.command("deploy", CwsCommand, short_help="Deploy the CoWorks microservice on AWS Lambda.")
# Zip options (redefined)
@click.option('--api', is_flag=True,
              help="Stop after API create step (forces also dry mode).")
@click.option('--bucket', '-b',
              help="Bucket to upload sources zip file to", required=True)
@click.option('--dry', is_flag=True,
              help="Doesn't perform deploy [Global option only].")
@click.option('--ignore', '-i', multiple=True,
              help="Ignore pattern when copying source to lambda.")
@click.option('--key', '-k',
              help="Sources zip file bucket's name.")
@click.option('--module-name', '-m', multiple=True,
              help="Python module added from current pyenv (module or file.py).")
@click.option('--profile-name', '-pn', required=True,
              help="AWS credential profile.")
# Deploy specific options
@click.option('--binary-types', multiple=True,
              help="Content types defined as binary contents (no encoding).")
@click.option('--json-types', multiple=True,
              help="Add mime types for JSON response [at least application/json, text/x-json, "
                   "application/javascript, application/x-javascript].")
@click.option('--layers', '-l', multiple=True, required=True,
              help="Add layer (full arn: aws:lambda:...). Must contains CoWorks at least.")
@click.option('--memory-size', default=128,
              help="Lambda memory size (default 128).")
@click.option('--python', '-p', type=click.Choice(['3.7', '3.8']), default='3.8',
              help="Python version for the lambda.")
@click.option('--security-groups', multiple=True, default=[],
              help="Security groups to be added [ids].")
@click.option('-s', '--stage', default='dev',
              help="Deploiement stage.")
@click.option('--subnets', multiple=True, default=[],
              help="Subnets to be added [ids].")
@click.option('--timeout', default=60,
              help="Lambda timeout (default 60s).Only for asynchronous call (API call 30s).")
@click.option('--terraform-cloud', '-tc', is_flag=True, default=False,
              help="Use cloud workspaces (default false).")
@click.option('--terraform-dir', '-td', default="terraform",
              help="Terraform files folder (default terraform).")
@click.option('--terraform-organization', '-to',
              help="Terraform organization needed if using cloud terraform.")
@click.option('--terraform-refresh', '-tr', is_flag=True, default=False,
              help="Forces terraform to refresh the state (default false).")
@click.option('--text-types', multiple=True,
              help="Add mime types for JSON response [at least text/plain, text/html].")
@click.pass_context
@pass_script_info
@with_appcontext
def deploy_command(info, ctx, stage, **options) -> None:
    """ Deploiement in 2 steps:
        Step 1. Create API and routes integrations
        Step 2. Deploy API and Lambda
    """

    if options.get('terraform_cloud') and not options.get('terraform_organization'):
        raise click.BadParameter('An organization must be defined if using cloud terraform')

    app_context = TerraformContext(info)
    app = app_context.app
    os.environ['CWS_STAGE'] = stage
    terraform = None

    app.logger.debug(f"Start deploy command: {options}")
    show_stage_banner(stage)
    cloud = options.get('terraform_cloud')
    refresh = options.get('terraform_refresh')
    click.secho(f" * Using terraform backend {'cloud' if cloud else 's3'} (refresh={refresh})", fg="green")
    with progressbar(label="Deploy microservice", threaded=not app.debug) as bar:
        terraform_backend_class = options.pop('terraform_class', TerraformBackend)
        app.logger.debug(f"Deploying {app} using {terraform_backend_class}")
        backend = terraform_backend_class(app_context, bar, **options)
        backend.process_terraform(ctx, 'deploy.j2', **options)


@click.command("destroy", CwsCommand, short_help="Destroy the CoWorks microservice on AWS Lambda.")
# Zip options (redefined)
@click.option('--bucket', '-b', required=True,
              help="Bucket to upload sources zip file to")
@click.option('--key', '-k',
              help="Sources zip file bucket's name.")
@click.option('--profile-name', '-pn', required=True,
              help="AWS credential profile.")
@click.option('-s', '--stage', default='dev',
              help="Deploiement stage.")
@click.option('--terraform-dir', default="terraform",
              help="Terraform folder (default terraform).")
@click.option('--terraform-cloud', is_flag=True,
              help="Use cloud workspaces (default false).")
@click.pass_context
@pass_script_info
@with_appcontext
def destroy_command(info, ctx, stage, **options) -> None:
    """ Destroy by setting counters to 0.
    """
    app_context = TerraformContext(info)
    app = app_context.app
    os.environ['CWS_STAGE'] = stage

    app.logger.debug('Start destroy command')
    terraform_class = pop_terraform_class(options)
    with progressbar(label='Destroy microservice', threaded=not app.debug) as bar:
        app.logger.debug(f'Destroying {app} using {terraform_class}')
        process_terraform(app_context, ctx, terraform_class, bar, 'destroy.j2', memory_size=128, timeout=60,
                          deploy=False, **options)
    click.echo(f"You can now delete the terraform_{get_app_stage()} folder.")


@click.command("deployed", CwsCommand, short_help="Retrieve the microservices deployed for this project.")
@click.option('-s', '--stage', default='dev',
              help="Deploiement stage.")
@click.option('--terraform-dir', default="terraform",
              help="Terraform folder (default terraform).")
@click.option('--terraform-cloud', is_flag=True,
              help="Use cloud workspaces (default false).")
@click.pass_context
@pass_script_info
@with_appcontext
def deployed_command(info, ctx, stage, **options) -> None:
    app_context = TerraformContext(info)
    app = app_context.app
    os.environ['CWS_STAGE'] = stage
    terraform = None

    app.logger.debug('Start deployed command')
    show_stage_banner(stage)
    terraform_class = pop_terraform_class(options)
    with progressbar(label='Retrieving information', threaded=not app.debug) as bar:
        app.logger.debug(f'Get deployed informations {app} using {terraform_class}')
        root_command_params = ctx.find_root().params
        terraform = terraform_class(app_context, bar, **root_command_params, **options)
        if terraform:
            echo_output(terraform)


def echo_output(terraform):
    """Pretty print terraform output.
    """
    rows = terraform.output()
    for row in rows.split('\n'):
        values = row.split('=')
        if len(values) > 1:
            cws_name = values[0].strip()[:-3]  # remove last _id
            api_id = values[1].strip()[1:-1]  # remove quotes
            api_url = f"https://{api_id}.execute-api.eu-west-1.amazonaws.com/"
            click.echo(f"The microservice {cws_name} is deployed at {api_url}")


def load_dotvalues(stage: str):
    environment_variables = {}
    for env_filename in get_env_filenames(stage):
        path = dotenv.find_dotenv(env_filename, usecwd=True)
        if path:
            environment_variables.update(dotenv.dotenv_values(path))
    return environment_variables
