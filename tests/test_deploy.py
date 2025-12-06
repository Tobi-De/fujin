import hashlib
from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest

from inline_snapshot import snapshot
from fujin.commands.deploy import Deploy
from fujin.config import InstallationMode


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
        mock_digest.return_value.hexdigest.return_value = ""
        yield


@pytest.fixture(autouse=True)
def mock_time():
    with patch("time.time", return_value=1234567890):
        yield


def test_deploy_binary_mode(
    mock_config, mock_connection, get_commands, setup_distfile, mock_checksum_match
):
    mock_config.installation_mode = InstallationMode.BINARY
    mock_config.app_name = "myapp"

    # Mock subprocess to avoid actual build
    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mkdir -p /home/testuser/.local/share/fujin/myapp/.versions',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && sha256sum /home/testuser/.local/share/fujin/myapp/.versions/myapp-0.1.0.tar.gz.uploading.1234567890 | awk '{print $1}'",
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mv /home/testuser/.local/share/fujin/myapp/.versions/myapp-0.1.0.tar.gz.uploading.1234567890 /home/testuser/.local/share/fujin/myapp/.versions/myapp-0.1.0.tar.gz',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && mkdir -p /tmp/myapp-0.1.0 && tar --overwrite -xzf /home/testuser/.local/share/fujin/myapp/.versions/myapp-0.1.0.tar.gz -C /tmp/myapp-0.1.0 && cd /tmp/myapp-0.1.0 && chmod +x install.sh && bash ./install.sh || (echo 'install.sh failed' >&2; exit 1) && cd / && rm -rf /tmp/myapp-0.1.0",
        ]
    )


def test_deploy_python_rebuild_venv(
    mock_config,
    mock_connection,
    tmp_path,
    get_commands,
    setup_distfile,
    mock_checksum_match,
):
    mock_config.installation_mode = InstallationMode.PY_PACKAGE
    mock_config.requirements = "requirements.txt"

    # Create dummy requirements file
    req_path = tmp_path / "requirements.txt"
    req_path.write_text("django")
    mock_config.requirements = str(req_path)

    # Mock remote state: No previous version, so hash check fails/skipped
    def run_side_effect(cmd, **kwargs):
        stdout = ""
        if "head -n 1 .versions" in cmd:
            stdout = ""
        return stdout, True

    mock_connection.run.side_effect = run_side_effect

    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mkdir -p /home/testuser/.local/share/fujin/testapp/.versions',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && sha256sum /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz.uploading.1234567890 | awk '{print $1}'",
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mv /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz.uploading.1234567890 /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && mkdir -p /tmp/testapp-0.1.0 && tar --overwrite -xzf /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz -C /tmp/testapp-0.1.0 && cd /tmp/testapp-0.1.0 && chmod +x install.sh && bash ./install.sh || (echo 'install.sh failed' >&2; exit 1) && cd / && rm -rf /tmp/testapp-0.1.0",
        ]
    )


def test_deploy_python_reuse_venv(
    mock_config,
    mock_connection,
    tmp_path,
    get_commands,
    setup_distfile,
    mock_checksum_match,
):
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
        stdout = ""
        if "head -n 1 .versions" in cmd:
            stdout = "0.0.1"
        if "md5sum" in cmd:
            stdout = f"{local_hash}  requirements.txt"
        return stdout, True

    mock_connection.run.side_effect = run_side_effect

    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mkdir -p /home/testuser/.local/share/fujin/testapp/.versions',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && sha256sum /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz.uploading.1234567890 | awk '{print $1}'",
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mv /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz.uploading.1234567890 /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && mkdir -p /tmp/testapp-0.1.0 && tar --overwrite -xzf /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz -C /tmp/testapp-0.1.0 && cd /tmp/testapp-0.1.0 && chmod +x install.sh && bash ./install.sh || (echo 'install.sh failed' >&2; exit 1) && cd / && rm -rf /tmp/testapp-0.1.0",
        ]
    )


def test_deploy_version_update(
    mock_config, mock_connection, get_commands, setup_distfile, mock_checksum_match
):
    # Mock remote state: .versions file exists
    def run_side_effect(cmd, **kwargs):
        stdout = ""
        if "head -n 1 .versions" in cmd:
            stdout = "0.0.1"  # Different from current version
        return stdout, True

    mock_connection.run.side_effect = run_side_effect

    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mkdir -p /home/testuser/.local/share/fujin/testapp/.versions',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && sha256sum /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz.uploading.1234567890 | awk '{print $1}'",
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mv /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz.uploading.1234567890 /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && mkdir -p /tmp/testapp-0.1.0 && tar --overwrite -xzf /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz -C /tmp/testapp-0.1.0 && cd /tmp/testapp-0.1.0 && chmod +x install.sh && bash ./install.sh || (echo 'install.sh failed' >&2; exit 1) && cd / && rm -rf /tmp/testapp-0.1.0",
        ]
    )


def test_deploy_pruning(
    mock_config, mock_connection, get_commands, setup_distfile, mock_checksum_match
):
    mock_config.versions_to_keep = 2

    # Mock remote state: return list of versions to prune
    def run_side_effect(cmd, **kwargs):
        stdout = ""
        if "sed -n" in cmd:
            # Simulate 3 versions existing, keeping 2, so 1 to prune
            stdout = "0.0.1"
        return stdout, True

    mock_connection.run.side_effect = run_side_effect

    with patch("subprocess.run"):
        deploy = Deploy()
        deploy()

    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mkdir -p /home/testuser/.local/share/fujin/testapp/.versions',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && sha256sum /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz.uploading.1234567890 | awk '{print $1}'",
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mv /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz.uploading.1234567890 /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz',
            "export PATH=\"/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH\" && mkdir -p /tmp/testapp-0.1.0 && tar --overwrite -xzf /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz -C /tmp/testapp-0.1.0 && cd /tmp/testapp-0.1.0 && chmod +x install.sh && bash ./install.sh || (echo 'install.sh failed' >&2; exit 1) && cd / && rm -rf /tmp/testapp-0.1.0",
        ]
    )
