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


def get_install_commands(version: str | None = None) -> list[str]:
    if version is None:
        version = get_latest_gh_tag()

    download_url = GH_DOWNL0AD_URL.format(version=version)
    filename = GH_TAR_FILENAME.format(version=version)

    commands = []
    # Download and install binary
    commands.append(f"cd /tmp && curl -O -L {download_url}")
    commands.append(f"cd /tmp && tar -xzvf {filename}")
    commands.append("sudo mv /tmp/caddy /usr/bin/")
    commands.append(f"rm /tmp/{filename} /tmp/LICENSE /tmp/README.md")

    # User and Group
    commands.append("sudo groupadd --force --system caddy")
    commands.append(
        "sudo useradd --system --gid caddy --create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin --comment 'Caddy web server' caddy || true"
    )

    # Config dirs
    commands.append("sudo mkdir -p /etc/caddy/conf.d")
    commands.append("sudo chown -R caddy:caddy /etc/caddy")

    # Default Caddyfile
    main_caddyfile = "import conf.d/*.caddy\n"
    commands.append(f"echo '{main_caddyfile}' | sudo tee /etc/caddy/Caddyfile")

    # Systemd service
    commands.append(
        f"echo '{systemd_service}' | sudo tee /etc/systemd/system/caddy.service"
    )

    # Enable and start
    commands.append("sudo systemctl daemon-reload")
    commands.append("sudo systemctl enable --now caddy")

    return commands


def get_uninstall_commands() -> list[str]:
    return [
        "sudo systemctl stop caddy",
        "sudo systemctl disable caddy",
        "sudo rm -f /usr/bin/caddy",
        "sudo rm -f /etc/systemd/system/caddy.service",
        "sudo userdel caddy",
        "sudo rm -rf /etc/caddy",
    ]


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
