from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa
import tomli_w

from fujin.commands import BaseCommand
from fujin.config import tomllib


@cappa.command(help="Scale a service to run multiple replicas")
@dataclass(kw_only=True)
class Scale(BaseCommand):
    service: Annotated[
        str,
        cappa.Arg(help="Name of the service to scale (e.g., 'worker', 'web')"),
    ]

    count: Annotated[
        int,
        cappa.Arg(help="Number of replicas (1 for single instance, 2+ for template)"),
    ]

    def __call__(self):
        if self.count == 0:
            self.output.error(
                f"Cannot scale to 0. To stop the service, use:\n"
                f"  fujin app stop {self.service}\n\n"
                f"To remove the service entirely, delete the files manually:\n"
                f"  rm .fujin/systemd/{self.service}*.service .fujin/systemd/{self.service}*.socket"
            )
            raise cappa.Exit(code=1)

        if self.count < 0:
            self.output.error("Replica count must be 1 or greater")
            raise cappa.Exit(code=1)

        systemd_dir = Path(".fujin/systemd")
        deployed_unit = None
        for unit in self.config.deployed_units:
            if unit.service_name == self.service:
                deployed_unit = unit
                break

        if not deployed_unit:
            self.output.error(
                f"Service '{self.service}' not found in {systemd_dir}/\n"
                f"Use 'fujin new service {self.service}' to create it first."
            )
            raise cappa.Exit(code=1)

        old_replica_count = deployed_unit.replica_count
        old_is_template = deployed_unit.is_template
        needs_conversion = False

        if self.count == 1:
            # Scale to 1 - convert template to regular or keep regular
            if deployed_unit.is_template:
                # Convert template to regular
                template_service = systemd_dir / f"{self.service}@.service"
                regular_service = systemd_dir / f"{self.service}.service"
                content = template_service.read_text()
                # Remove %i, %I template specifiers (basic conversion)
                content = content.replace("%i", "").replace("%I", "")
                regular_service.write_text(content)
                template_service.unlink()
                self.output.success(
                    f"Converted {template_service.name} → {regular_service.name}"
                )
                needs_conversion = True
            else:
                self.output.info(
                    f"{self.service} already configured for single instance"
                )
            self._update_replicas_config(self.service, None)

        else:
            # Scale to 2+ - convert to template or update
            if not deployed_unit.is_template:
                # Convert regular to template
                regular_service = systemd_dir / f"{self.service}.service"
                template_service = systemd_dir / f"{self.service}@.service"
                content = regular_service.read_text()
                # Add %i to Description if it contains the service name
                if f"{{{{app_name}}}} {self.service}" in content:
                    content = content.replace(
                        f"{{{{app_name}}}} {self.service}",
                        f"{{{{app_name}}}} {self.service} %i",
                    )
                template_service.write_text(content)
                regular_service.unlink()
                self.output.success(
                    f"Converted {regular_service.name} → {template_service.name}"
                )
                needs_conversion = True
            else:
                self.output.info(f"{self.service} already configured as template")

            if deployed_unit.socket_file:
                self.output.warning(
                    f"\n[bold]Warning: Scaling a socket-activated service is not recommended.[/bold]\n\n"
                    f"Socket file {deployed_unit.socket_file.name} found. Sockets don't scale well because:\n"
                    f"  - Only one socket exists for all replicas\n"
                    f"  - Socket activation happens per-connection, not per-replica\n"
                    f"  - Your web server likely has built-in concurrency/worker settings\n\n"
                    f"Instead of scaling replicas, configure your web server:\n"
                    f"  - Gunicorn: --workers N or --threads N\n"
                    f"  - Uvicorn: --workers N\n"
                    f"  - Other servers: check their concurrency/worker documentation\n"
                )
            self._update_replicas_config(self.service, self.count)

        # Only scale on server if:
        # 1. No conversion needed (service was already a template)
        # 2. Count changed
        if needs_conversion:
            self.output.info(
                f"\nNext steps:\n  1. Deploy to apply changes: fujin deploy"
            )
        elif old_replica_count != self.count and old_is_template:
            self._scale_on_server(old_replica_count, self.count)
        elif old_replica_count == self.count:
            self.output.info(f"\nService already at {self.count} replica(s)")

    def _scale_on_server(self, old_count: int, new_count: int):
        with self.connection() as conn:
            app_name = self.config.app_name

            if new_count > old_count:
                # Scaling up - start new instances
                self.output.info(
                    f"Scaling up {self.service} from {old_count} to {new_count} instances..."
                )
                new_instances = [
                    f"{app_name}-{self.service}@{i}.service"
                    for i in range(old_count + 1, new_count + 1)
                ]
                instances_str = " ".join(new_instances)
                _, success = conn.run(
                    f"sudo systemctl start {instances_str} && sudo systemctl enable {instances_str}"
                )
                if success:
                    self.output.success(f"Started {len(new_instances)} new instance(s)")
                else:
                    self.output.error(f"Failed to start new instances")

            elif new_count < old_count:
                # Scaling down - stop old instances
                self.output.info(
                    f"Scaling down {self.service} from {old_count} to {new_count} instances..."
                )
                removed_instances = [
                    f"{app_name}-{self.service}@{i}.service"
                    for i in range(new_count + 1, old_count + 1)
                ]
                instances_str = " ".join(removed_instances)
                _, success = conn.run(
                    f"sudo systemctl stop {instances_str} && sudo systemctl disable {instances_str}"
                )
                if success:
                    self.output.success(f"Stopped {len(removed_instances)} instance(s)")
                else:
                    self.output.error(f"Failed to stop instances")

    def _update_replicas_config(self, service_name: str, count: int | None):
        fujin_toml = Path("fujin.toml")
        if not fujin_toml.exists():
            return

        config_dict = tomllib.loads(fujin_toml.read_text())
        replicas = config_dict.get("replicas", {})

        if count is None:
            if service_name in replicas:
                del replicas[service_name]
                self.output.success(f"Removed replica config for {service_name}")
        else:
            replicas[service_name] = count
            self.output.success(
                f"Updated fujin.toml: {service_name} = {count} replicas"
            )

        config_dict["replicas"] = replicas
        if not replicas:
            del config_dict["replicas"]

        fujin_toml.write_text(tomli_w.dumps(config_dict, multiline_strings=True))
