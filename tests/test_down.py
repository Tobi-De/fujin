from unittest.mock import patch, MagicMock
from fujin.commands.down import Down
from fujin.commands.deploy import Deploy
from inline_snapshot import snapshot
from tests.script_runner import script_runner  # noqa: F401
import tarfile
import io
import pytest


def test_down_aborts_if_not_confirmed(mock_connection, get_commands):
    with patch("rich.prompt.Confirm.ask", return_value=False):
        down = Down()
        down()
        assert get_commands(mock_connection.mock_calls) == snapshot([])


def test_down_command_generation(mock_connection, get_commands):
    with patch("rich.prompt.Confirm.ask", return_value=True):
        down = Down()
        down()

        assert get_commands(mock_connection.mock_calls) == snapshot(
            [
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && cat /home/testuser/.local/share/fujin/testapp/.version',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && test -f /home/testuser/.local/share/fujin/testapp/.versions/testapp-.tar.gz',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mkdir -p /tmp/uninstall-testapp- && tar --overwrite -xzf /home/testuser/.local/share/fujin/testapp/.versions/testapp-.tar.gz -C /tmp/uninstall-testapp- && cd /tmp/uninstall-testapp- && chmod +x uninstall.sh && bash ./uninstall.sh && cd / && rm -rf /tmp/uninstall-testapp-rm -rf /home/testuser/.local/share/fujin/testapp',
            ]
        )


@pytest.fixture
def setup_distfile(tmp_path, mock_config):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    dist_file = dist_dir / f"testapp-{mock_config.version}.whl"
    dist_file.touch()
    mock_config.distfile = str(dist_dir / "testapp-{version}.whl")
    return dist_file


def test_down_script_execution(
    mock_connection,
    script_runner,
    mock_config,
    capture_bundle,
    setup_distfile,
):
    # 1. Deploy first to generate the bundle
    mock_config.app_name = "testapp"
    mock_config.version = "0.1.0"

    captured_command = []

    def run_side_effect(cmd, **kwargs):
        captured_command.append(cmd)
        if "sha256sum" in cmd:
            return "checksum123\n", True
        return "", True

    mock_connection.run.side_effect = run_side_effect

    with patch("hashlib.file_digest") as mock_digest:
        mock_digest.return_value.hexdigest.return_value = "checksum123"
        with patch("subprocess.run"):
            deploy = Deploy()
            deploy()

    # 2. Setup environment for Down
    app_dir = script_runner.root / "home/testuser/.local/share/fujin/testapp"
    app_dir.mkdir(parents=True)
    (app_dir / ".version").write_text("0.1.0")

    versions_dir = app_dir / ".versions"
    versions_dir.mkdir()

    # Copy the generated bundle to the "remote" location
    bundle_name = f"testapp-0.1.0.tar.gz"
    generated_bundle = capture_bundle / "deploy.tar.gz"
    remote_bundle = versions_dir / bundle_name
    remote_bundle.write_bytes(generated_bundle.read_bytes())

    # Mock system commands
    script_runner._create_mock("userdel", "echo userdel $@")

    # Setup systemd units to be removed
    systemd_dir = script_runner.root / "etc/systemd/system"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / "testapp.service").touch()
    (systemd_dir / "testapp-worker@.service").touch()

    # Mock systemctl list-unit-files output for uninstall script
    script_runner._create_mock(
        "systemctl",
        """
if [[ "$1" == "list-unit-files" ]]; then
    echo "testapp.service enabled"
    echo "testapp-worker@.service enabled"
else
    echo "systemctl $@" >> """
        + str(script_runner.logs / "systemctl.log")
        + """
fi
""",
    )

    # 3. Run Down
    with patch("rich.prompt.Confirm.ask", return_value=True):
        down = Down(full=False)
        down()

    # 4. Verify uninstall script execution
    # Extract and run the script manually as before
    with tarfile.open(remote_bundle, "r:gz") as tar:
        extracted_script = tar.extractfile("./uninstall.sh").read().decode()

    result = script_runner.run(extracted_script)
    result.assert_success()

    # Verify systemd units were stopped/disabled
    log = result.get_log("systemctl")
    assert "disable --now testapp.service" in log
    assert "testapp-worker@1.service" in log
    assert "testapp-worker@2.service" in log

    # Verify Caddy was NOT removed (full=False)
    caddy_cmds = "sudo systemctl stop caddy"
    assert not any(caddy_cmds in cmd for cmd in captured_command)


def test_down_full_script_execution(
    mock_connection,
    script_runner,
    mock_config,
    capture_bundle,
    setup_distfile,
):
    # 1. Deploy first to generate the bundle
    mock_config.app_name = "testapp"
    mock_config.version = "0.1.0"

    captured_command = []

    def run_side_effect(cmd, **kwargs):
        captured_command.append(cmd)
        if "sha256sum" in cmd:
            return "checksum123\n", True
        return "", True

    mock_connection.run.side_effect = run_side_effect

    with patch("hashlib.file_digest") as mock_digest:
        mock_digest.return_value.hexdigest.return_value = "checksum123"
        with patch("subprocess.run"):
            deploy = Deploy()
            deploy()

    # 2. Setup environment for Down
    app_dir = script_runner.root / "home/testuser/.local/share/fujin/testapp"
    app_dir.mkdir(parents=True)
    (app_dir / ".version").write_text("0.1.0")

    versions_dir = app_dir / ".versions"
    versions_dir.mkdir()

    # Copy the generated bundle to the "remote" location
    bundle_name = f"testapp-0.1.0.tar.gz"
    generated_bundle = capture_bundle / "deploy.tar.gz"
    remote_bundle = versions_dir / bundle_name
    remote_bundle.write_bytes(generated_bundle.read_bytes())

    # Mock system commands
    script_runner._create_mock("userdel", "echo userdel $@")

    # Setup systemd units
    systemd_dir = script_runner.root / "etc/systemd/system"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / "testapp.service").touch()

    # Mock systemctl
    script_runner._create_mock(
        "systemctl",
        """
if [[ "$1" == "list-unit-files" ]]; then
    echo "testapp.service enabled"
else
    echo "systemctl $@" >> """
        + str(script_runner.logs / "systemctl.log")
        + """
fi
""",
    )

    # 3. Run Down with full=True
    with patch("rich.prompt.Confirm.ask", return_value=True):
        down = Down(full=True)
        down()

    # 4. Verify uninstall script execution
    with tarfile.open(remote_bundle, "r:gz") as tar:
        extracted_script = tar.extractfile("./uninstall.sh").read().decode()

    result = script_runner.run(extracted_script)
    result.assert_success()

    # Verify systemd units were stopped/disabled
    log = result.get_log("systemctl")
    assert "disable --now testapp.service" in log

    # Verify Caddy WAS removed (full=True)
    caddy_cmds = "&& ".join(
        [
            "sudo systemctl stop caddy",
            "sudo systemctl disable caddy",
            "sudo rm -f /usr/bin/caddy",
            "sudo rm -f /etc/systemd/system/caddy.service",
            "sudo userdel caddy",
            "sudo rm -rf /etc/caddy",
        ]
    )
    assert any(caddy_cmds in cmd for cmd in captured_command)
