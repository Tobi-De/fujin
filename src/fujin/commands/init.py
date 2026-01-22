from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa
import tomli_w

from fujin.commands import BaseCommand
from fujin.config import InstallationMode
from fujin.config import tomllib
from fujin.templates import CADDYFILE_TEMPLATE
from fujin.templates import NEW_SERVICE_TEMPLATE


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
        service_content = NEW_SERVICE_TEMPLATE.format(name="web")
        # Customize ExecStart for gunicorn
        service_content = service_content.replace(
            "ExecStart={install_dir}/.venv/bin/python -m myapp.web",
            f"ExecStart={{install_dir}}/.venv/bin/gunicorn {app_name}.wsgi:application --bind 0.0.0.0:8000",
        )
        web_service.write_text(service_content)
        self.output.success(f"  Created {web_service}")

        self._create_caddyfile(fujin_dir, app_name, "localhost:8000")

    def _generate_django(self, app_name: str, fujin_dir: Path):
        """Generate Django profile: web service with migrations."""
        systemd_dir = fujin_dir / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)

        # Web service with pre-start migrations
        web_service = systemd_dir / "web.service"
        service_content = NEW_SERVICE_TEMPLATE.format(name="web")

        # Add ExecStartPre for migrations and collectstatic
        exec_start_pre_lines = (
            f"# Run migrations and collect static files before starting\n"
            f"ExecStartPre={{install_dir}}/.venv/bin/{app_name} migrate\n"
            f"ExecStartPre={{install_dir}}/.venv/bin/{app_name} collectstatic --no-input\n"
            f"ExecStartPre=/bin/bash -c 'rsync -a --delete staticfiles/ {{app_dir}}/staticfiles/'\n"
        )
        service_content = service_content.replace(
            "# Main command - adjust to match your application\n", exec_start_pre_lines
        )

        # Customize ExecStart for gunicorn
        service_content = service_content.replace(
            "ExecStart={install_dir}/.venv/bin/python -m myapp.web",
            f"ExecStart={{install_dir}}/.venv/bin/gunicorn {app_name}.wsgi:application --bind 0.0.0.0:8000",
        )
        web_service.write_text(service_content)
        self.output.success(f"  Created {web_service}")

        self._create_caddyfile(
            fujin_dir, app_name, "localhost:8000", static_path="{app_dir}/staticfiles/"
        )

    def _generate_falco(self, app_name: str, fujin_dir: Path):
        """Generate Falco profile: web + worker services."""
        systemd_dir = fujin_dir / "systemd"
        dropin_dir = systemd_dir / "common.d"
        dropin_dir.mkdir(parents=True, exist_ok=True)

        dropin = dropin_dir / "base.conf"
        dropin.write_text(
            f"""
[Service]
Type=simple
User={{app_user}}
WorkingDirectory={{app_dir}}
EnvironmentFile={{install_dir}}/.env
RuntimeDirectory={app_name}
RuntimeDirectoryMode=0755
Restart=on-failure
RestartSec=5s

# Security Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={{app_dir}}
        """
        )

        web_service = systemd_dir / "web.service"
        web_service.write_text(
            f"""
[Unit]
Description={app_name} web
After=network.target

[Service]
UMask=0002
ExecStartPre={{install_dir}}/.venv/bin/{app_name} setup
ExecStart={{install_dir}}/.venv/bin/{app_name} prodserver --uds /run/{app_name}/web.sock
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
"""
        )
        self.output.success(f"  Created {web_service}")

        # Worker service
        worker_service = systemd_dir / "worker.service"
        worker_service.write_text(
            f"""
[Unit]
Description={app_name} worker
After=network.target

[Service]
ExecStart={{install_dir}}/.venv/bin/{app_name} db_worker

[Install]
WantedBy=multi-user.target
"""
        )
        self.output.success(f"  Created {worker_service}")

        self._create_caddyfile(fujin_dir, app_name, f"unix//run/{app_name}/web.sock")

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
            f"# Run migrations before starting\nExecStartPre={{app_dir}}/{app_name} migrate\n\n",
        )

        # Customize ExecStart for binary (no .venv)
        service_content = service_content.replace(
            "ExecStart={install_dir}/.venv/bin/python -m myapp.web",
            f"ExecStart={{app_dir}}/{app_name} prodserver",
        )
        web_service.write_text(service_content)
        self.output.success(f"  Created {web_service}")

        self._create_caddyfile(fujin_dir, app_name, "localhost:8000")
