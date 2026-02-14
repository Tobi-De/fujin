from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Annotated

import cappa

from fujin.commands._base import MessageFormatter
from fujin.templates import (
    NEW_DROPIN_TEMPLATE,
    NEW_SERVICE_TEMPLATE,
    NEW_SOCKET_TEMPLATE,
    NEW_TIMER_SERVICE_TEMPLATE,
    NEW_TIMER_TEMPLATE,
)


@cappa.command(help="Create new systemd service, timer, socket, or dropin files")
@dataclass
class New:
    kind: Annotated[
        str,
        cappa.Arg(
            help="Type of unit to create",
            choices=["service", "timer", "dropin", "socket"],
        ),
    ]
    name: Annotated[
        str,
        cappa.Arg(help="Name of the unit (e.g., 'worker', 'web', 'cleanup')"),
    ]
    service: Annotated[
        str | None,
        cappa.Arg(
            long="--service",
            help="For dropin: apply to specific service (otherwise applies to all via common.d/)",
        ),
    ] = None

    @cached_property
    def output(self) -> MessageFormatter:
        return MessageFormatter(cappa.Output())

    def __call__(self):
        if self.kind == "service":
            self._create_service()
        elif self.kind == "timer":
            self._create_timer()
        elif self.kind == "socket":
            self._create_socket()
        elif self.kind == "dropin":
            self._create_dropin()

    def _ensure_systemd_dir(self) -> Path:
        systemd_dir = Path(".fujin/systemd")
        if not systemd_dir.exists():
            systemd_dir.mkdir(parents=True)
            self.output.info(f"Created {systemd_dir}/")
        return systemd_dir

    def _create_service(self):
        systemd_dir = self._ensure_systemd_dir()

        service_file = systemd_dir / f"{self.name}.service"
        if service_file.exists():
            self.output.error(f"{service_file} already exists")
            raise cappa.Exit(code=1)

        service_content = NEW_SERVICE_TEMPLATE.format(name=self.name)
        service_file.write_text(service_content)
        self.output.success(f"Created {service_file}")

        self.output.info(
            f"\nNext steps:\n"
            f"  1. Edit {service_file} to configure your service\n"
            f"  2. Deploy: fujin deploy"
        )

    def _create_timer(self):
        systemd_dir = self._ensure_systemd_dir()

        service_file = systemd_dir / f"{self.name}.service"
        timer_file = systemd_dir / f"{self.name}.timer"

        if service_file.exists() or timer_file.exists():
            self.output.error(f"Service or timer file already exists for '{self.name}'")
            raise cappa.Exit(code=1)

        service_content = NEW_TIMER_SERVICE_TEMPLATE.format(name=self.name)
        service_file.write_text(service_content)
        self.output.success(f"Created {service_file}")

        timer_content = NEW_TIMER_TEMPLATE.format(name=self.name)
        timer_file.write_text(timer_content)
        self.output.success(f"Created {timer_file}")

        self.output.info(
            f"\nNext steps:\n"
            f"  1. Edit {service_file} to configure your task\n"
            f"  2. Edit {timer_file} to set schedule (OnCalendar, OnBootSec, etc.)\n"
            f"  3. Deploy: fujin deploy"
        )

    def _create_socket(self):
        systemd_dir = self._ensure_systemd_dir()

        socket_file = systemd_dir / f"{self.name}.socket"
        if socket_file.exists():
            self.output.error(f"{socket_file} already exists")
            raise cappa.Exit(code=1)

        socket_content = NEW_SOCKET_TEMPLATE.format(name=self.name)
        socket_file.write_text(socket_content)
        self.output.success(f"Created {socket_file}")

        self.output.info(
            f"\nNext steps:\n"
            f"  1. Edit {socket_file} to configure your socket\n"
            f"  2. Ensure a matching {self.name}.service exists\n"
            f"  3. Deploy: fujin deploy"
        )

    def _create_dropin(self):
        systemd_dir = self._ensure_systemd_dir()

        if self.service:
            dropin_dir = systemd_dir / f"{self.service}.service.d"
            dropin_dir.mkdir(exist_ok=True)
            dropin_file = dropin_dir / f"{self.name}.conf"
        else:
            dropin_dir = systemd_dir / "common.d"
            dropin_dir.mkdir(exist_ok=True)
            dropin_file = dropin_dir / f"{self.name}.conf"

        if dropin_file.exists():
            self.output.error(f"{dropin_file} already exists")
            raise cappa.Exit(code=1)

        dropin_file.write_text(NEW_DROPIN_TEMPLATE)
        self.output.success(f"Created {dropin_file}")

        if self.service:
            self.output.info(
                f"\nThis dropin will apply only to {self.service}.service\n"
                f"Edit {dropin_file} to configure service overrides"
            )
        else:
            self.output.info(
                f"\nThis dropin will apply to ALL services\n"
                f"Edit {dropin_file} to configure common service settings"
            )
