import itertools
import logging
import sys
from dataclasses import dataclass
from itertools import chain, repeat
from pathlib import Path
from threading import Thread
from time import sleep
from typing import List

import boto3
import click

from coworks import TechMicroService
from coworks.config import CORSConfig
from .command import CwsCommand, CwsCommandError
from .writer import CwsTemplateWriter
from .zip import CwsZipArchiver

UID_SEP = '_'


@dataclass
class TerraformEntry:
    app: TechMicroService
    parent_uid: str
    path: str
    methods: List[str]
    cors: CORSConfig

    @property
    def uid(self):
        def remove_brackets(path):
            return f"{path.replace('{', '').replace('}', '')}"

        if self.path is None:
            return UID_SEP

        last = remove_brackets(self.path)
        return f"{self.parent_uid}{UID_SEP}{last}" if self.parent_uid else last

    @property
    def is_root(self):
        return self.path is None

    @property
    def parent_is_root(self):
        return self.parent_uid == UID_SEP

    def __repr__(self):
        return f"{self.uid}:{self.methods}"


class CwsTerraformDeployer(CwsCommand):
    """ Deploiement in 4 steps:
    create
        Step 1. Create API in default workspace (destroys API integrations made in previous deployment)
        Step 2. Create Lambda in stage workspace (destroys API deployment made in previous deployment)
    update
        Step 3. Update API routes integrations
        Step 4. Update API deployment
    """

    @classmethod
    def multi_execute(cls, project_dir, workspace, client_options, execution_params):
        create = client_options.get('create')

        # if one command is dry all are dry
        dry = client_options.get('dry') or client_options.get('stop') is not None
        for command, options in execution_params:
            dry = dry or options['dry']

        # Validate create option choice
        if create:
            prompts = chain(["Are you sure you want to (re)create the API [yN]?:"], repeat("Answer [yN]: "))
            replies = map(input, prompts)
            valid_response = next(filter(lambda x: x == 'y' or x == 'n' or x == '', replies))
            if valid_response != 'y':
                return

        # Transfert zip file to S3 (to be done on each service)
        key = None
        for command, options in execution_params:
            print(f"Uploading zip to S3")
            key = options.pop('key') or f"{cls.bucket_key(command, options)}/archive.zip"
            ignore = options.pop('ignore') or ['terraform', '.terraform']
            command.app.execute('zip', key=key, ignore=ignore, **options)

        # Generates terraform files (create step)
        terraform = Terraform()
        for command, options in execution_params:
            debug = client_options.get('debug') or options['debug']
            profile_name = client_options.get('profile_name') or options['profile_name']
            aws_region = boto3.Session(profile_name=profile_name).region_name

            # Generates terraform files and apply terraform if not dry
            if not dry or options.get('stop') == 'create':
                name = f"{options['module']}-{options['service']}"
                if debug:
                    print(f"Generate terraform files for creating API and lambdas for {name}")
                output = str(Path(terraform.working_dir) / f"{name}.tf")
                command.app.execute('export', template=["terraform.j2"], output=output, aws_region=aws_region,
                                    step="create", key=key, entries=_entries(command.app), **options)

        if not dry:
            msg = ["Create API", "Create lambda"] if create else ["Update API", "Update lambda"]
            cls._terraform_apply_local(terraform, workspace, msg)

        # Generates terraform files (update step)
        for command, options in execution_params:
            debug = client_options.get('debug') or options['debug']
            profile_name = client_options.get('profile_name') or options['profile_name']
            aws_region = boto3.Session(profile_name=profile_name).region_name

            if not dry or options.get('stop') == 'update':
                name = f"{options['module']}-{options['service']}"
                if debug:
                    print(f"Generate terraform files for updating API for {name}")
                output = str(Path(terraform.working_dir) / f"{name}.tf")
                command.app.execute('export', template=["terraform.j2"], output=output, aws_region=aws_region,
                                    step="update", key=key, entries=_entries(command.app), **options)

        if not dry:
            cls._terraform_apply_local(terraform, workspace, ["Update API routes", f"Deploy API {workspace}"])

        terraform = Terraform()
        out = terraform.output_local("default")
        print(f"terraform output : {out}")

    def __init__(self, app=None, name='deploy', template_folder='.'):
        self.zip_cmd = CwsZipArchiver(app)
        CwsTemplateWriter(app)
        super().__init__(app, name=name)

    @property
    def options(self):
        return [
            *super().options,
            *self.zip_cmd.options,
            click.option('--binary_media_types'),
            click.option('--create', '-c', is_flag=True, help="May create or recreate the API."),
            click.option('--layers', '-l', multiple=True),
            click.option('--memory_size', default=128),
            click.option('--stop', type=click.Choice(['create', 'update']), help="Stop the terraform generation"),
            click.option('--timeout', default=30),
        ]

    @classmethod
    def bucket_key(cls, command, options):
        return f"{options['module']}-{command.app.name}"

    @staticmethod
    def _terraform_apply_local(terraform, workspace, traces):
        stop = False

        def display_spinning_cursor():
            spinner = itertools.cycle('|/-\\')
            while not stop:
                sys.stdout.write(next(spinner))
                sys.stdout.write('\b')
                sys.stdout.flush()
                sleep(0.1)

        """
        In the default terraform workspace, we have the API.
        In the specific workspace, we have the correspondingg stagging lambda.
        """
        spin_thread = Thread(target=display_spinning_cursor)
        spin_thread.start()

        try:
            print(f"Terraform apply ({traces[0]})", flush=True)
            terraform.apply_local("default")
            print(f"Terraform apply ({traces[1]})", flush=True)
            terraform.apply_local(workspace)
        finally:
            stop = True

    def _execute(self, step=None, output=None, **options):
        raise CwsCommandError("Not implemented")


logging.getLogger("python_terraform").setLevel(logging.ERROR)


class Terraform:

    def __init__(self):
        from python_terraform import Terraform as PythonTerraform

        self.terraform = PythonTerraform(working_dir='terraform', terraform_bin_path='terraform')
        Path(self.working_dir).mkdir(exist_ok=True)

    @property
    def working_dir(self):
        return self.terraform.working_dir

    def apply_local(self, workspace):
        self._select_workspace(workspace)
        return_code, _, err = self.terraform.apply(skip_plan=True, input=False, raise_on_error=False, parallelism=1)
        if return_code != 0:
            raise CwsCommandError(err)

    def output_local(self, workspace):
        self._select_workspace(workspace)
        values = self.terraform.output(capture_output=True)
        return {key: value['value'] for key, value in values.items()}

    def _select_workspace(self, workspace):
        return_code, out, err = self.terraform.workspace('select', workspace)
        if workspace != 'default' and return_code != 0:
            _, out, err = self.terraform.workspace('new', workspace, raise_on_error=True)
        if not (Path(self.working_dir) / '.terraform').exists():
            self.terraform.init(input=False, raise_on_error=True)


def _entries(app):
    """Returns the list of flatten path (prev, last, keys)."""
    all_pathes_id = {}

    def add_entry(previous, last, meth):
        entry = TerraformEntry(app, previous, last, meth, app.config.cors)
        uid = entry.uid
        if uid not in all_pathes_id:
            all_pathes_id[uid] = entry
        if all_pathes_id[uid].methods is None:
            all_pathes_id[uid].methods = meth
        return uid

    for route, methods in app.routes.items():
        previous_uid = UID_SEP
        splited_route = route[1:].split('/')

        # special root case
        if splited_route == ['']:
            add_entry(None, None, methods.keys())
            continue

        # creates intermediate resources
        last_path = splited_route[-1:][0]
        for prev in splited_route[:-1]:
            previous_uid = add_entry(previous_uid, prev, None)

        # set entryes keys for last entry
        add_entry(previous_uid, last_path, methods.keys())

    return all_pathes_id
