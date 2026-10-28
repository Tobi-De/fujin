"""Integration test fixtures.

Fixtures for integration tests that require Docker, SSH, and real deployments.

The Docker image is content-addressed (Dockerfile hash) and built at most once
per machine thanks to an ``flock``-guarded inspect/build.  Most tests share a
single container per xdist worker (``shared_vps``); only tests that mutate
global server state (bootstrap, create-user, harden) get a fresh container via
``fresh_vps_container``.
"""

from __future__ import annotations

import fcntl
import hashlib
import itertools
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import msgspec
import pytest

from fujin.config import Config

from .helpers import teardown_app

REPO_ROOT = Path(__file__).parent.parent.parent
INTEGRATION_DIR = Path(__file__).parent
DOCKERFILE = INTEGRATION_DIR / "Dockerfile"


@pytest.fixture(autouse=True)
def mock_stdin():
    """Replace sys.stdin with /dev/null to provide a real file descriptor for select()."""
    original_stdin = sys.stdin
    with open(os.devnull, "r") as devnull:
        sys.stdin = devnull
        yield
        sys.stdin = original_stdin


# ---------------------------------------------------------------------------
# Docker image
# ---------------------------------------------------------------------------


def _image_tag() -> str:
    """Content-addressed tag: sha256(Dockerfile + build context) [:12].

    The Dockerfile COPYs nothing from the context, so the Dockerfile bytes are
    the only build input; the context marker still keeps the tag stable when
    the build layout changes.
    """
    digest = hashlib.sha256(DOCKERFILE.read_bytes())
    digest.update(b"fujin-integration-build-context:v1")
    dockerignore = REPO_ROOT / ".dockerignore"
    if dockerignore.exists():
        digest.update(dockerignore.read_bytes())
    return f"fujin-test-vps:{digest.hexdigest()[:12]}"


@pytest.fixture(scope="session")
def docker_image() -> str:
    """Return the test VPS image, building it only when it is absent."""
    tag = _image_tag()
    lock_path = Path(tempfile.gettempdir()) / f"fujin-image-{tag.split(':')[1]}.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        inspected = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True,
        )
        if inspected.returncode != 0:
            subprocess.run(
                ["docker", "build", "-t", tag, "-f", str(DOCKERFILE), "."],
                check=True,
                cwd=REPO_ROOT,
            )
    return tag


# ---------------------------------------------------------------------------
# SSH key (one per session, injected into every container)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def ssh_key(tmp_path_factory: pytest.TempPathFactory) -> str:
    key_path = tmp_path_factory.mktemp("ssh") / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", ""],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return str(key_path)


def _inject_key(container_name: str, key_path: str, user: str = "fujin") -> None:
    """Idempotently append the session public key to ``user`` authorized_keys."""
    pub_key = Path(key_path).with_suffix(".pub").read_text().strip()
    script = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        'grep -qxF "$0" ~/.ssh/authorized_keys 2>/dev/null || '
        'printf "%s\\n" "$0" >> ~/.ssh/authorized_keys; '
        "chmod 600 ~/.ssh/authorized_keys"
    )
    subprocess.run(
        ["docker", "exec", "-u", user, container_name, "bash", "-c", script, pub_key],
        check=True,
    )


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


def _wait_for_ssh(container_name: str, host_port: int, timeout: float = 20.0) -> None:
    """Poll the mapped SSH port, then wait for systemd to finish starting."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                ("127.0.0.1", host_port), timeout=0.5
            ) as sock:
                # Docker's port proxy accepts before sshd listens; wait for the
                # actual protocol banner instead of the TCP connect.
                if sock.recv(255).startswith(b"SSH-"):
                    break
        except OSError:
            pass
        time.sleep(0.2)
    else:
        raise RuntimeError(
            f"sshd on 127.0.0.1:{host_port} did not accept connections "
            f"within {timeout}s"
        )
    subprocess.run(
        ["docker", "exec", container_name, "systemctl", "is-system-running", "--wait"],
        capture_output=True,
        timeout=60,
    )


def _start_container(image: str, key_path: str) -> dict[str, object]:
    """Start a privileged systemd container and wait until it is usable."""
    container_name = f"fujin-vps-{uuid.uuid4().hex[:12]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--privileged",
            "-v",
            "/sys/fs/cgroup:/sys/fs/cgroup:rw",
            "--cgroupns=host",
            "-p",
            "0:22",
            "--name",
            container_name,
            image,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    port_output = subprocess.check_output(
        ["docker", "port", container_name, "22"], text=True
    ).strip()
    host_port = int(port_output.split(":")[-1])

    # Every fresh container gets a new host key for (127.0.0.1, port);
    # purge stale conflicting entries once per container.
    subprocess.run(
        ["ssh-keygen", "-R", f"[127.0.0.1]:{host_port}"],
        capture_output=True,
    )

    try:
        _wait_for_ssh(container_name, host_port)
        _inject_key(container_name, key_path)
    except Exception:
        subprocess.run(["docker", "rm", "-f", container_name], check=False)
        raise

    return {
        "name": container_name,
        "ip": "127.0.0.1",
        "port": host_port,
        "user": "fujin",
    }


@pytest.fixture(scope="session")
def shared_vps(docker_image: str, ssh_key: str) -> dict[str, object]:
    """One container shared by all app-namespaced tests in this worker."""
    container = _start_container(docker_image, ssh_key)
    yield container
    subprocess.run(["docker", "rm", "-f", container["name"]], check=False)


@pytest.fixture
def fresh_vps_container(docker_image: str, ssh_key: str) -> dict[str, object]:
    """A pristine container for tests that mutate global server state."""
    container = _start_container(docker_image, ssh_key)
    yield container
    subprocess.run(["docker", "rm", "-f", container["name"]], check=False)


# ---------------------------------------------------------------------------
# Per-test app isolation on the shared container
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _teardown_test_apps(request: pytest.FixtureRequest):
    """Remove every app a test deployed from its (shared) container."""
    yield
    apps: list[tuple[str, str]] = getattr(request.node, "_fujin_apps", [])
    if not apps or request.node.get_closest_marker("no_app_cleanup"):
        return
    seen: set[tuple[str, str]] = set()
    for container_name, app in reversed(apps):
        if (container_name, app) in seen:
            continue
        seen.add((container_name, app))
        teardown_app(container_name, app)


# ---------------------------------------------------------------------------
# Project scaffolding
# ---------------------------------------------------------------------------


_port_counter = itertools.count(18080)


@pytest.fixture
def app_port() -> int:
    """A port unique among the tests of this worker's container."""
    return next(_port_counter)


def _create_minimal_wheel(wheel_path: Path, name: str, version: str) -> None:
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as whl:
        whl.writestr(
            f"{name}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        whl.writestr(
            f"{name}-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        whl.writestr(f"{name}-{version}.dist-info/RECORD", "")


@pytest.fixture
def project(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ssh_key: str,
):
    """Factory building a minimal deployable project (config + files on disk)."""
    monkeypatch.chdir(tmp_path)

    def make(
        app: str,
        container: dict[str, object],
        *,
        version: str = "1.0.0",
        script: str | None = None,
        units: dict[str, str] | None = None,
        mode: str = "binary",
        caddyfile: str | None = None,
        env: str | None = None,
        user: str | None = None,
        replicas: dict[str, int] | None = None,
        hooks: dict[str, list[str]] | None = None,
    ) -> Config:
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir(exist_ok=True)
        data: dict[str, object] = {
            "app": app,
            "version": version,
            "build_command": "echo 'building'",
            "installation_mode": mode,
        }
        if mode == "binary":
            distfile = dist_dir / f"{app}-{version}"
            distfile.write_text(script or "#!/bin/bash\nsleep infinity\n")
            distfile.chmod(0o755)
            data["distfile"] = str(distfile)
        else:
            distfile = dist_dir / f"{app}-{version}-py3-none-any.whl"
            _create_minimal_wheel(distfile, app, version)
            requirements = tmp_path / "requirements.txt"
            requirements.write_text("")
            (tmp_path / "pyproject.toml").write_text(
                f'[project]\nname = "{app}"\nversion = "{version}"\n'
            )
            data["distfile"] = f"dist/{app}-{{version}}-py3-none-any.whl"
            data["python_version"] = "3.11"
            data["requirements"] = str(requirements)

        systemd_dir = tmp_path / ".fujin" / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)
        services = units or {
            "web.service": _service_unit(app, "web"),
        }
        for filename, content in services.items():
            target = systemd_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        if caddyfile is not None:
            (tmp_path / ".fujin" / "Caddyfile").write_text(caddyfile)
        host: dict[str, object] = {
            "address": container["ip"],
            "user": user or container["user"],
            "port": container["port"],
            "key_filename": ssh_key,
        }
        if env is not None:
            (tmp_path / ".fujin" / "env").write_text(env)
            host["envfile"] = ".fujin/env"
        data["hosts"] = [host]
        if replicas is not None:
            data["replicas"] = replicas
        if hooks is not None:
            data["hooks"] = hooks

        config = msgspec.convert(data, type=Config)

        registry: list[tuple[str, str]] | None = getattr(
            request.node, "_fujin_apps", None
        )
        if registry is None:
            registry = request.node._fujin_apps = []
        registry.append((str(container["name"]), app))
        return config

    return make


def _service_unit(
    app: str,
    service: str,
    *,
    restart: str = "always",
    exec_start: str | None = None,
    body: str = "",
) -> str:
    """A standard binary-app service unit (placeholders resolved at deploy)."""
    exec_line = exec_start or f"{{app_dir}}/current/bin/{app}"
    sections = f"""Type=simple
Restart={restart}
ExecStart={exec_line}
WorkingDirectory={{app_dir}}
EnvironmentFile=-{{app_dir}}/current/.env"""
    if body:
        sections = f"{sections}\n{body}"
    return f"""[Unit]
Description={app} {service}

[Service]
{sections}

[Install]
WantedBy=multi-user.target
"""


@pytest.fixture
def unit():
    """Factory for a standard service unit (see :func:`_service_unit`)."""
    return _service_unit
