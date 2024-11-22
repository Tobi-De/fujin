import cappa

from fujin.commands import BaseCommand
from fujin.secrets import resolve_secrets


@cappa.command(
    help="Print the content of the envfile with extracted secrets (for debugging)"
)
class Printenv(BaseCommand):
    def __call__(self):
        if self.config.secret_config:
            result = resolve_secrets(
                self.config.host.envfile, self.config.secret_config
            )
        else:
            result = self.config.host.envfile.read_text()
        self.stdout.output(result)
