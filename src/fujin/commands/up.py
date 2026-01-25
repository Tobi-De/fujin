from dataclasses import dataclass

import cappa

from fujin.commands import BaseCommand

from .deploy import Deploy
from .server import Server


@cappa.command(
    help="Bootstrap server and deploy application (one-command setup)",
)
@dataclass
class Up(BaseCommand):
    def __call__(self):
        Server(host=self.host).bootstrap()
        Deploy(host=self.host)()
        self.output.success(
            "Server bootstrapped and application deployed successfully!"
        )
