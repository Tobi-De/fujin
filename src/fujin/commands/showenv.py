from dataclasses import dataclass

import cappa

from fujin.commands import BaseCommand
from fujin.errors import FujinError
from fujin.exit_codes import EXIT_USER_ABORTED
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
        try:
            if secret_config:
                parsed_env = resolve_secrets(env_template, secret_config)
            else:
                parsed_env = render_template(env_template)
        except FujinError as exc:
            # A missing/unresolvable secret is a local configuration error:
            # nothing was deployed, so this is a user abort (1), never the
            # deploy-failure code (4).
            message = getattr(exc, "message", None) or str(exc)
            raise cappa.Exit(message, code=EXIT_USER_ABORTED) from None
        self.output.output(parsed_env)
