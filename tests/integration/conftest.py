"""Integration test fixtures.

Fixtures for integration tests that require Docker, SSH, and real deployments.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def mock_stdin():
    """Replace sys.stdin with /dev/null to provide a real file descriptor for select()."""
    original_stdin = sys.stdin
    with open(os.devnull, "r") as devnull:
        sys.stdin = devnull
        yield
        sys.stdin = original_stdin


@pytest.fixture(scope="session")
def docker_image():
    """Build Docker image once per session."""
    image_name = "fujin-test-vps"
    dockerfile_path = Path(__file__).parent / "Dockerfile"

    subprocess.run(
        ["docker", "build", "-t", image_name, "-f", str(dockerfile_path), "."],
        check=True,
        cwd=Path(__file__).parent.parent.parent,
    )
    return image_name


@pytest.fixture(scope="function")
def vps_container(docker_image):
    """Run container with systemd support for each test."""
    container_name = f"fujin-vps-{int(time.time() * 1000)}"

    # Run with cgroups and privileged mode for systemd
    cmd = [
        "docker",
        "run",
        "-d",
        "--privileged",
        "-v",
        "/sys/fs/cgroup:/sys/fs/cgroup:rw",
        "--cgroupns=host",
        "-p",
        "0:22",  # Let Docker assign a port
        "--name",
        container_name,
        docker_image,
    ]

    subprocess.run(cmd, check=True)

    # Get the assigned port
    port_output = subprocess.check_output(
        ["docker", "port", container_name, "22"], text=True
    ).strip()
    # Output format: 0.0.0.0:32768
    host_port = int(port_output.split(":")[-1])

    # Wait for SSH to be ready
    time.sleep(5)

    # Ensure SSH is running
    subprocess.run(
        ["docker", "exec", container_name, "service", "ssh", "start"], check=False
    )

    time.sleep(2)

    yield {
        "name": container_name,
        "ip": "127.0.0.1",
        "port": host_port,
        "user": "fujin",
    }

    # Teardown
    subprocess.run(["docker", "rm", "-f", container_name], check=False)


@pytest.fixture
def ssh_key(vps_container, tmp_path):
    """Generate temp SSH key and inject into container."""
    key_path = tmp_path / "id_ed25519"

    # Generate key
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", ""],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    # Read public key
    pub_key = key_path.with_suffix(".pub").read_text().strip()

    # Inject into container
    setup_cmd = (
        f"mkdir -p /home/fujin/.ssh && "
        f"echo '{pub_key}' >> /home/fujin/.ssh/authorized_keys && "
        f"chown -R fujin:fujin /home/fujin/.ssh && "
        f"chmod 700 /home/fujin/.ssh && "
        f"chmod 600 /home/fujin/.ssh/authorized_keys"
    )

    subprocess.run(
        [
            "docker",
            "exec",
            "-u",
            "fujin",
            vps_container["name"],
            "bash",
            "-c",
            setup_cmd,
        ],
        check=True,
    )

    return str(key_path)
