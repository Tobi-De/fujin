import hashlib
from unittest.mock import MagicMock, patch

from inline_snapshot import snapshot
from fujin.commands.deploy import Deploy
from fujin.config import InstallationMode


def test_deploy_binary_mode(mock_config, mock_connection, get_commands):
    mock_config.installation_mode = InstallationMode.BINARY
    mock_config.app_name = "myapp"

    # Mock subprocess to avoid actual build
    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            "mkdir -p /home/testuser/.local/share/fujin/myapp",
            "echo 'FOO=bar' > /home/testuser/.local/share/fujin/myapp/.env",
            "mkdir -p /home/testuser/.local/share/fujin/myapp/v0.1.0",
            """\
echo 'set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export PATH="/home/testuser/.local/share/fujin/myapp:$PATH"' > /home/testuser/.local/share/fujin/myapp/.appenv\
""",
            "rm /home/testuser/.local/share/fujin/myapp/myapp",
            "ln -s /home/testuser/.local/share/fujin/myapp/v0.1.0/testapp-0.1.0.whl /home/testuser/.local/share/fujin/myapp/myapp",
            "head -n 1 .versions",
            "sed -i '1i 0.1.0' .versions",
            """\
echo '# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
# Inspiration was taken from here https://docs.gunicorn.org/en/stable/deploy.html#systemd
[Unit]
Description=myapp

After=network.target

[Service]
#Type=notify
#NotifyAccess=main
User=testuser
Group=testuser
RuntimeDirectory=myapp
WorkingDirectory=/home/testuser/.local/share/fujin/myapp
ExecStart=/home/testuser/.local/share/fujin/myapp/run web
EnvironmentFile=/home/testuser/.local/share/fujin/myapp/.env
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true
# if your app does not need administrative capabilities, let systemd know
ProtectSystem=strict

[Install]
WantedBy=multi-user.target' | sudo tee /etc/systemd/system/myapp.service\
""",
            """\
echo '# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
[Unit]
Description=myapp-worker@

[Service]
User=testuser
Group=testuser
WorkingDirectory=/home/testuser/.local/share/fujin/myapp
ExecStart=/home/testuser/.local/share/fujin/myapp/run worker
EnvironmentFile=/home/testuser/.local/share/fujin/myapp/.env
Restart=always

[Install]
WantedBy=multi-user.target' | sudo tee /etc/systemd/system/myapp-worker@.service\
""",
            "sudo systemctl daemon-reload",
            "sudo systemctl enable --now myapp.service myapp-worker@1.service myapp-worker@2.service",
            "systemctl list-units --full --all --plain --no-legend 'myapp*'",
            "ls /etc/systemd/system/myapp*",
            "ls /etc/systemd/system/multi-user.target.wants/myapp*",
            "sudo systemctl restart myapp.service myapp-worker@1.service myapp-worker@2.service",
            """\
echo 'example.com {
	

	reverse_proxy localhost:8000
}' | sudo tee /etc/caddy/conf.d/myapp.caddy\
""",
            "sudo systemctl reload caddy",
            "sed -n '6,$p' .versions",
        ]
    )


def test_deploy_python_rebuild_venv(
    mock_config, mock_connection, tmp_path, get_commands
):
    mock_config.installation_mode = InstallationMode.PY_PACKAGE
    mock_config.requirements = "requirements.txt"

    # Create dummy requirements file
    req_path = tmp_path / "requirements.txt"
    req_path.write_text("django")
    mock_config.requirements = str(req_path)

    # Mock remote state: No previous version, so hash check fails/skipped
    def run_side_effect(cmd, **kwargs):
        mock_res = MagicMock()
        mock_res.ok = True
        mock_res.stdout = ""
        if "head -n 1 .versions" in cmd:
            mock_res.stdout = ""
        return mock_res

    mock_connection.run.side_effect = run_side_effect

    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            "mkdir -p /home/testuser/.local/share/fujin/testapp",
            "echo 'FOO=bar' > /home/testuser/.local/share/fujin/testapp/.env",
            "mkdir -p /home/testuser/.local/share/fujin/testapp/v0.1.0",
            """\
echo 'set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export UV_COMPILE_BYTECODE=1
export UV_PYTHON=python3.12
export PATH=".venv/bin:$PATH"' > /home/testuser/.local/share/fujin/testapp/.appenv\
""",
            "head -n 1 .versions",
            "sudo rm -rf .venv",
            "uv python install 3.12",
            "uv venv",
            "uv pip install -r /home/testuser/.local/share/fujin/testapp/v0.1.0/requirements.txt",
            "uv pip install /home/testuser/.local/share/fujin/testapp/v0.1.0/testapp-0.1.0.whl",
            "head -n 1 .versions",
            "echo '0.1.0' > .versions",
            """\
echo '# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
# Inspiration was taken from here https://docs.gunicorn.org/en/stable/deploy.html#systemd
[Unit]
Description=testapp

After=network.target

[Service]
#Type=notify
#NotifyAccess=main
User=testuser
Group=testuser
RuntimeDirectory=testapp
WorkingDirectory=/home/testuser/.local/share/fujin/testapp
ExecStart=/home/testuser/.local/share/fujin/testapp/run web
EnvironmentFile=/home/testuser/.local/share/fujin/testapp/.env
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true
# if your app does not need administrative capabilities, let systemd know
ProtectSystem=strict

[Install]
WantedBy=multi-user.target' | sudo tee /etc/systemd/system/testapp.service\
""",
            """\
echo '# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
[Unit]
Description=testapp-worker@

[Service]
User=testuser
Group=testuser
WorkingDirectory=/home/testuser/.local/share/fujin/testapp
ExecStart=/home/testuser/.local/share/fujin/testapp/run worker
EnvironmentFile=/home/testuser/.local/share/fujin/testapp/.env
Restart=always

[Install]
WantedBy=multi-user.target' | sudo tee /etc/systemd/system/testapp-worker@.service\
""",
            "sudo systemctl daemon-reload",
            "sudo systemctl enable --now testapp.service testapp-worker@1.service testapp-worker@2.service",
            "systemctl list-units --full --all --plain --no-legend 'testapp*'",
            "ls /etc/systemd/system/testapp*",
            "ls /etc/systemd/system/multi-user.target.wants/testapp*",
            "sudo systemctl restart testapp.service testapp-worker@1.service testapp-worker@2.service",
            """\
echo 'example.com {
	

	reverse_proxy localhost:8000
}' | sudo tee /etc/caddy/conf.d/testapp.caddy\
""",
            "sudo systemctl reload caddy",
            "sed -n '6,$p' .versions",
        ]
    )


def test_deploy_python_reuse_venv(mock_config, mock_connection, tmp_path, get_commands):
    mock_config.installation_mode = InstallationMode.PY_PACKAGE
    mock_config.requirements = "requirements.txt"

    # Create dummy requirements file
    req_path = tmp_path / "requirements.txt"
    content = b"django"
    req_path.write_bytes(content)
    mock_config.requirements = str(req_path)
    local_hash = hashlib.md5(content).hexdigest()

    # Mock remote state: Previous version exists, hashes match
    def run_side_effect(cmd, **kwargs):
        mock_res = MagicMock()
        mock_res.ok = True
        mock_res.stdout = ""
        if "head -n 1 .versions" in cmd:
            mock_res.stdout = "0.0.1"
        if "md5sum" in cmd:
            mock_res.stdout = f"{local_hash}  requirements.txt"
        return mock_res

    mock_connection.run.side_effect = run_side_effect

    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            "mkdir -p /home/testuser/.local/share/fujin/testapp",
            "echo 'FOO=bar' > /home/testuser/.local/share/fujin/testapp/.env",
            "mkdir -p /home/testuser/.local/share/fujin/testapp/v0.1.0",
            """\
echo 'set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export UV_COMPILE_BYTECODE=1
export UV_PYTHON=python3.12
export PATH=".venv/bin:$PATH"' > /home/testuser/.local/share/fujin/testapp/.appenv\
""",
            "head -n 1 .versions",
            "md5sum /home/testuser/.local/share/fujin/testapp/v0.0.1/requirements.txt",
            "cp /home/testuser/.local/share/fujin/testapp/v0.0.1/requirements.txt /home/testuser/.local/share/fujin/testapp/v0.1.0/requirements.txt",
            "uv pip install /home/testuser/.local/share/fujin/testapp/v0.1.0/testapp-0.1.0.whl",
            "head -n 1 .versions",
            "sed -i '1i 0.1.0' .versions",
            """\
echo '# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
# Inspiration was taken from here https://docs.gunicorn.org/en/stable/deploy.html#systemd
[Unit]
Description=testapp

After=network.target

[Service]
#Type=notify
#NotifyAccess=main
User=testuser
Group=testuser
RuntimeDirectory=testapp
WorkingDirectory=/home/testuser/.local/share/fujin/testapp
ExecStart=/home/testuser/.local/share/fujin/testapp/run web
EnvironmentFile=/home/testuser/.local/share/fujin/testapp/.env
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true
# if your app does not need administrative capabilities, let systemd know
ProtectSystem=strict

[Install]
WantedBy=multi-user.target' | sudo tee /etc/systemd/system/testapp.service\
""",
            """\
echo '# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
[Unit]
Description=testapp-worker@

[Service]
User=testuser
Group=testuser
WorkingDirectory=/home/testuser/.local/share/fujin/testapp
ExecStart=/home/testuser/.local/share/fujin/testapp/run worker
EnvironmentFile=/home/testuser/.local/share/fujin/testapp/.env
Restart=always

[Install]
WantedBy=multi-user.target' | sudo tee /etc/systemd/system/testapp-worker@.service\
""",
            "sudo systemctl daemon-reload",
            "sudo systemctl enable --now testapp.service testapp-worker@1.service testapp-worker@2.service",
            "systemctl list-units --full --all --plain --no-legend 'testapp*'",
            "ls /etc/systemd/system/testapp*",
            "ls /etc/systemd/system/multi-user.target.wants/testapp*",
            "sudo systemctl restart testapp.service testapp-worker@1.service testapp-worker@2.service",
            """\
echo 'example.com {
	

	reverse_proxy localhost:8000
}' | sudo tee /etc/caddy/conf.d/testapp.caddy\
""",
            "sudo systemctl reload caddy",
            "sed -n '6,$p' .versions",
        ]
    )


def test_deploy_version_update(mock_config, mock_connection, get_commands):
    # Mock remote state: .versions file exists
    def run_side_effect(cmd, **kwargs):
        mock_res = MagicMock()
        mock_res.ok = True
        mock_res.stdout = ""
        if "head -n 1 .versions" in cmd:
            mock_res.stdout = "0.0.1"  # Different from current version
        return mock_res

    mock_connection.run.side_effect = run_side_effect

    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            "mkdir -p /home/testuser/.local/share/fujin/testapp",
            "echo 'FOO=bar' > /home/testuser/.local/share/fujin/testapp/.env",
            "mkdir -p /home/testuser/.local/share/fujin/testapp/v0.1.0",
            """\
echo 'set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export UV_COMPILE_BYTECODE=1
export UV_PYTHON=python3.12
export PATH=".venv/bin:$PATH"' > /home/testuser/.local/share/fujin/testapp/.appenv\
""",
            "sudo rm -rf .venv",
            "uv python install 3.12",
            "uv venv",
            "uv pip install /home/testuser/.local/share/fujin/testapp/v0.1.0/testapp-0.1.0.whl",
            "head -n 1 .versions",
            "sed -i '1i 0.1.0' .versions",
            """\
echo '# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
# Inspiration was taken from here https://docs.gunicorn.org/en/stable/deploy.html#systemd
[Unit]
Description=testapp

After=network.target

[Service]
#Type=notify
#NotifyAccess=main
User=testuser
Group=testuser
RuntimeDirectory=testapp
WorkingDirectory=/home/testuser/.local/share/fujin/testapp
ExecStart=/home/testuser/.local/share/fujin/testapp/run web
EnvironmentFile=/home/testuser/.local/share/fujin/testapp/.env
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true
# if your app does not need administrative capabilities, let systemd know
ProtectSystem=strict

[Install]
WantedBy=multi-user.target' | sudo tee /etc/systemd/system/testapp.service\
""",
            """\
echo '# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
[Unit]
Description=testapp-worker@

[Service]
User=testuser
Group=testuser
WorkingDirectory=/home/testuser/.local/share/fujin/testapp
ExecStart=/home/testuser/.local/share/fujin/testapp/run worker
EnvironmentFile=/home/testuser/.local/share/fujin/testapp/.env
Restart=always

[Install]
WantedBy=multi-user.target' | sudo tee /etc/systemd/system/testapp-worker@.service\
""",
            "sudo systemctl daemon-reload",
            "sudo systemctl enable --now testapp.service testapp-worker@1.service testapp-worker@2.service",
            "systemctl list-units --full --all --plain --no-legend 'testapp*'",
            "ls /etc/systemd/system/testapp*",
            "ls /etc/systemd/system/multi-user.target.wants/testapp*",
            "sudo systemctl restart testapp.service testapp-worker@1.service testapp-worker@2.service",
            """\
echo 'example.com {
	

	reverse_proxy localhost:8000
}' | sudo tee /etc/caddy/conf.d/testapp.caddy\
""",
            "sudo systemctl reload caddy",
            "sed -n '6,$p' .versions",
        ]
    )


def test_deploy_pruning(mock_config, mock_connection, get_commands):
    mock_config.versions_to_keep = 2

    # Mock remote state: return list of versions to prune
    def run_side_effect(cmd, **kwargs):
        mock_res = MagicMock()
        mock_res.ok = True
        mock_res.stdout = ""
        if "sed -n" in cmd:
            # Simulate 3 versions existing, keeping 2, so 1 to prune
            mock_res.stdout = "0.0.1"
        return mock_res

    mock_connection.run.side_effect = run_side_effect

    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            "mkdir -p /home/testuser/.local/share/fujin/testapp",
            "echo 'FOO=bar' > /home/testuser/.local/share/fujin/testapp/.env",
            "mkdir -p /home/testuser/.local/share/fujin/testapp/v0.1.0",
            """\
echo 'set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export UV_COMPILE_BYTECODE=1
export UV_PYTHON=python3.12
export PATH=".venv/bin:$PATH"' > /home/testuser/.local/share/fujin/testapp/.appenv\
""",
            "sudo rm -rf .venv",
            "uv python install 3.12",
            "uv venv",
            "uv pip install /home/testuser/.local/share/fujin/testapp/v0.1.0/testapp-0.1.0.whl",
            "head -n 1 .versions",
            "echo '0.1.0' > .versions",
            """\
echo '# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
# Inspiration was taken from here https://docs.gunicorn.org/en/stable/deploy.html#systemd
[Unit]
Description=testapp

After=network.target

[Service]
#Type=notify
#NotifyAccess=main
User=testuser
Group=testuser
RuntimeDirectory=testapp
WorkingDirectory=/home/testuser/.local/share/fujin/testapp
ExecStart=/home/testuser/.local/share/fujin/testapp/run web
EnvironmentFile=/home/testuser/.local/share/fujin/testapp/.env
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true
# if your app does not need administrative capabilities, let systemd know
ProtectSystem=strict

[Install]
WantedBy=multi-user.target' | sudo tee /etc/systemd/system/testapp.service\
""",
            """\
echo '# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
[Unit]
Description=testapp-worker@

[Service]
User=testuser
Group=testuser
WorkingDirectory=/home/testuser/.local/share/fujin/testapp
ExecStart=/home/testuser/.local/share/fujin/testapp/run worker
EnvironmentFile=/home/testuser/.local/share/fujin/testapp/.env
Restart=always

[Install]
WantedBy=multi-user.target' | sudo tee /etc/systemd/system/testapp-worker@.service\
""",
            "sudo systemctl daemon-reload",
            "sudo systemctl enable --now testapp.service testapp-worker@1.service testapp-worker@2.service",
            "systemctl list-units --full --all --plain --no-legend 'testapp*'",
            "ls /etc/systemd/system/testapp*",
            "ls /etc/systemd/system/multi-user.target.wants/testapp*",
            "sudo systemctl restart testapp.service testapp-worker@1.service testapp-worker@2.service",
            """\
echo 'example.com {
	

	reverse_proxy localhost:8000
}' | sudo tee /etc/caddy/conf.d/testapp.caddy\
""",
            "sudo systemctl reload caddy",
            "sed -n '3,$p' .versions",
            "rm -r /home/testuser/.local/share/fujin/testapp/v0.0.1",
            "sed -i '3,$d' .versions",
        ]
    )
