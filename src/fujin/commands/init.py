from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa
import tomli_w

from fujin.commands import BaseCommand
from fujin.config import InstallationMode
from fujin.config import tomllib


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
        if fujin_toml.exists():
            self.output.warning("fujin.toml file already exists, skipping generation")
        else:
            profile_to_func = {
                "simple": simple_config,
                "falco": falco_config,
                "binary": binary_config,
                "django": django_config,
            }
            app_name = Path().resolve().stem.replace("-", "_").replace(" ", "_").lower()
            config = profile_to_func[self.profile](app_name)
            if not Path(".python-version").exists():
                config["python_version"] = "3.12"
                pyproject_toml = Path("pyproject.toml")
                if pyproject_toml.exists():
                    pyproject = tomllib.loads(pyproject_toml.read_text())
                    config["app"] = pyproject.get("project", {}).get("name", app_name)
                    if pyproject.get("project", {}).get("version"):
                        # fujin will read the version itself from the pyproject
                        config.pop("version")
            fujin_toml.write_text(tomli_w.dumps(config, multiline_strings=True))
            self.output.success("Sample configuration file generated successfully!")


def simple_config(app_name) -> dict:
    config = {
        "app": app_name,
        "version": "0.0.1",
        "build_command": "uv build && uv pip compile pyproject.toml -o requirements.txt > /dev/null",
        "distfile": f"dist/{app_name}-{{version}}-py3-none-any.whl",
        "requirements": "requirements.txt",
        "python_version": "3.12",
        "webserver": {
            "upstream": f"unix//run/{app_name}/{app_name}.sock",
        },
        "installation_mode": InstallationMode.PY_PACKAGE,
        "processes": {
            "web": {
                "command": f".venv/bin/gunicorn {app_name}.wsgi:application --bind unix:/run/{app_name}/{app_name}.sock",
                "socket": True,
            }
        },
        "aliases": {
            "shell": "app shell",
            "status": "app info",
            "logs": "app logs",
            "restart": "app restart",
        },
        "hosts": [
            {
                "user": "root",
                "domain_name": f"{app_name}.com",
                "envfile": ".env.prod",
            }
        ],
    }
    return config


def django_config(app_name) -> dict:
    config = {
        "app": app_name,
        "version": "0.0.1",
        "build_command": "uv build && uv pip compile pyproject.toml -o requirements.txt > /dev/null",
        "distfile": f"dist/{app_name}-{{version}}-py3-none-any.whl",
        "requirements": "requirements.txt",
        "python_version": "3.12",
        "webserver": {
            "upstream": f"unix//run/{app_name}/{app_name}.sock",
            "statics": {"/static/*": f"/var/www/{app_name}/static/"},
        },
        "release_command": f"{app_name} migrate && {app_name} collectstatic --no-input && sudo mkdir -p /var/www/{app_name}/static/ && sudo rsync  -a --delete staticfiles/ /var/www/{app_name}/static/",
        "installation_mode": InstallationMode.PY_PACKAGE,
        "processes": {
            "web": {
                "command": f".venv/bin/gunicorn {app_name}.wsgi:application --bind unix:/run/{app_name}/{app_name}.sock",
                "socket": True,
            }
        },
        "aliases": {
            "shell": "server exec --appenv -i bash",
            "status": "app info",
        },
        "hosts": [
            {
                "user": "root",
                "domain_name": f"{app_name}.com",
                "envfile": ".env.prod",
            }
        ],
    }
    return config


def falco_config(app_name: str) -> dict:
    config = simple_config(app_name)
    config.update(
        {
            "release_command": f"{app_name} setup",
            "processes": {
                "web": {"command": f".venv/bin/{app_name} prodserver"},
                "worker": {"command": f".venv/bin/{app_name} db_worker"},
            },
            "webserver": {
                "upstream": "localhost:8000",
            },
            "aliases": {
                "console": f"app shell '{app_name} shell'",
                "dbconsole": f"app shell '{app_name} dbshell'",
                "print_settings": "app exec print_settings --format=pprint",
                "shell": "app shell",
                "status": "app info",
            },
            "hosts": [
                {
                    "user": "root",
                    "domain_name": f"{app_name}.com",
                    "envfile": ".env.prod",
                }
            ],
        }
    )
    return config


def binary_config(app_name: str) -> dict:
    return {
        "app": app_name,
        "version": "0.0.1",
        "build_command": "just build-bin",
        "distfile": f"dist/bin/{app_name}-{{version}}",
        "webserver": {
            "upstream": "localhost:8000",
        },
        "release_command": f"{app_name} migrate",
        "installation_mode": InstallationMode.BINARY,
        "processes": {"web": {"command": f"{app_name} prodserver"}},
        "aliases": {
            "shell": "app shell",
            "status": "app info",
        },
        "hosts": [
            {
                "user": "root",
                "domain_name": f"{app_name}.com",
                "envfile": ".env.prod",
            }
        ],
    }
