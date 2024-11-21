from __future__ import annotations

from pathlib import Path

import msgspec

from fujin.config import Config
from fujin.connection import Connection


class WebProxy(msgspec.Struct):
    conn: Connection
    domain_name: str
    app_name: str
    upstream: str
    statics: dict[str, str]
    local_config_dir: Path
    certbot_email: str | None = None

    @property
    def config_file(self) -> Path:
        return self.local_config_dir / f"{self.app_name}.conf"

    @classmethod
    def create(cls, config: Config, conn: Connection) -> WebProxy:
        return cls(
            conn=conn,
            domain_name=config.host.domain_name,
            upstream=config.webserver.upstream,
            app_name=config.app_name,
            local_config_dir=config.local_config_dir,
            statics=config.webserver.statics,
            certbot_email=config.webserver.certbot_email,
        )

    def run_pty(self, *args, **kwargs):
        return self.run_pty(*args, **kwargs, pty=True)

    def install(self):
        self.run_pty(
            "sudo apt install -y nginx libpq-dev python3-dev python3-certbot-nginx"
        )

    def uninstall(self):
        self.stop()
        self.run_pty("sudo apt remove -y nginx")
        self.run_pty(f"sudo rm /etc/nginx/sites-available/{self.app_name}.conf")
        self.run_pty(f"sudo rm /etc/nginx/sites-enabled/{self.app_name}.conf")
        self.run_pty("sudo systemctl disable certbot.timer")
        self.run_pty("sudo apt remove -y python3-certbot-nginx")

    def setup(self):
        conf = (
            self.config_file.read_text()
            if self.config_file.exists()
            else self._get_config()
        )
        self.run_pty(
            f"sudo echo '{conf}' | sudo tee /etc/nginx/sites-available/{self.app_name}.conf",
            hide="out",
        )
        self.run_pty(
            f"sudo ln -sf /etc/nginx/sites-available/{self.app_name}.conf /etc/nginx/sites-enabled/{self.app_name}.conf",
        )
        if self.certbot_email:
            cert_path = f"/etc/letsencrypt/live/{self.domain_name}/fullchain.pem"
            cert_exists = self.run_pty(f"sudo test -f {cert_path}", warn=True).ok

            if not cert_exists:
                self.run_pty(
                    f"sudo certbot --nginx -d {self.domain_name} --non-interactive --agree-tos --email {self.certbot_email} --redirect"
                )
                self.config_file.parent.mkdir(exist_ok=True)
                self.conn.get(
                    f"/etc/nginx/sites-available/{self.app_name}.conf",
                    str(self.config_file),
                )
                self.run_pty("sudo systemctl enable certbot.timer")
                self.run_pty("sudo systemctl start certbot.timer")
        self.restart()

    def teardown(self):
        self.run_pty(f"sudo rm /etc/nginx/sites-available/{self.app_name}.conf")
        self.run_pty(f"sudo rm /etc/nginx/sites-enabled/{self.app_name}.conf")
        self.run_pty("sudo systemctl restart nginx")

    def start(self) -> None:
        self.run_pty("sudo systemctl start nginx")

    def stop(self) -> None:
        self.run_pty("sudo systemctl stop nginx")

    def status(self) -> None:
        self.run_pty("sudo systemctl status nginx", warn=True)

    def restart(self) -> None:
        self.run_pty("sudo systemctl restart nginx")

    def logs(self) -> None:
        self.run_pty(f"sudo journalctl -u nginx -f", warn=True)

    def export_config(self) -> None:
        self.config_file.write_text(self._get_config())

    def _get_config(self) -> str:
        static_locations = ""
        for path, directory in self.statics.items():
            static_locations += f"""
    location {path} {{
        alias {directory};
    }}
        """

        return f"""
server {{
    listen 80;
    server_name {self.domain_name};

    {static_locations}

    location / {{
        proxy_pass {self.upstream};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""
