from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa
import tomli_w

from fujin.commands import BaseCommand
from fujin.config import InstallationMode, tomllib
from fujin.templates import CADDYFILE_TEMPLATE, NEW_SERVICE_TEMPLATE


@cappa.command(help="Initialize a new fujin.toml configuration file")
@dataclass
class Init(BaseCommand):
    """
    Examples:
      fujin init                        Create config with simple profile
      fujin init --profile django       Create config for Django project
    """

    profile: Annotated[
        str,
        cappa.Arg(
            choices=["simple", "binary", "django"],
            short="-p",
            long="--profile",
            help="Configuration profile to use",
        ),
    ] = "simple"

    def __call__(self):
        fujin_toml = Path("fujin.toml")
        fujin_dir = Path(".fujin")

        if fujin_toml.exists():
            self.output.warning("fujin.toml file already exists, skipping generation")
            return

        if fujin_dir.exists():
            self.output.warning(".fujin/ directory already exists, skipping generation")
            return

        app_name = Path().resolve().stem.replace("-", "_").replace(" ", "_").lower()

        # Generate minimal fujin.toml
        config = self._generate_toml(app_name)
        if self.profile == "django":
            # Migrations and static collection run once per release against
            # the new release's own virtualenv, not on every service restart.
            resolved_app = config["app"]
            config["hooks"] = {
                "post_install": [
                    f"{{app_dir}}/venv/bin/{resolved_app} migrate --no-input",
                    f"{{app_dir}}/venv/bin/{resolved_app} collectstatic --no-input",
                ]
            }
        fujin_toml.write_text(tomli_w.dumps(config, multiline_strings=True))
        self.output.success("Generated fujin.toml")

        # Generate .fujin/ directory structure
        profile_generators = {
            "simple": self._generate_simple,
            "django": self._generate_django,
            "binary": self._generate_binary,
        }

        profile_generators[self.profile](app_name, fujin_dir)

        self.output.success(f"Generated .fujin/ directory with {self.profile} profile")
        self.output.info(
            "\nNext steps:\n"
            "  1. Review and customize files in .fujin/systemd/\n"
            "  2. Update fujin.toml with your host details\n"
            "  3. Create .env.prod with your environment variables\n"
            "  4. Deploy: fujin deploy"
        )

    def _generate_toml(self, app_name: str) -> dict:
        """Generate minimal fujin.toml without processes/sites."""
        config = {
            "app": app_name,
            "version": "0.0.1",
            "build_command": "uv build && uv pip compile pyproject.toml -o requirements.txt > /dev/null",
            "distfile": f"dist/{app_name}-{{version}}-py3-none-any.whl",
            "requirements": "requirements.txt",
            "installation_mode": InstallationMode.PY_PACKAGE,
            "hosts": [
                {
                    "user": "deploy",
                    "address": f"{app_name}.com",
                    "envfile": ".env.prod",
                }
            ],
        }

        # Check for .python-version or pyproject.toml
        if not Path(".python-version").exists():
            config["python_version"] = "3.12"

        pyproject_toml = Path("pyproject.toml")
        if pyproject_toml.exists():
            pyproject = tomllib.loads(pyproject_toml.read_text())
            config["app"] = pyproject.get("project", {}).get("name", app_name)
            if pyproject.get("project", {}).get("version"):
                # fujin will read the version itself from the pyproject
                config.pop("version")

        return config

    def _create_caddyfile(
        self,
        fujin_dir: Path,
        app_name: str,
        upstream: str,
        static_path: str | None = None,
    ):
        """Create Caddyfile."""
        if static_path:
            # Django-style with static files
            content = f"""# Caddyfile for {app_name}
# Learn more: https://caddyserver.com/docs/caddyfile

{app_name}.com {{
    handle_path /static/* {{
        root * {static_path}
        file_server
    }}

    handle {{
        reverse_proxy {upstream}
    }}
}}
"""
        else:
            content = CADDYFILE_TEMPLATE.format(
                app_name=app_name, domain=f"{app_name}.com", upstream=upstream
            )

        caddyfile = fujin_dir / "Caddyfile"
        caddyfile.write_text(content)
        self.output.success(f"  Created {caddyfile}")

    def _generate_simple(self, app_name: str, fujin_dir: Path):
        """Generate simple profile: web service."""
        systemd_dir = fujin_dir / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)

        # Web service
        web_service = systemd_dir / "web.service"
        # Keep the framework-agnostic placeholder from the template; the unit
        # tells the user to adjust the main command.
        service_content = NEW_SERVICE_TEMPLATE.format(name="web")
        web_service.write_text(service_content)
        self.output.success(f"  Created {web_service}")

        self._create_caddyfile(fujin_dir, app_name, "localhost:8000")

    def _generate_django(self, app_name: str, fujin_dir: Path):
        """Generate Django profile: web service served by gunicorn.

        Migrations and collectstatic run as post-install hooks (once per
        release) rather than on every service restart.
        """
        systemd_dir = fujin_dir / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)

        web_service = systemd_dir / "web.service"
        service_content = NEW_SERVICE_TEMPLATE.format(name="web")

        service_content = service_content.replace(
            "WorkingDirectory={app_dir}",
            "WorkingDirectory={app_dir}/current",
        )
        service_content = service_content.replace(
            "ExecStart={app_dir}/current/venv/bin/python -m myapp.web",
            "# Adjust the WSGI module to match your Django project\n"
            "ExecStart={app_dir}/current/venv/bin/gunicorn "
            "myapp.wsgi:application --bind localhost:8000",
        )
        web_service.write_text(service_content)
        self.output.success(f"  Created {web_service}")

        self._create_caddyfile(
            fujin_dir,
            app_name,
            "localhost:8000",
            static_path="{app_dir}/current/staticfiles/",
        )

    def _generate_binary(self, app_name: str, fujin_dir: Path):
        """Generate binary profile: single binary deployment."""
        systemd_dir = fujin_dir / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)

        # Web service for binary
        web_service = systemd_dir / "web.service"
        service_content = NEW_SERVICE_TEMPLATE.format(name="web")

        # Add ExecStartPre for migrations
        service_content = service_content.replace(
            "# Main command - adjust to match your application\n",
            "# Run migrations before starting\n"
            f"ExecStartPre={{app_dir}}/current/bin/{app_name} migrate\n\n",
        )

        # Customize ExecStart for binary (no venv)
        service_content = service_content.replace(
            "ExecStart={app_dir}/current/venv/bin/python -m myapp.web",
            f"ExecStart={{app_dir}}/current/bin/{app_name} prodserver",
        )
        web_service.write_text(service_content)
        self.output.success(f"  Created {web_service}")

        self._create_caddyfile(fujin_dir, app_name, "localhost:8000")
