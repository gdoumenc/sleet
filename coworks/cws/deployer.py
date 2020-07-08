import os
import sys
from pathlib import Path

import click
from python_terraform import Terraform

from .command import CwsCommand


class CwsTerraform(Terraform):

    def __init__(self, working_dir, debug):
        super().__init__(working_dir=working_dir, terraform_bin_path='/usr/local/bin/terraform')
        self.debug = debug
        self.__initialized = False

    def apply_local(self, workspace):
        self.select_workspace(workspace)
        if not self.__initialized:
            self.init()
            self.__initialized = True
        self.apply()

    def destroy_local(self, workspace):
        self.select_workspace(workspace)
        if not self.__initialized:
            self.init()
            self.__initialized = True
        self.destroy()

    def select_workspace(self, workspace):
        return_code, out, err = self.workspace('select', workspace)
        self._print(out, err)
        if workspace != 'default' and return_code != 0:
            return_code, out, err = self.workspace('new', workspace)
            self._print(out, err)

    def init(self, **kwargs):
        return_code, out, err = super().init(input=False, raise_on_error=True)
        self._print(out, err)

    def apply(self, **kwargs):
        return_code, out, err = super().apply(skip_plan=True, input=False, raise_on_error=True)
        self._print(out, err)

    def destroy(self, **kwargs):
        return_code, out, err = super().destroy(input=False, raise_on_error=True)
        self._print(out, err)

    def _print(self, out, err):
        if self.debug:
            print(out, file=sys.stdout)
            print(err, file=sys.stderr)


class CwsDeployer(CwsCommand):
    def __init__(self, app=None, name='deploy'):
        super().__init__(app, name=name)

    @property
    def options(self):
        return (
            click.option('--dry', is_flag=True, help="Doesn't perform terraform commands."),
            click.option('--remote', '-r', is_flag=True, help="Deploy on fpr-coworks.io."),
            click.option('--debug/--no-debug', default=False, help="Print debug logs to stderr."),
        )

    def _execute(self, options):
        if options['remote']:
            self._remote_deploy(options)
        else:
            self._local_deploy(options)

    def _remote_deploy(self, options):
        pass

    def _local_deploy(self, options):
        """ Deploiement in 4 steps:
        create
            Step 1. Create API (destroys API integrations made in previous deployment)
            Step 2. Create Lambda (destroys API deployment made in previous deployment)
        update
            Step 3. Update API integrations
            Step 4. Update API deployment
        """

        print("Creating lambda and api resources ...")
        self._terraform_export_and_apply_local('create', options)
        print("Updating api integrations and deploying api ...")
        self._terraform_export_and_apply_local('update', options)
        print("Microservice deployed.")

    def _terraform_export_and_apply_local(self, step, options):
        output_file = os.path.join(".", "terraform", f"_{options.module}-{options.service}.tf")
        self.app.execute('terraform-staging', output=output_file, step=step, **options.to_dict())

        if not options['dry']:
            terraform = CwsTerraform(Path('.') / 'terraform', options['debug'])
            terraform.apply_local("default")
            terraform.apply_local(options.workspace)


class CwsDestroyer(CwsCommand):

    def __init__(self, app=None, name='destroy'):
        super().__init__(app, name=name)

    @property
    def options(self):
        return (
            click.option('--dry', is_flag=True, help="Doesn't perform terraform commands."),
            click.option('--remote', '-r', is_flag=True, help="Deploy on fpr-coworks.io."),
            click.option('--debug/--no-debug', default=False, help="Print debug logs to stderr."),
        )

    def _execute(self, options):
        if options['remote']:
            self._remote_destroy(options)
        else:
            self._local_destroy(options)

    def _remote_destroy(self, options):
        pass

    def _local_destroy(self, options):
        output_path = str(Path('.') / 'terraform' / f"_{options.module}-{options.service}.tf")
        self.app.execute('terraform-staging', output=output_path, step='create', **options.to_dict())
        terraform = CwsTerraform(Path('.') / 'terraform', options['debug'])
        print("Destroying api deployment ...")
        terraform.apply_local(options.workspace)
        print("Destroying api integrations ...")
        terraform.apply_local('default')
        print("Destroying lambdas ...")
        terraform.destroy_local(options.workspace)
        print("Destroying api resource ...")
        terraform.destroy_local('default')
        print("Destroy completed")


