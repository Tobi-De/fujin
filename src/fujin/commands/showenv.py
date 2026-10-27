from dataclasses import dataclass

import cappa

from fujin.commands import BaseCommand
from fujin.secrets import render_template, resolve_secrets


@cappa.command(
    help="Display the contents of the envfile with resolved secrets (for debugging purposes)"
)
@dataclass
class Showenv(BaseCommand):
    def __call__(self):
        host = self.selected_host
        secret_config = self.config.effective_secrets(host)
        env_template = self.config.effective_env(host)
        if secret_config:
            parsed_env = resolve_secrets(env_template, secret_config)
        else:
            parsed_env = render_template(env_template)
        self.output.output(parsed_env)
