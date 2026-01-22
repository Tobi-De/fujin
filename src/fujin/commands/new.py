from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa

from fujin.commands import BaseCommand
from fujin.templates import NEW_DROPIN_TEMPLATE
from fujin.templates import NEW_SERVICE_TEMPLATE
from fujin.templates import NEW_TIMER_SERVICE_TEMPLATE
from fujin.templates import NEW_TIMER_TEMPLATE


@cappa.command(help="Create new systemd service, timer, or dropin files")
@dataclass
class New(BaseCommand):
    """
    Examples:
      fujin new service worker          Create a new service file
      fujin new timer cleanup           Create a scheduled task
      fujin new dropin resources        Create common dropin for all services
      fujin new dropin --service web limits  Create dropin for specific service
    """

    @cappa.command(help="Create a new systemd service file")
    def service(
        self,
        name: Annotated[
            str, cappa.Arg(help="Name of the service (e.g., 'worker', 'web')")
        ],
    ):
        systemd_dir = Path(".fujin/systemd")
        if not systemd_dir.exists():
            systemd_dir.mkdir(parents=True)
            self.output.info(f"Created {systemd_dir}/")

        service_file = systemd_dir / f"{name}.service"
        if service_file.exists():
            self.output.error(f"{service_file} already exists")
            raise cappa.Exit(code=1)

        service_content = NEW_SERVICE_TEMPLATE.format(name=name)
        service_file.write_text(service_content)
        self.output.success(f"Created {service_file}")

        self.output.info(
            f"\nNext steps:\n"
            f"  1. Edit {service_file} to configure your service\n"
            f"  2. Deploy: fujin deploy"
        )

    @cappa.command(help="Create a new systemd timer and service")
    def timer(
        self,
        name: Annotated[
            str, cappa.Arg(help="Name of the timer (e.g., 'cleanup', 'backup')")
        ],
    ):
        systemd_dir = Path(".fujin/systemd")
        if not systemd_dir.exists():
            systemd_dir.mkdir(parents=True)
            self.output.info(f"Created {systemd_dir}/")

        service_file = systemd_dir / f"{name}.service"
        timer_file = systemd_dir / f"{name}.timer"

        if service_file.exists() or timer_file.exists():
            self.output.error(f"Service or timer file already exists for '{name}'")
            raise cappa.Exit(code=1)

        service_content = NEW_TIMER_SERVICE_TEMPLATE.format(name=name)
        service_file.write_text(service_content)
        self.output.success(f"Created {service_file}")

        timer_content = NEW_TIMER_TEMPLATE.format(name=name)
        timer_file.write_text(timer_content)
        self.output.success(f"Created {timer_file}")

        self.output.info(
            f"\nNext steps:\n"
            f"  1. Edit {service_file} to configure your task\n"
            f"  2. Edit {timer_file} to set schedule (OnCalendar, OnBootSec, etc.)\n"
            f"  3. Deploy: fujin deploy"
        )

    @cappa.command(help="Create a new systemd dropin configuration")
    def dropin(
        self,
        name: Annotated[
            str,
            cappa.Arg(help="Name of the dropin file (e.g., 'resources', 'security')"),
        ],
        service: Annotated[
            str | None,
            cappa.Arg(
                long="--service",
                help="Apply to specific service (if not set, applies to all services via common.d/)",
            ),
        ] = None,
    ):
        systemd_dir = Path(".fujin/systemd")
        if not systemd_dir.exists():
            systemd_dir.mkdir(parents=True)
            self.output.info(f"Created {systemd_dir}/")

        if service:
            dropin_dir = systemd_dir / f"{service}.service.d"
            dropin_dir.mkdir(exist_ok=True)
            dropin_file = dropin_dir / f"{name}.conf"
        else:
            dropin_dir = systemd_dir / "common.d"
            dropin_dir.mkdir(exist_ok=True)
            dropin_file = dropin_dir / f"{name}.conf"

        if dropin_file.exists():
            self.output.error(f"{dropin_file} already exists")
            raise cappa.Exit(code=1)

        dropin_file.write_text(NEW_DROPIN_TEMPLATE)
        self.output.success(f"Created {dropin_file}")

        if service:
            self.output.info(
                f"\nThis dropin will apply only to {service}.service\n"
                f"Edit {dropin_file} to configure service overrides"
            )
        else:
            self.output.info(
                f"\nThis dropin will apply to ALL services\n"
                f"Edit {dropin_file} to configure common service settings"
            )
