import cappa

from fujin.commands import BaseCommand
from fujin.secrets import resolve_secrets


@cappa.command(
    help="Display the contents of the envfile with resolved secrets (for debugging purposes)"
)
class Printenv(BaseCommand):
    def __call__(self):
        if self.config.secret_config:
            result = resolve_secrets(
                self.config.host.env_content, self.config.secret_config
            )
        else:
            result = self.config.host.env_content
        self.stdout.output(result)
