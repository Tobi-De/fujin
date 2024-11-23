from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

import gevent

from fujin.config import Config
from fujin.connection import Connection


@dataclass(frozen=True, slots=True)
class ProcessManager:
    conn: Connection
    app_name: str
    processes: dict[str, str]
    app_dir: str
    user: str
    is_using_unix_socket: bool
    local_config_dir: Path

    @classmethod
    def create(cls, config: Config, conn: Connection):
        return cls(
            processes=config.processes,
            app_name=config.app_name,
            app_dir=config.host.get_app_dir(config.app_name),
            conn=conn,
            user=config.host.user,
            is_using_unix_socket="unix" in config.webserver.upstream
            and config.webserver.type != "fujin.proxies.dummy",
            local_config_dir=config.local_config_dir,
        )

    @property
    def service_names(self) -> list[str]:
        services = [self.get_service_name(name) for name in self.processes]
        if self.is_using_unix_socket:
            services.append(f"{self.app_name}.socket")
        return services

    def get_service_name(self, process_name: str):
        if process_name == "web":
            return f"{self.app_name}.service"
        return f"{self.app_name}-{process_name}.service"

    def run_pty(self, *args, **kwargs):
        return self.conn.run(*args, **kwargs, pty=True)

    def install_services(self) -> None:
        conf_files = self.get_configuration_files()
        for filename, content in conf_files:
            self.run_pty(
                f"echo '{content}' | sudo tee /etc/systemd/system/{filename}",
                hide="out",
            )

        threads = []
        for name in self.processes:
            if name == "web" and self.is_using_unix_socket:
                threads.append(
                    gevent.spawn(
                        self.run_pty,
                        f"sudo systemctl enable --now {self.app_name}.socket",
                    )
                )
            else:
                threads.append(
                    gevent.spawn(
                        self.run_pty,
                        f"sudo systemctl enable {self.get_service_name(name)}",
                    )
                )
        gevent.joinall(threads)

    def get_configuration_files(
        self, ignore_local: bool = False
    ) -> list[tuple[str, str]]:
        templates_folder = (
            Path(importlib.util.find_spec("fujin").origin).parent / "templates"
        )
        web_service_content = (templates_folder / "web.service").read_text()
        web_socket_content = (templates_folder / "web.socket").read_text()
        simple_service_content = (templates_folder / "simple.service").read_text()
        if not self.is_using_unix_socket:
            web_service_content = web_service_content.replace(
                "Requires={app_name}.socket\n", ""
            )

        context = {
            "app_name": self.app_name,
            "user": self.user,
            "app_dir": self.app_dir,
        }

        files = []
        for name, command in self.processes.items():
            template = web_service_content if name == "web" else simple_service_content
            name = self.get_service_name(name)
            local_config = self.local_config_dir / name
            body = (
                local_config.read_text()
                if local_config.exists() and not ignore_local
                else template.format(**context, command=command)
            )
            files.append((name, body))
        # if using unix then we are sure a web process was defined and the proxy is not dummy
        if self.is_using_unix_socket:
            name = f"{self.app_name}.socket"
            local_config = self.local_config_dir / name
            body = (
                local_config.read_text()
                if local_config.exists() and not ignore_local
                else web_socket_content.format(**context)
            )
            files.append((name, body))
        return files

    def uninstall_services(self) -> None:
        self.stop_services()
        threads = [
            gevent.spawn(self.run_pty, f"sudo systemctl disable {name}", warn=True)
            for name in self.service_names
        ]
        gevent.joinall(threads)
        files_to_delete = [f"/etc/systemd/system/{name}" for name in self.service_names]
        self.run_pty(f"sudo rm {' '.join(files_to_delete)}", warn=True)

    def start_services(self, *names) -> None:
        names = names or self.service_names
        threads = [
            gevent.spawn(self.run_pty, f"sudo systemctl start {name}")
            for name in names
            if name in self.service_names
        ]
        gevent.joinall(threads)

    def restart_services(self, *names) -> None:
        names = names or self.service_names
        threads = [
            gevent.spawn(self.run_pty, f"sudo systemctl restart {name}")
            for name in names
            if name in self.service_names
        ]
        gevent.joinall(threads)

    def stop_services(self, *names) -> None:
        names = names or self.service_names
        threads = [
            gevent.spawn(self.run_pty, f"sudo systemctl stop {name}")
            for name in names
            if name in self.service_names
        ]
        gevent.joinall(threads)

    def is_enabled(self, *names) -> dict[str, bool]:
        names = names or self.service_names
        threads = {
            name: gevent.spawn(
                self.run_pty, f"sudo systemctl is-enabled {name}", warn=True, hide=True
            )
            for name in names
        }
        gevent.joinall(threads.values())
        return {
            name: thread.value.stdout.strip() == "enabled"
            for name, thread in threads.items()
        }

    def is_active(self, *names) -> dict[str, bool]:
        names = names or self.service_names
        threads = {
            name: gevent.spawn(
                self.run_pty, f"sudo systemctl is-active {name}", warn=True, hide=True
            )
            for name in names
        }
        gevent.joinall(threads.values())
        return {
            name: thread.value.stdout.strip() == "active"
            for name, thread in threads.items()
        }

    def service_logs(self, name: str, follow: bool = False):
        # TODO: add more options here
        self.run_pty(f"sudo journalctl -u {name} {'-f' if follow else ''}", warn=True)

    def reload_configuration(self) -> None:
        self.run_pty(f"sudo systemctl daemon-reload")
