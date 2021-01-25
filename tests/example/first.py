from aws_xray_sdk.core import xray_recorder

from coworks import TechMicroService, entry
from coworks.config import Config
from coworks.context_manager import XRayContextManager
from coworks.cws.deployer import CwsTerraformDeployer
from coworks.cws.runner import CwsRunner


class SimpleMicroService(TechMicroService):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.value = 0

    def auth(self, auth_request):
        return auth_request.token == "token"

    @entry
    def get(self):
        return f"Stored value {self.value}.\n"

    @entry
    def post(self, value=None):
        if value is not None:
            self.value = value
        return "Value stored.\n"


CONFIG = Config(
    workspace="dev"
)

app = SimpleMicroService(configs=[CONFIG])
CwsRunner(app)
CwsTerraformDeployer(app, name='deploy')
XRayContextManager(app, xray_recorder)

if __name__ == '__main__':
    from coworks.blueprint import Admin

    app.register_blueprint(Admin(), url_prefix='/admin')
    app.execute('run', project_dir='.', module='quickstart3', workspace='dev')
