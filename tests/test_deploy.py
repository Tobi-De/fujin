import os
from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest
from contextlib import contextmanager

from inline_snapshot import snapshot
import time


from fujin.commands.deploy import Deploy
from fujin.config import InstallationMode
from tests.script_runner import script_runner  # noqa: F401


@pytest.fixture
def setup_distfile(tmp_path, mock_config):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    dist_file = dist_dir / f"testapp-{mock_config.version}.whl"
    dist_file.touch()
    mock_config.distfile = str(dist_dir / "testapp-{version}.whl")
    return dist_file


@pytest.fixture
def mock_checksum_match():
    with patch("hashlib.file_digest") as mock_digest:
        mock_digest.return_value.hexdigest.return_value = "checksum123"
        yield


@pytest.fixture(autouse=True)
def mock_time():
    with patch("time.time", return_value=1234567890):
        yield


@pytest.fixture
def capture_bundle(tmp_path):
    @contextmanager
    def _mock_temp_dir():
        yield str(tmp_path)

    with patch("tempfile.TemporaryDirectory", side_effect=_mock_temp_dir):
        yield tmp_path


def test_script_execution_binary(
    mock_config,
    capture_bundle,
    script_runner,
    setup_distfile,
    mock_connection,
):
    mock_config.installation_mode = InstallationMode.BINARY
    mock_config.app_name = "myapp"

    def run_side_effect(command, **kwargs):
        if "sha256sum" in command:
            return "checksum123\n", True
        return "", True

    mock_connection.run.side_effect = run_side_effect

    with patch("hashlib.file_digest") as mock_digest:
        mock_digest.return_value.hexdigest.return_value = "checksum123"
        with patch("subprocess.run"):
            deploy = Deploy()
            deploy()

    bundle_dir = capture_bundle / "myapp-bundle"
    install_script = (bundle_dir / "install.sh").read_text()
    uninstall_script = (bundle_dir / "uninstall.sh").read_text()

    (script_runner.root / "home/testuser/.local/share/fujin/myapp/.versions").mkdir(
        parents=True, exist_ok=True
    )

    # --- Run Install ---
    result = script_runner.run(install_script, cwd=bundle_dir)
    result.assert_success()

    # Check file system state
    app_dir = result.root / "home/testuser/.local/share/fujin/myapp"
    assert (app_dir / ".env").exists()
    assert (app_dir / ".appenv").exists()
    assert (app_dir / "myapp").exists()
    assert (app_dir / ".version").read_text().strip() == "0.1.0"

    # Check systemd files
    systemd_dir = result.root / "etc/systemd/system"
    assert (systemd_dir / "myapp.service").exists()
    assert (systemd_dir / "myapp-worker@.service").exists()

    # Check caddy config
    caddy_dir = result.root / "etc/caddy/conf.d"
    assert (caddy_dir / "myapp.caddy").exists()

    # --- Run Uninstall ---
    result_uninstall = script_runner.run(uninstall_script, cwd=bundle_dir)
    result_uninstall.assert_success()

    # Check systemd files removed
    assert not (systemd_dir / "myapp.service").exists()
    assert not (systemd_dir / "myapp-worker@.service").exists()

    # Check caddy config removed
    assert not (caddy_dir / "myapp.caddy").exists()


def test_script_execution_python(
    mock_config,
    capture_bundle,
    script_runner,
    setup_distfile,
    mock_connection,
    tmp_path,
):
    mock_config.installation_mode = InstallationMode.PY_PACKAGE
    mock_config.requirements = "requirements.txt"

    req_path = tmp_path / "requirements.txt"
    req_path.write_text("django")
    mock_config.requirements = str(req_path)

    def run_side_effect(command, **kwargs):
        if "sha256sum" in command:
            return "checksum123\n", True
        return "", True

    mock_connection.run.side_effect = run_side_effect

    with patch("hashlib.file_digest") as mock_digest:
        mock_digest.return_value.hexdigest.return_value = "checksum123"
        with patch("subprocess.run"):
            deploy = Deploy()
            deploy()

    bundle_dir = capture_bundle / "testapp-bundle"
    install_script = (bundle_dir / "install.sh").read_text()

    (script_runner.root / "home/testuser/.local/share/fujin/testapp/.versions").mkdir(
        parents=True, exist_ok=True
    )

    result = script_runner.run(install_script, cwd=bundle_dir)
    result.assert_success()

    app_dir = result.root / "home/testuser/.local/share/fujin/testapp"
    assert (app_dir / ".venv").exists()
    assert "uv pip install -r" in result.get_log("uv")


def test_script_execution_pruning(
    mock_config,
    capture_bundle,
    script_runner,
    setup_distfile,
    mock_connection,
):
    mock_config.versions_to_keep = 2

    def run_side_effect(command, **kwargs):
        if "sha256sum" in command:
            return "checksum123\n", True
        return "", True

    mock_connection.run.side_effect = run_side_effect

    with patch("hashlib.file_digest") as mock_digest:
        mock_digest.return_value.hexdigest.return_value = "checksum123"
        with patch("subprocess.run"):
            deploy = Deploy()
            deploy()

    bundle_dir = capture_bundle / "testapp-bundle"
    install_script = (bundle_dir / "install.sh").read_text()

    # Setup old versions
    versions_dir = (
        script_runner.root / "home/testuser/.local/share/fujin/testapp/.versions"
    )
    versions_dir.mkdir(parents=True, exist_ok=True)
    (versions_dir / "v1").touch()
    (versions_dir / "v2").touch()
    (versions_dir / "v3").touch()
    (versions_dir / "v4").touch()

    now = time.time()
    os.utime(versions_dir / "v1", (now - 400, now - 400))
    os.utime(versions_dir / "v2", (now - 300, now - 300))
    os.utime(versions_dir / "v3", (now - 200, now - 200))
    os.utime(versions_dir / "v4", (now - 100, now - 100))

    result = script_runner.run(install_script, cwd=bundle_dir)
    result.assert_success()

    remaining = sorted([p.name for p in versions_dir.iterdir()])
    assert "v4" in remaining
    assert "v3" in remaining
    assert "v2" not in remaining
    assert "v1" not in remaining


def test_script_execution_update(
    mock_config,
    capture_bundle,
    script_runner,
    setup_distfile,
    mock_connection,
    tmp_path,
):
    mock_config.version = "0.2.0"

    # Create the distfile for 0.2.0 manually since setup_distfile created 0.1.0
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(exist_ok=True)
    (dist_dir / "testapp-0.2.0.whl").touch()

    def run_side_effect(command, **kwargs):
        if "sha256sum" in command:
            return "checksum123\n", True
        return "", True

    mock_connection.run.side_effect = run_side_effect

    with patch("hashlib.file_digest") as mock_digest:
        mock_digest.return_value.hexdigest.return_value = "checksum123"
        with patch("subprocess.run"):
            deploy = Deploy()
            deploy()

    bundle_dir = capture_bundle / "testapp-bundle"
    install_script = (bundle_dir / "install.sh").read_text()

    # Setup existing version
    app_dir = script_runner.root / "home/testuser/.local/share/fujin/testapp"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / ".version").write_text("0.1.0")

    # Create .versions
    (app_dir / ".versions").mkdir(exist_ok=True)

    result = script_runner.run(install_script, cwd=bundle_dir)
    result.assert_success()

    assert (app_dir / ".version").read_text().strip() == "0.2.0"


def test_deploy_binary_commands(
    mock_config,
    mock_connection,
    get_commands,
    setup_distfile,
    mock_checksum_match,
    capture_bundle,
):
    mock_config.installation_mode = InstallationMode.BINARY
    mock_config.app_name = "myapp"

    def run_side_effect(command, **kwargs):
        if "sha256sum" in command:
            return "checksum123\n", True
        return "", True

    mock_connection.run.side_effect = run_side_effect

    # Mock subprocess to avoid actual build
    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    bundle_dir = capture_bundle / "myapp-bundle"
    assert (bundle_dir / "install.sh").read_text() == snapshot(
        """\
#!/usr/bin/env bash
set -e
BUNDLE_DIR=$(pwd)
echo "==> Setting up directories..."
mkdir -p /home/testuser/.local/share/fujin/myapp
mv .env /home/testuser/.local/share/fujin/myapp/.env
echo '==> Installing application...'
cd /home/testuser/.local/share/fujin/myapp || exit 1
trap 'echo "ERROR: install failed at $(date)" >&2; echo "Working dir: $BUNDLE_DIR" >&2; exit 1' ERR
echo 'set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export PATH="/home/testuser/.local/share/fujin/myapp:$PATH"' > /home/testuser/.local/share/fujin/myapp/.appenv
echo '==> Installing binary...'
rm -f /home/testuser/.local/share/fujin/myapp/myapp
cp $BUNDLE_DIR/testapp-0.1.0.whl /home/testuser/.local/share/fujin/myapp/myapp
chmod +x /home/testuser/.local/share/fujin/myapp/myapp
echo '0.1.0' > .version
cd $BUNDLE_DIR
echo "==> Configuring systemd services..."
sudo cp units/* /etc/systemd/system/


APP_NAME="myapp"
read -r -a VALID_UNITS <<< "myapp-worker@.service myapp-worker@1.service myapp-worker@2.service myapp.service"

# Exact match helper
contains_exact() {
    local needle="$1"; shift
    for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
    return 1
}

echo "==> Discovering installed unit files"
mapfile -t INSTALLED_UNITS < <(
    systemctl list-unit-files --type=service --no-legend --no-pager \\
    | awk -v app="$APP_NAME" '$1 ~ "^"app {print $1}'
)

echo "==> Disabling + stopping stale units"
for UNIT in "${INSTALLED_UNITS[@]}"; do
    if ! contains_exact "$UNIT" "${VALID_UNITS[@]}"; then

        # If it's a template file (myapp@.service), only disable it.
        if [[ "$UNIT" == *@.service ]]; then
            echo "→ Disabling template unit: $UNIT"
            sudo systemctl disable "$UNIT" --quiet || true
        else
            echo "→ Stopping + disabling stale unit: $UNIT"
            sudo systemctl stop "$UNIT" --quiet || true
            sudo systemctl disable "$UNIT" --quiet || true
        fi

        sudo systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
    fi
done

echo "==> Removing stale service files"
SEARCH_DIRS=(
    /etc/systemd/system/
    /etc/systemd/system/multi-user.target.wants/
)

for DIR in "${SEARCH_DIRS[@]}"; do
    [[ -d "$DIR" ]] || continue

    while IFS= read -r -d '' FILE; do
        BASENAME=$(basename "$FILE")
        if ! contains_exact "$BASENAME" "${VALID_UNITS[@]}"; then
            echo "→ Removing stale file: $FILE"
            sudo rm -f -- "$FILE"
        fi
    done < <(find "$DIR" -maxdepth 1 -type f -name "${APP_NAME}*" -print0)
done

echo "==> Reloading systemd"
sudo systemctl daemon-reload


echo "==> Restarting services..."
sudo systemctl enable myapp.service myapp-worker@1.service myapp-worker@2.service
sudo systemctl restart myapp.service myapp-worker@1.service myapp-worker@2.service
echo "==> Configuring Caddy..."
sudo mkdir -p $(dirname /etc/caddy/conf.d/myapp.caddy)
if caddy validate --config Caddyfile >/dev/null 2>&1; then
  sudo mv Caddyfile /etc/caddy/conf.d/myapp.caddy
  sudo chown caddy:caddy /etc/caddy/conf.d/myapp.caddy
  sudo systemctl reload caddy
else
  echo 'Caddyfile validation failed, leaving local Caddyfile for inspection' >&2
fi
echo "==> Pruning old versions..."
cd /home/testuser/.local/share/fujin/myapp/.versions
ls -1t | tail -n +6 | xargs -r rm
echo '==> Install script completed successfully.'\
"""
    )
    assert (bundle_dir / "uninstall.sh").read_text() == snapshot(
        """\
#!/usr/bin/env bash
set -e
sudo systemctl disable --now myapp.service myapp-worker@1.service myapp-worker@2.service --quiet || true
APP_NAME="myapp"
UNITS=(myapp.service myapp-worker@.service)
for UNIT in "${UNITS[@]}"; do
  case "$UNIT" in
    */*|*..*) echo "Skipping suspicious unit name: $UNIT" >&2; continue;;
  esac
  if [[ "$UNIT" != ${APP_NAME}* ]]; then
    echo "Refusing to remove non-app unit: $UNIT" >&2
    continue
  fi
  sudo rm -f /etc/systemd/system/"$UNIT"
done
sudo systemctl daemon-reload
sudo systemctl reset-failed
sudo rm -f /etc/caddy/conf.d/myapp.caddy
sudo systemctl reload caddy
echo '==> Uninstall completed.'\
"""
    )
    assert (bundle_dir / "Caddyfile").read_text() == snapshot(
        """\
example.com {
	

	reverse_proxy localhost:8000
}\
"""
    )
    assert (bundle_dir / "units" / "myapp.service").read_text() == snapshot(
        """\
# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
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
WantedBy=multi-user.target\
"""
    )

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mkdir -p /home/testuser/.local/share/fujin/myapp/.versions',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && sha256sum /home/testuser/.local/share/fujin/myapp/.versions/myapp-0.1.0.tar.gz.uploading.1234567890 | awk '{print $1}'",
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mv /home/testuser/.local/share/fujin/myapp/.versions/myapp-0.1.0.tar.gz.uploading.1234567890 /home/testuser/.local/share/fujin/myapp/.versions/myapp-0.1.0.tar.gz',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && mkdir -p /tmp/myapp-0.1.0 && tar --overwrite -xzf /home/testuser/.local/share/fujin/myapp/.versions/myapp-0.1.0.tar.gz -C /tmp/myapp-0.1.0 && cd /tmp/myapp-0.1.0 && chmod +x install.sh && bash ./install.sh || (echo 'install.sh failed' >&2; exit 1) && cd / && rm -rf /tmp/myapp-0.1.0",
        ]
    )


def test_deploy_python_commands(
    mock_config,
    mock_connection,
    get_commands,
    setup_distfile,
    mock_checksum_match,
    tmp_path,
    capture_bundle,
):
    mock_config.installation_mode = InstallationMode.PY_PACKAGE
    mock_config.requirements = "requirements.txt"

    # Create dummy requirements file
    req_path = tmp_path / "requirements.txt"
    req_path.write_text("django")
    mock_config.requirements = str(req_path)

    def run_side_effect(command, **kwargs):
        if "sha256sum" in command:
            return "checksum123\n", True
        return "", True

    mock_connection.run.side_effect = run_side_effect

    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    bundle_dir = capture_bundle / "testapp-bundle"
    assert (bundle_dir / "install.sh").read_text() == snapshot(
        """\
#!/usr/bin/env bash
set -e
BUNDLE_DIR=$(pwd)
echo "==> Setting up directories..."
mkdir -p /home/testuser/.local/share/fujin/testapp
mv .env /home/testuser/.local/share/fujin/testapp/.env
echo '==> Installing application...'
cd /home/testuser/.local/share/fujin/testapp || exit 1
trap 'echo "ERROR: install failed at $(date)" >&2; echo "Working dir: $BUNDLE_DIR" >&2; exit 1' ERR
echo 'set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export UV_COMPILE_BYTECODE=1
export UV_PYTHON=python3.12
export PATH=".venv/bin:$PATH"' > /home/testuser/.local/share/fujin/testapp/.appenv
echo '==> Syncing Python dependencies...'
uv python install 3.12
test -d .venv || uv venv
uv pip install -r $BUNDLE_DIR/requirements.txt
uv pip install --no-deps $BUNDLE_DIR/testapp-0.1.0.whl
echo '0.1.0' > .version
cd $BUNDLE_DIR
echo "==> Configuring systemd services..."
sudo cp units/* /etc/systemd/system/


APP_NAME="testapp"
read -r -a VALID_UNITS <<< "testapp-worker@.service testapp-worker@1.service testapp-worker@2.service testapp.service"

# Exact match helper
contains_exact() {
    local needle="$1"; shift
    for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
    return 1
}

echo "==> Discovering installed unit files"
mapfile -t INSTALLED_UNITS < <(
    systemctl list-unit-files --type=service --no-legend --no-pager \\
    | awk -v app="$APP_NAME" '$1 ~ "^"app {print $1}'
)

echo "==> Disabling + stopping stale units"
for UNIT in "${INSTALLED_UNITS[@]}"; do
    if ! contains_exact "$UNIT" "${VALID_UNITS[@]}"; then

        # If it's a template file (myapp@.service), only disable it.
        if [[ "$UNIT" == *@.service ]]; then
            echo "→ Disabling template unit: $UNIT"
            sudo systemctl disable "$UNIT" --quiet || true
        else
            echo "→ Stopping + disabling stale unit: $UNIT"
            sudo systemctl stop "$UNIT" --quiet || true
            sudo systemctl disable "$UNIT" --quiet || true
        fi

        sudo systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
    fi
done

echo "==> Removing stale service files"
SEARCH_DIRS=(
    /etc/systemd/system/
    /etc/systemd/system/multi-user.target.wants/
)

for DIR in "${SEARCH_DIRS[@]}"; do
    [[ -d "$DIR" ]] || continue

    while IFS= read -r -d '' FILE; do
        BASENAME=$(basename "$FILE")
        if ! contains_exact "$BASENAME" "${VALID_UNITS[@]}"; then
            echo "→ Removing stale file: $FILE"
            sudo rm -f -- "$FILE"
        fi
    done < <(find "$DIR" -maxdepth 1 -type f -name "${APP_NAME}*" -print0)
done

echo "==> Reloading systemd"
sudo systemctl daemon-reload


echo "==> Restarting services..."
sudo systemctl enable testapp.service testapp-worker@1.service testapp-worker@2.service
sudo systemctl restart testapp.service testapp-worker@1.service testapp-worker@2.service
echo "==> Configuring Caddy..."
sudo mkdir -p $(dirname /etc/caddy/conf.d/testapp.caddy)
if caddy validate --config Caddyfile >/dev/null 2>&1; then
  sudo mv Caddyfile /etc/caddy/conf.d/testapp.caddy
  sudo chown caddy:caddy /etc/caddy/conf.d/testapp.caddy
  sudo systemctl reload caddy
else
  echo 'Caddyfile validation failed, leaving local Caddyfile for inspection' >&2
fi
echo "==> Pruning old versions..."
cd /home/testuser/.local/share/fujin/testapp/.versions
ls -1t | tail -n +6 | xargs -r rm
echo '==> Install script completed successfully.'\
"""
    )
    assert (bundle_dir / "uninstall.sh").read_text() == snapshot(
        """\
#!/usr/bin/env bash
set -e
sudo systemctl disable --now testapp.service testapp-worker@1.service testapp-worker@2.service --quiet || true
APP_NAME="testapp"
UNITS=(testapp.service testapp-worker@.service)
for UNIT in "${UNITS[@]}"; do
  case "$UNIT" in
    */*|*..*) echo "Skipping suspicious unit name: $UNIT" >&2; continue;;
  esac
  if [[ "$UNIT" != ${APP_NAME}* ]]; then
    echo "Refusing to remove non-app unit: $UNIT" >&2
    continue
  fi
  sudo rm -f /etc/systemd/system/"$UNIT"
done
sudo systemctl daemon-reload
sudo systemctl reset-failed
sudo rm -f /etc/caddy/conf.d/testapp.caddy
sudo systemctl reload caddy
echo '==> Uninstall completed.'\
"""
    )
    assert (bundle_dir / "Caddyfile").read_text() == snapshot(
        """\
example.com {
	

	reverse_proxy localhost:8000
}\
"""
    )
    assert (bundle_dir / "units" / "testapp.service").read_text() == snapshot(
        """\
# All options are documented here https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
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
WantedBy=multi-user.target\
"""
    )

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mkdir -p /home/testuser/.local/share/fujin/testapp/.versions',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && sha256sum /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz.uploading.1234567890 | awk '{print $1}'",
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mv /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz.uploading.1234567890 /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && mkdir -p /tmp/testapp-0.1.0 && tar --overwrite -xzf /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz -C /tmp/testapp-0.1.0 && cd /tmp/testapp-0.1.0 && chmod +x install.sh && bash ./install.sh || (echo 'install.sh failed' >&2; exit 1) && cd / && rm -rf /tmp/testapp-0.1.0",
        ]
    )
