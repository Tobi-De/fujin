import cappa

from .deploy import Deploy
from .server import Server
from fujin.commands import BaseCommand


@cappa.command(
    help="Bootstrap server and deploy application (one-command setup)",
)
class Up(BaseCommand):
    def __call__(self):
        Server().bootstrap()
        Deploy()()
        self.output.success(
            "Server bootstrapped and application deployed successfully!"
        )
