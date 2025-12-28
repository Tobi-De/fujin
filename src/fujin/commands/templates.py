from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa
from rich.prompt import Confirm

from fujin.commands import BaseCommand


@cappa.command(help="Manage template files")
@dataclass
class Templates(BaseCommand):
    """Manage template files for systemd units and Caddy configuration."""

    @cappa.command(help="Copy templates to .fujin/ directory for customization")
    def eject(
        self,
        name: Annotated[
            str | None,
            cappa.Arg(help="Process name or 'caddy'. Omit to eject all templates."),
        ] = None,
    ):
        """
        Eject template files to .fujin/ directory for customization.

        Examples:
            fujin templates eject              # Eject all templates
            fujin templates eject web          # Eject web-specific templates
            fujin templates eject worker       # Eject worker-specific templates
            fujin templates eject caddy        # Eject Caddyfile template
        """

        local_config_dir = self.config.local_config_dir
        local_config_dir.mkdir(exist_ok=True)

        package_templates = self.config._package_templates_path()

        templates_to_eject = self._get_templates_to_eject(
            name, package_templates=package_templates
        )

        if not templates_to_eject:
            self.output.warning("No templates to eject")
            return

        ejected_files = []
        skipped_files = []

        for template_name in templates_to_eject:
            source_file = package_templates / template_name
            target_file = local_config_dir / template_name

            if not source_file.exists():
                self.output.warning(f"Template {template_name} not found in package")
                continue

            if target_file.exists():
                try:
                    overwrite = Confirm.ask(
                        f"[yellow]{template_name}[/yellow] already exists. Overwrite?",
                        default=False,
                    )
                except KeyboardInterrupt:
                    self.output.warning("\nEject cancelled")
                    raise cappa.Exit(code=0)

                if not overwrite:
                    skipped_files.append(template_name)
                    continue

            shutil.copy(source_file, target_file)
            ejected_files.append(template_name)

        if ejected_files:
            self.output.success(
                f"Ejected {len(ejected_files)} template(s) to {local_config_dir}/:"
            )
            for filename in ejected_files:
                self.output.output(f"  - [cyan]{filename}[/cyan]")

        if skipped_files:
            self.output.warning(f"Skipped {len(skipped_files)} existing template(s):")
            for filename in skipped_files:
                self.output.output(f"  - [dim]{filename}[/dim]")

        if not ejected_files and not skipped_files:
            self.output.warning("No templates ejected")

    def _get_templates_to_eject(
        self, name: str | None, package_templates: Path
    ) -> list[str]:
        if name is None:
            return sorted(
                [f.name for f in package_templates.iterdir() if f.suffix == ".j2"]
            )

        if name == "caddy":
            return ["Caddyfile.j2"]

        if name not in self.config.processes:
            available = ", ".join(self.config.processes.keys())
            raise cappa.Exit(
                f"Unknown process '{name}'. Available processes: {available}, caddy",
                code=1,
            )

        templates = []
        process_config = self.config.processes[name]

        if name == "web":
            templates.append("web.service.j2")
        else:
            templates.append("default.service.j2")

        if process_config.socket:
            templates.append("default.socket.j2")

        if process_config.timer:
            templates.append("default.timer.j2")

        return templates
