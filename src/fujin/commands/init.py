from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa
import tomli_w

from fujin.commands import BaseCommand
from fujin.config import InstallationMode
from fujin.config import tomllib
from fujin.templates import BASE_DROPIN_TEMPLATE
from fujin.templates import CADDY_HANDLE_PROXY
from fujin.templates import CADDY_HANDLE_STATIC
from fujin.templates import CADDYFILE_HEADER
from fujin.templates import SERVICE_TEMPLATE
from fujin.templates import SOCKET_TEMPLATE


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
            choices=["simple", "falco", "binary", "django"],
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
        fujin_toml.write_text(tomli_w.dumps(config, multiline_strings=True))
        self.output.success("Generated fujin.toml")

        # Generate .fujin/ directory structure
        profile_generators = {
            "simple": self._generate_simple,
            "django": self._generate_django,
            "falco": self._generate_falco,
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
            "aliases": {
                "shell": "exec --appenv bash",
                "status": "app info",
                "logs": "app logs",
                "restart": "app restart",
            },
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

    def _create_common_dropins(self, systemd_dir: Path, app_name: str):
        """Create common.d/ directory with base drop-in files."""
        common_dir = systemd_dir / "common.d"
        common_dir.mkdir(parents=True, exist_ok=True)

        # Base configuration drop-in
        base_conf = common_dir / "base.conf"
        base_conf.write_text(
            BASE_DROPIN_TEMPLATE.format(app_name=app_name, app_dir="{app_dir}")
        )
        self.output.success(f"  Created {base_conf}")

    def _create_caddyfile(self, fujin_dir: Path, app_name: str, routes: dict):
        """Create Caddyfile with given routes."""
        caddyfile_content = CADDYFILE_HEADER.format(
            app_name=app_name, domain=f"{app_name}.com"
        )

        for route, target in routes.items():
            if isinstance(target, dict) and "static" in target:
                # Static file serving
                static_path = target["static"]
                caddyfile_content += CADDY_HANDLE_STATIC.format(
                    path=route, root=static_path
                )
            else:
                # Reverse proxy to service
                if target == "web":
                    upstream = f"unix//run/{{app_name}}/web.sock"
                else:
                    upstream = "localhost:8000"

                caddyfile_content += CADDY_HANDLE_PROXY.format(
                    name=target if isinstance(target, str) else "web",
                    path=route,
                    upstream=upstream,
                    extra_directives="",
                )

        caddyfile_content += "}\n"

        caddyfile = fujin_dir / "Caddyfile"
        caddyfile.write_text(caddyfile_content)
        self.output.success(f"  Created {caddyfile}")

    def _generate_simple(self, app_name: str, fujin_dir: Path):
        """Generate simple profile: web service with socket activation."""
        systemd_dir = fujin_dir / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)

        # Web service with socket activation
        web_service = systemd_dir / "web.service"
        web_service.write_text(
            SERVICE_TEMPLATE.format(
                description=f"Web service for {app_name}",
                app_name="{app_name}",
                description_suffix="web server",
                service_type="notify",
                user="{user}",
                exec_start_pre="",
                exec_start="{app_dir}/.venv/bin/gunicorn "
                + app_name
                + ".wsgi:application --bind unix:/run/{app_name}/web.sock",
            )
        )
        self.output.success(f"  Created {web_service}")

        # Socket file
        web_socket = systemd_dir / "web.socket"
        web_socket.write_text(
            SOCKET_TEMPLATE.format(
                name="web",
                app_name="{app_name}",
                instance_suffix="",
                template_suffix="",
                listen_stream="/run/{app_name}/web.sock",
                user="{user}",
            )
        )
        self.output.success(f"  Created {web_socket}")

        # Common drop-ins
        self._create_common_dropins(systemd_dir, app_name)

        # Caddyfile
        self._create_caddyfile(fujin_dir, app_name, {"/*": "web"})

    def _generate_django(self, app_name: str, fujin_dir: Path):
        """Generate Django profile: web service with migrations."""
        systemd_dir = fujin_dir / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)

        exec_start_pre = (
            f"ExecStartPre={{app_dir}}/.venv/bin/{app_name} migrate\n"
            f"ExecStartPre={{app_dir}}/.venv/bin/{app_name} collectstatic --no-input\n"
            f"ExecStartPre=/bin/bash -c 'sudo mkdir -p /var/www/{{app_name}}/static/ && sudo rsync -a --delete staticfiles/ /var/www/{{app_name}}/static/'\n"
        )

        # Web service with pre-start migrations
        web_service = systemd_dir / "web.service"
        web_service.write_text(
            SERVICE_TEMPLATE.format(
                description=f"Django web service for {app_name}",
                app_name="{app_name}",
                description_suffix="Django web server",
                service_type="notify",
                user="{user}",
                exec_start_pre=exec_start_pre,
                exec_start="{app_dir}/.venv/bin/gunicorn "
                + app_name
                + ".wsgi:application --bind unix:/run/{app_name}/web.sock",
            )
        )
        self.output.success(f"  Created {web_service}")

        # Socket file
        web_socket = systemd_dir / "web.socket"
        web_socket.write_text(
            SOCKET_TEMPLATE.format(
                name="web",
                app_name="{app_name}",
                instance_suffix="",
                template_suffix="",
                listen_stream="/run/{app_name}/web.sock",
                user="{user}",
            )
        )
        self.output.success(f"  Created {web_socket}")

        # Common drop-ins
        self._create_common_dropins(systemd_dir, app_name)

        # Caddyfile with static file serving
        self._create_caddyfile(
            fujin_dir,
            app_name,
            {
                "/static/*": {"static": f"/var/www/{app_name}/static/"},
                "/*": "web",
            },
        )

    def _generate_falco(self, app_name: str, fujin_dir: Path):
        """Generate Falco profile: web + worker services."""
        systemd_dir = fujin_dir / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)

        # Web service
        web_service = systemd_dir / "web.service"
        web_service.write_text(
            SERVICE_TEMPLATE.format(
                description=f"Falco web service for {app_name}",
                app_name="{app_name}",
                description_suffix="Falco web server",
                service_type="simple",
                user="{user}",
                exec_start_pre=f"ExecStartPre={{app_dir}}/.venv/bin/{app_name} setup\n",
                exec_start=f"{{app_dir}}/.venv/bin/{app_name} prodserver",
            )
        )
        self.output.success(f"  Created {web_service}")

        # Worker service
        worker_service = systemd_dir / "worker.service"
        worker_service.write_text(
            SERVICE_TEMPLATE.format(
                description=f"Falco worker service for {app_name}",
                app_name="{app_name}",
                description_suffix="Falco database worker",
                service_type="simple",
                user="{user}",
                exec_start_pre="",
                exec_start=f"{{app_dir}}/.venv/bin/{app_name} db_worker",
            )
        )
        self.output.success(f"  Created {worker_service}")

        # Common drop-ins
        self._create_common_dropins(systemd_dir, app_name)

        # Caddyfile
        self._create_caddyfile(fujin_dir, app_name, {"/*": "web"})

    def _generate_binary(self, app_name: str, fujin_dir: Path):
        """Generate binary profile: single binary deployment."""
        systemd_dir = fujin_dir / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)

        # Web service for binary
        web_service = systemd_dir / "web.service"
        web_service.write_text(
            SERVICE_TEMPLATE.format(
                description=f"Binary web service for {app_name}",
                app_name="{app_name}",
                description_suffix="web server (binary)",
                service_type="simple",
                user="{user}",
                exec_start_pre=f"ExecStartPre={{app_dir}}/{app_name} migrate\n",
                exec_start=f"{{app_dir}}/{app_name} prodserver",
            )
        )
        self.output.success(f"  Created {web_service}")

        # Common drop-ins
        self._create_common_dropins(systemd_dir, app_name)

        # Caddyfile
        self._create_caddyfile(fujin_dir, app_name, {"/*": "web"})
