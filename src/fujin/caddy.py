from __future__ import annotations

import json
import urllib.request

import cappa
from rich import print

from fujin.config import Config
from fujin.connection import Connection

DEFAULT_VERSION = "2.10.2"
GH_TAR_FILENAME = "caddy_{version}_linux_amd64.tar.gz"
GH_DOWNL0AD_URL = (
    "https://github.com/caddyserver/caddy/releases/download/v{version}/"
    + GH_TAR_FILENAME
)
GH_RELEASE_LATEST_URL = "https://api.github.com/repos/caddyserver/caddy/releases/latest"


def install(conn: Connection) -> bool:
    result = conn.run(f"command -v caddy", warn=True, hide=True)
    if result.ok:
        return False
    version = get_latest_gh_tag()
    download_url = GH_DOWNL0AD_URL.format(version=version)
    filename = GH_TAR_FILENAME.format(version=version)
    with conn.cd("/tmp"):
        conn.run(f"curl -O -L {download_url}")
        conn.run(f"tar -xzvf {filename}")
        conn.run("sudo mv caddy /usr/bin/", pty=True)
        conn.run(f"rm {filename}")
        conn.run("rm LICENSE && rm README.md")
    conn.run("sudo groupadd --force --system caddy", pty=True)
    conn.run(
        "sudo useradd --system --gid caddy --create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin --comment 'Caddy web server' caddy",
        pty=True,
        warn=True,
    )

    # Setup directories
    conn.run("sudo mkdir -p /etc/caddy/conf.d", pty=True)
    conn.run("sudo chown -R caddy:caddy /etc/caddy", pty=True)

    # Create main Caddyfile
    main_caddyfile = "import conf.d/*.caddy\n"
    conn.run(
        f"echo '{main_caddyfile}' | sudo tee /etc/caddy/Caddyfile",
        hide="out",
        pty=True,
    )

    conn.run(
        f"echo '{systemd_service}' | sudo tee /etc/systemd/system/caddy.service",
        hide="out",
        pty=True,
    )
    conn.run("sudo systemctl daemon-reload", pty=True)
    conn.run("sudo systemctl enable --now caddy", pty=True)
    return True


def uninstall(conn: Connection):
    conn.run("sudo systemctl stop caddy", pty=True)
    conn.run("sudo systemctl disable caddy", pty=True)
    conn.run("sudo rm /usr/bin/caddy", pty=True)
    conn.run("sudo rm /etc/systemd/system/caddy.service", pty=True)
    conn.run("sudo userdel caddy", pty=True)
    conn.run("sudo rm -rf /etc/caddy", pty=True)


def setup(conn: Connection, config: Config):
    rendered_content = config.render_caddyfile()

    remote_path = config.caddy_config_path
    res = conn.run(
        f"echo '{rendered_content}' | sudo tee {remote_path}",
        hide="out",
        pty=True,
        warn=True,
    )
    conn.run("sudo systemctl reload caddy", pty=True, warn=True)
    return res.ok


def teardown(conn: Connection, config: Config):
    remote_path = config.caddy_config_path
    conn.run(f"sudo rm {remote_path}", warn=True, pty=True)
    conn.run("sudo systemctl reload caddy", pty=True)


def get_latest_gh_tag() -> str:
    with urllib.request.urlopen(GH_RELEASE_LATEST_URL) as response:
        if response.status != 200:
            return DEFAULT_VERSION
        try:
            data = json.loads(response.read().decode())
            return data["tag_name"][1:]
        except (KeyError, json.JSONDecodeError):
            return DEFAULT_VERSION


systemd_service = """
# caddy.service
#
# For using Caddy with a config file.
#
# See https://caddyserver.com/docs/install for instructions.

[Unit]
Description=Caddy
Documentation=https://caddyserver.com/docs/
After=network.target network-online.target
Requires=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
LimitNPROC=512
PrivateTmp=true
ProtectSystem=full
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
"""
