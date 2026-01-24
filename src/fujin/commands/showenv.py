import cappa


from fujin.commands import BaseCommand


from fujin.secrets import resolve_secrets
from dataclasses import dataclass


@cappa.command(
    help="Display the contents of the envfile with resolved secrets (for debugging purposes)"
)
@dataclass
class Showenv(BaseCommand):
    def __call__(self):
        if self.config.secret_config:
            parsed_env = resolve_secrets(
                self.selected_host.env_content, self.config.secret_config
            )
        else:
            parsed_env = self.selected_host.env_content
        self.output.output(parsed_env)
