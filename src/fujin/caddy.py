from __future__ import annotations

import json
import logging
import urllib.request

from fujin.config import Config
from fujin.connection import SSH2Connection as Connection

logger = logging.getLogger(__name__)

DEFAULT_VERSION = "2.10.2"
GH_TAR_FILENAME = "caddy_{version}_linux_amd64.tar.gz"
GH_DOWNL0AD_URL = (
    "https://github.com/caddyserver/caddy/releases/download/v{version}/"
    + GH_TAR_FILENAME
)
GH_RELEASE_LATEST_URL = "https://api.github.com/repos/caddyserver/caddy/releases/latest"


def install(conn: Connection) -> bool:
    logger.debug("Checking if Caddy is already installed")
    _, result_ok = conn.run(f"command -v caddy", warn=True, hide=True)
    if result_ok:
        logger.debug("Caddy is already installed")
        return False
    version = get_latest_gh_tag()
    logger.info(f"Installing Caddy version {version}")
    download_url = GH_DOWNL0AD_URL.format(version=version)
    filename = GH_TAR_FILENAME.format(version=version)
    with conn.cd("/tmp"):
        commands = [
            f"curl -O -L {download_url}",
            f"tar -xzvf {filename}",
            "sudo mv caddy /usr/bin/",
            f"rm {filename}",
            "rm LICENSE README.md",
        ]
        conn.run(" && ".join(commands), pty=True)
    conn.run("sudo groupadd --force --system caddy", pty=True)
    conn.run(
        "sudo useradd --system --gid caddy --create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin --comment 'Caddy web server' caddy",
        pty=True,
        warn=True,
    )
    conn.run(
        "sudo mkdir -p /etc/caddy/conf.d && sudo chown -R caddy:caddy /etc/caddy",
        pty=True,
    )
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
    conn.run(
        "sudo systemctl daemon-reload && sudo systemctl enable --now caddy", pty=True
    )
    return True


def uninstall(conn: Connection):
    logger.info("Uninstalling Caddy")
    commands = [
        "sudo systemctl stop caddy",
        "sudo systemctl disable caddy",
        "sudo rm /usr/bin/caddy",
        "sudo rm /etc/systemd/system/caddy.service",
        "sudo userdel caddy",
        "sudo rm -rf /etc/caddy",
    ]
    conn.run(" && ".join(commands), pty=True)


def setup(conn: Connection, config: Config):
    logger.debug("Setting up Caddy configuration")
    rendered_content = config.render_caddyfile()

    remote_path = config.caddy_config_path
    commands = [
        f"echo '{rendered_content}' | sudo tee {remote_path}",
        "sudo systemctl reload caddy",
    ]
    _, res_ok = conn.run(" && ".join(commands), hide="out", pty=True, warn=True)
    return res_ok


def teardown(conn: Connection, config: Config):
    logger.debug("Tearing down Caddy configuration")
    remote_path = config.caddy_config_path
    conn.run(f"sudo rm {remote_path}", warn=True, pty=True)
    conn.run("sudo systemctl reload caddy", pty=True)


def get_latest_gh_tag() -> str:
    logger.debug("Fetching latest Caddy version from GitHub")
    with urllib.request.urlopen(GH_RELEASE_LATEST_URL) as response:
        if response.status != 200:
            logger.warning(
                f"Failed to fetch latest Caddy version, using default: {DEFAULT_VERSION}"
            )
            return DEFAULT_VERSION
        try:
            data = json.loads(response.read().decode())
            return data["tag_name"][1:]
        except (KeyError, json.JSONDecodeError):
            logger.warning(
                f"Failed to parse GitHub response, using default: {DEFAULT_VERSION}"
            )
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
