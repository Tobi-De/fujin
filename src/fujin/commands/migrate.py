from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa

from fujin.commands import BaseCommand
from fujin.config import tomllib
from fujin.gitignore import (
    CACHE_PATTERN,
    ensure_fujin_cache_ignored,
    is_fujin_cache_ignored,
)
from fujin.templates import NEW_SERVICE_TEMPLATE

# Migration-specific Caddy templates for converting old config format
_CADDYFILE_HEADER = """# Caddyfile for {app_name}
# Learn more: https://caddyserver.com/docs/caddyfile

{domain} {{
"""

_CADDY_HANDLE_STATIC = """
    # Serve static files
    handle_path {path} {{
        root * {root}
        file_server
    }}
"""

_CADDY_HANDLE_PROXY = """
    # Proxy to {name} service
    handle {path} {{
{extra_directives}        reverse_proxy {upstream}
    }}
"""


@cappa.command(help="Migrate fujin.toml and generate .fujin/ directory structure")
@dataclass
class Migrate(BaseCommand):
    backup: Annotated[
        bool,
        cappa.Arg(
            long="--backup",
            help="Create backup of original file (fujin.toml.backup)",
        ),
    ] = False
    dry_run: Annotated[
        bool,
        cappa.Arg(
            long="--dry-run",
            help="Show what would change without writing files",
        ),
    ] = False

    def __call__(self):
        fujin_toml = Path("fujin.toml")
        if not fujin_toml.exists():
            self.output.error("No fujin.toml file found in the current directory")
            raise cappa.Exit(code=1)

        # Read raw TOML
        try:
            config_dict = tomllib.loads(fujin_toml.read_text())
        except Exception as e:
            self.output.error(f"Failed to parse fujin.toml: {e}")
            raise cappa.Exit(code=1) from None

        # Check if already file-based
        if "processes" not in config_dict and "sites" not in config_dict:
            fujin_dir = Path(".fujin")
            if fujin_dir.exists() and (fujin_dir / "systemd").exists():
                self.output.info("Configuration is already file-based")
                return

        # Check if migration is needed
        needs_migration = bool(
            config_dict.get("processes")
            or config_dict.get("sites")
            or config_dict.get("webserver")
            or config_dict.get("release_command")
        )

        if not needs_migration:
            self.output.info("No migration needed - configuration is up to date")
            return

        self.output.info("[bold]File-Based Migration[/bold]")
        self.output.info(
            "Converting template-based config to file-based structure (.fujin/ directory)"
        )

        self._preview_changes(config_dict)

        if self.dry_run:
            self.output.info("\n[dim]Dry run - no changes written[/dim]")
            return

        if self.backup:
            backup_path = Path("fujin.toml.backup")
            if backup_path.exists():
                self.output.warning(f"Backup already exists: {backup_path}")
            else:
                shutil.copy2(fujin_toml, backup_path)
                self.output.success(f"Backup created: {backup_path}")

        self._migrate_to_file_based(config_dict)

        if ensure_fujin_cache_ignored():
            self.output.success(f"Added {CACHE_PATTERN} to .gitignore")

        self.output.success("\nMigration completed successfully!")
        self.output.info(
            "\nNext steps:\n"
            "  1. Review generated files in .fujin/systemd/\n"
            "  2. Customize service files if needed\n"
            "  3. Review .fujin/Caddyfile\n"
            "  4. Test your deployment: fujin deploy"
        )

    def _preview_changes(self, config: dict):
        """Show what files will be created/modified."""
        config.get("app", "app")
        processes = config.get("processes", {})
        sites = config.get("sites", [])
        release_command = config.get("release_command")

        self.output.output("\n[bold cyan]Files to be created:[/bold cyan]")

        if processes:
            self.output.output("  .fujin/systemd/")
            for name, process in processes.items():
                replicas = (
                    process.get("replicas", 1) if isinstance(process, dict) else 1
                )
                suffix = "@" if replicas > 1 else ""
                self.output.output(f"    ├── {name}{suffix}.service")
                if isinstance(process, dict):
                    if process.get("socket"):
                        self.output.output(f"    ├── {name}.socket")
                    if process.get("timer"):
                        self.output.output(f"    ├── {name}.timer")

        if sites:
            self.output.output("  .fujin/Caddyfile")

        self.output.output("\n[bold cyan]fujin.toml changes:[/bold cyan]")
        changes = []

        if processes:
            replicas = {
                name: proc.get("replicas", 1)
                for name, proc in processes.items()
                if isinstance(proc, dict) and proc.get("replicas", 1) > 1
            }
            if replicas:
                changes.append(
                    f"  + \\[replicas] section with {len(replicas)} service(s)"
                )
            changes.append(f"  - \\[processes] section ({len(processes)} process(es))")

        if sites:
            changes.append(f"  - \\[sites] section ({len(sites)} site(s))")

        if "webserver" in config:
            changes.append("  - \\[webserver] section")

        if release_command:
            changes.append("  + \\[hooks].post_install (from release_command)")
            changes.append("  - release_command (removed)")

        # Check if any host has apps_dir field
        hosts = config.get("hosts", [])
        if any("apps_dir" in host for host in hosts):
            changes.append("  - apps_dir from host configs (deprecated)")

        for change in changes:
            self.output.output(change)

        self.output.output("\n[bold cyan].gitignore:[/bold cyan]")
        if is_fujin_cache_ignored():
            self.output.output(f"  {CACHE_PATTERN} already ignored")
        else:
            self.output.output(f"  + {CACHE_PATTERN}")

    def _migrate_to_file_based(self, config: dict):
        """Migrate config to file-based structure."""
        app_name = config.get("app", "app")
        processes = config.get("processes", {})
        sites = config.get("sites", [])
        release_command = config.get("release_command")
        installation_mode = config.get("installation_mode", "python-package")

        fujin_dir = Path(".fujin")
        systemd_dir = fujin_dir / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)

        # Generate systemd service files from processes
        replicas = {}
        for name, process_config in processes.items():
            if isinstance(process_config, str):
                command = process_config
                num_replicas = 1
                socket = False
                timer = None
            else:
                command = process_config.get("command", "")
                num_replicas = process_config.get("replicas", 1)
                socket = process_config.get("socket", False)
                timer = process_config.get("timer")

            if num_replicas > 1:
                replicas[name] = num_replicas

            service_file = self._generate_service_file(
                name=name,
                command=command,
                replicas=num_replicas,
                installation_mode=installation_mode,
                app_name=app_name,
            )

            # Write service file (templated if replicas > 1)
            service_path = systemd_dir / (
                f"{name}@.service" if num_replicas > 1 else f"{name}.service"
            )
            service_path.write_text(service_file)
            self.output.success(f"Created {service_path}")

            # Socket and timer units are always singletons: fujin discovers and
            # installs ``<name>.socket``/``<name>.timer`` for every service,
            # including template (``@``) services.
            if socket:
                socket_file = self._generate_socket_file(name, num_replicas > 1)
                socket_path = systemd_dir / f"{name}.socket"
                socket_path.write_text(socket_file)
                self.output.success(f"Created {socket_path}")

            if timer:
                timer_file = self._generate_timer_file(name, timer)
                timer_path = systemd_dir / f"{name}.timer"
                timer_path.write_text(timer_file)
                self.output.success(f"Created {timer_path}")

        # Generate Caddyfile from sites
        if sites:
            caddyfile = self._generate_caddyfile(sites, processes)
            (fujin_dir / "Caddyfile").write_text(caddyfile)
            self.output.success(f"Created {fujin_dir / 'Caddyfile'}")

        # Update fujin.toml
        updated_config = dict(config)

        # Add replicas section if needed
        if replicas:
            updated_config["replicas"] = replicas

        # release_command ran on every deploy against the live tree; the
        # file-based equivalent is a post_install hook, which runs once per
        # release inside that release's directory.
        post_install, verbatim = self._convert_release_command(
            release_command, app_name, installation_mode
        )
        if post_install:
            hooks = updated_config.get("hooks")
            if isinstance(hooks, dict):
                existing = hooks.get("post_install", [])
                hooks["post_install"] = [*existing, *post_install]
            else:
                updated_config["hooks"] = {"post_install": post_install}
        if verbatim:
            self.output.warning(
                "release_command contains commands that were copied verbatim; "
                "review them: hooks run as the deploy user inside the release "
                "directory (no sudo, and /var/www is not writable):\n"
                + "\n".join(f"  - {cmd}" for cmd in verbatim)
            )

        # Remove old sections
        updated_config.pop("processes", None)
        updated_config.pop("sites", None)
        updated_config.pop("webserver", None)
        updated_config.pop("release_command", None)

        # Remove deprecated host config fields
        if "hosts" in updated_config:
            for host in updated_config["hosts"]:
                host.pop("apps_dir", None)  # Remove deprecated apps_dir

        # Write updated fujin.toml
        import tomli_w

        fujin_toml_content = tomli_w.dumps(updated_config, multiline_strings=True)
        Path("fujin.toml").write_text(fujin_toml_content)
        self.output.success("Updated fujin.toml")

    def _generate_service_file(
        self,
        name: str,
        command: str,
        replicas: int,
        installation_mode: str,
        app_name: str,
    ) -> str:
        """Generate systemd service file content."""
        service_content = NEW_SERVICE_TEMPLATE.format(name=name)

        # Customize ExecStart from the process command. The template default is
        # ``{app_dir}/current/venv/bin/python -m myapp.<name>``; commands that
        # cannot be mapped keep the template executable and their arguments.
        default_exec = f"{{app_dir}}/current/venv/bin/python -m myapp.{name}"
        exec_start = self._derive_exec_start(command, installation_mode, app_name)
        if not exec_start:
            exec_start = default_exec
        service_content = service_content.replace(
            f"ExecStart={default_exec}",
            f"ExecStart={exec_start}",
        )

        # Handle instance suffix for templated units
        if replicas > 1:
            service_content = service_content.replace(
                f"Description={{app_name}} {name}",
                f"Description={{app_name}} {name} %i",
            )

        return service_content

    def _derive_exec_start(
        self, command: str, installation_mode: str, app_name: str
    ) -> str:
        """Map a legacy process command to a release-tree ExecStart.

        Arguments are preserved verbatim; only the executable prefix is
        rewritten. Already-templated commands pass through untouched.
        """
        command = (command or "").strip()
        if not command:
            return ""

        if command.startswith("{"):
            return command

        if command.startswith(".venv/"):
            return "{app_dir}/current/venv/" + command[len(".venv/") :]

        if installation_mode == "binary":
            if command == app_name or command.startswith(f"{app_name} "):
                return f"{{app_dir}}/current/bin/{app_name}" + command[len(app_name) :]
            if command.startswith(f"./{app_name}") and (
                len(command) == len(app_name) + 2 or command[len(app_name) + 2] == " "
            ):
                return (
                    f"{{app_dir}}/current/bin/{app_name}" + command[len(app_name) + 2 :]
                )

        return command

    def _convert_release_command(
        self,
        release_command: str | None,
        app_name: str,
        installation_mode: str = "python-package",
    ) -> tuple[list[str], list[str]]:
        """Turn ``release_command`` into ``[hooks].post_install`` entries.

        App-binary commands are rewritten to the release's own executable
        (``venv/bin/<app>`` for python packages, ``bin/<app>`` for binaries);
        a leading ``./<app>`` or an already-templated executable such as
        ``{app_dir}/current/bin/<app>`` counts as the app binary too.

        Returns ``(hooks, verbatim)`` where ``verbatim`` lists commands that
        were not rewritten (they are kept as-is; hooks run as the deploy user
        in the release directory).
        """
        if not release_command:
            return [], []

        binary_dir = "venv/bin" if installation_mode != "binary" else "bin"
        hooks: list[str] = []
        verbatim: list[str] = []
        for cmd in release_command.split("&&"):
            cmd = cmd.strip()
            if not cmd:
                continue
            parts = cmd.split(None, 1)
            if self._is_app_binary(parts[0], app_name):
                rest = parts[1] if len(parts) > 1 else ""
                hooks.append(
                    f"{{app_dir}}/{binary_dir}/{app_name}"
                    + (f" {rest}" if rest else "")
                )
            else:
                hooks.append(cmd)
                verbatim.append(cmd)

        return hooks, verbatim

    @staticmethod
    def _is_app_binary(token: str, app_name: str) -> bool:
        """Whether a first command token refers to the app executable."""
        if token in (app_name, f"./{app_name}"):
            return True
        if token.startswith("{"):
            executable = token.rstrip("/").rsplit("/", 1)[-1]
            return executable in (app_name, "{app_name}")
        return False

    def _generate_socket_file(self, name: str, is_template: bool) -> str:
        """Generate systemd socket file content.

        Sockets are singletons even when the service is a template; the
        ``PartOf`` dependency is therefore only valid for a non-template
        service. ``ListenStream`` stays instance-less so the Caddy upstream
        (``unix//run/{app_name}/<name>.sock``) matches the bound socket.
        """
        socket_content = NEW_SOCKET_TEMPLATE.format(name=name)

        part_of = f"PartOf={name}.service\n"
        if is_template:
            socket_content = socket_content.replace(part_of, "")
        else:
            socket_content = socket_content.replace(
                part_of, f"PartOf={{app_name}}-{name}.service\n"
            )

        return socket_content

    def _generate_timer_file(self, name: str, timer_config: dict) -> str:
        """Generate systemd timer file content (always a singleton unit)."""

        # Build timer config section
        timer_lines = []
        if timer_config.get("on_calendar"):
            timer_lines.append(f"OnCalendar={timer_config['on_calendar']}")
        if timer_config.get("on_boot_sec"):
            timer_lines.append(f"OnBootSec={timer_config['on_boot_sec']}")
        if timer_config.get("on_unit_active_sec"):
            timer_lines.append(f"OnUnitActiveSec={timer_config['on_unit_active_sec']}")
        if timer_config.get("on_active_sec"):
            timer_lines.append(f"OnActiveSec={timer_config['on_active_sec']}")

        if timer_config.get("persistent", False):
            timer_lines.append("Persistent=true")
        if timer_config.get("randomized_delay_sec"):
            timer_lines.append(
                f"RandomizedDelaySec={timer_config['randomized_delay_sec']}"
            )

        timer_content = "\n".join(timer_lines) + "\n" if timer_lines else ""

        return TIMER_TEMPLATE.format(
            name=name,
            app_name="{app_name}",
            timer_config=timer_content,
        )

    def _generate_caddyfile(self, sites: list, processes: dict) -> str:
        """Generate Caddyfile from sites configuration."""
        if not sites:
            return ""

        site = sites[0]  # Use first site
        domains = site.get("domains", [])
        routes = site.get("routes", {})

        domain = domains[0] if domains else "example.com"

        # We need {app_name} to be literal in the Caddyfile for deploy time substitution
        content = _CADDYFILE_HEADER.format(app_name="{app_name}", domain=domain)

        # Process routes
        for path, target in routes.items():
            if isinstance(target, dict):
                # RouteConfig format
                if "static" in target:
                    static_path = target["static"]
                    content += _CADDY_HANDLE_STATIC.format(path=path, root=static_path)
                elif "process" in target:
                    process_name = target["process"]
                    strip_prefix = target.get("strip_prefix")

                    # Determine upstream
                    process_config = processes.get(process_name, {})
                    if isinstance(process_config, dict):
                        listen = process_config.get("listen", "localhost:8000")
                        socket = process_config.get("socket", False)

                        if socket:
                            upstream = f"unix//run/{{app_name}}/{process_name}.sock"
                        else:
                            upstream = listen
                    else:
                        upstream = "localhost:8000"

                    extra = (
                        f"        uri strip_prefix {strip_prefix}\n"
                        if strip_prefix
                        else ""
                    )
                    content += _CADDY_HANDLE_PROXY.format(
                        name=process_name,
                        path=path,
                        upstream=upstream,
                        extra_directives=extra,
                    )

            else:
                # Simple string format (process name)
                process_name = target
                process_config = processes.get(process_name, {})

                if isinstance(process_config, dict):
                    listen = process_config.get("listen", "localhost:8000")
                    socket = process_config.get("socket", False)

                    if socket:
                        upstream = f"unix//run/{{app_name}}/{process_name}.sock"
                    else:
                        upstream = listen
                else:
                    upstream = "localhost:8000"

                content += _CADDY_HANDLE_PROXY.format(
                    name=process_name, path=path, upstream=upstream, extra_directives=""
                )

        content += "}\n"

        return content


TIMER_TEMPLATE = """# Timer for {name} service
# Learn more: https://www.freedesktop.org/software/systemd/man/systemd.timer.html

[Unit]
Description={app_name} {name} timer

[Timer]
{timer_config}
[Install]
WantedBy=timers.target
"""

NEW_SOCKET_TEMPLATE = """# Systemd socket activation for {name}
# Learn more: https://www.freedesktop.org/software/systemd/man/systemd.socket.html

[Unit]
Description={{app_name}} {name} socket
PartOf={name}.service

[Socket]
ListenStream=/run/{{app_name}}/{name}.sock
SocketMode=0660
SocketUser={{app_user}}
SocketGroup={{app_user}}

[Install]
WantedBy=sockets.target
"""
