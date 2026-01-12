from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import cappa

from fujin.commands import BaseCommand
from fujin.templates import NEW_DROPIN_TEMPLATE
from fujin.templates import NEW_SERVICE_TEMPLATE
from fujin.templates import NEW_SOCKET_TEMPLATE
from fujin.templates import NEW_TIMER_SERVICE_TEMPLATE
from fujin.templates import NEW_TIMER_TEMPLATE


@cappa.command(help="Create new systemd service, timer, or dropin files")
class New(BaseCommand):
    """
    Examples:
      fujin new service worker          Create a new service file
      fujin new service web --socket    Create service with socket activation
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
        socket: Annotated[
            bool,
            cappa.Arg(
                long="--socket",
                help="Create socket activation unit alongside service",
            ),
        ] = False,
    ):
        systemd_dir = Path(".fujin/systemd")
        if not systemd_dir.exists():
            systemd_dir.mkdir(parents=True)
            self.output.info(f"Created {systemd_dir}/")

        service_file = systemd_dir / f"{name}.service"
        if service_file.exists():
            self.output.error(f"{service_file} already exists")
            sys.exit(1)

        # Create service file using template
        service_content = NEW_SERVICE_TEMPLATE.format(name=name)
        service_file.write_text(service_content)
        self.output.success(f"Created {service_file}")

        if socket:
            socket_file = systemd_dir / f"{name}.socket"
            socket_content = NEW_SOCKET_TEMPLATE.format(name=name)
            socket_file.write_text(socket_content)
            self.output.success(f"Created {socket_file}")

            self.output.info(
                f"\nNext steps:\n"
                f"  1. Edit {service_file} to configure your service\n"
                f"  2. Edit {socket_file} to configure socket activation\n"
                f"  3. Update your fujin.toml processes configuration\n"
                f"  4. Deploy: fujin deploy"
            )
        else:
            self.output.info(
                f"\nNext steps:\n"
                f"  1. Edit {service_file} to configure your service\n"
                f"  2. Update your fujin.toml processes configuration\n"
                f"  3. Deploy: fujin deploy"
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
            sys.exit(1)

        # Create service file (oneshot) using template
        service_content = NEW_TIMER_SERVICE_TEMPLATE.format(name=name)
        service_file.write_text(service_content)
        self.output.success(f"Created {service_file}")

        # Create timer file using template
        timer_content = NEW_TIMER_TEMPLATE.format(name=name)
        timer_file.write_text(timer_content)
        self.output.success(f"Created {timer_file}")

        self.output.info(
            f"\nNext steps:\n"
            f"  1. Edit {service_file} to configure your task\n"
            f"  2. Edit {timer_file} to set schedule (OnCalendar, OnBootSec, etc.)\n"
            f"  3. Update your fujin.toml processes configuration\n"
            f"  4. Deploy: fujin deploy"
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
            # Service-specific dropin
            dropin_dir = systemd_dir / f"{service}.service.d"
            dropin_dir.mkdir(exist_ok=True)
            dropin_file = dropin_dir / f"{name}.conf"
        else:
            # Common dropin
            dropin_dir = systemd_dir / "common.d"
            dropin_dir.mkdir(exist_ok=True)
            dropin_file = dropin_dir / f"{name}.conf"

        if dropin_file.exists():
            self.output.error(f"{dropin_file} already exists")
            sys.exit(1)

        # Create dropin file using template
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
