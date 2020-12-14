from aws_xray_sdk.core import recorder

from coworks import TechMicroService
from coworks.config import Config
from coworks.cws.deployer import CwsTerraformDeployer
from coworks.cws.runner import CwsRunner, run_with_reloader
from coworks.middleware import XRayMiddleware


class SimpleMicroService(TechMicroService):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.value = 0

    def auth(self, auth_request):
        return auth_request.token == "token"

    def get(self):
        return f"Stored value {self.value}.\n"

    def post(self, value=None):
        if value is not None:
            self.value = value
        return "Value stored.\n"


CONFIG = Config(
    workspace="dev"
)

app = SimpleMicroService(name='test', configs=[CONFIG])
CwsRunner(app)
CwsTerraformDeployer(app, name='deploy')
XRayMiddleware(app, recorder)

if __name__ == '__main__':
    run_with_reloader(app, project_dir='.', module='quickstart3', workspace='dev')
