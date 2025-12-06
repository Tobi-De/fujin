import pytest
import subprocess
import time
from pathlib import Path


@pytest.fixture(scope="session")
def mock_vps_image():
    """Builds the docker image once per session."""
    image_name = "fujin-test-vps"
    dockerfile_path = Path(__file__).parent / "Dockerfile"
    subprocess.run(
        ["docker", "build", "-t", image_name, "-f", str(dockerfile_path), "."],
        check=True,
    )
    return image_name


@pytest.fixture(scope="module")
def vps_container(mock_vps_image):
    """Runs the container with systemd support."""
    container_name = f"fujin-vps-{int(time.time())}"

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
        mock_vps_image,
    ]

    subprocess.run(cmd, check=True)

    # Get the assigned port
    port_output = subprocess.check_output(
        ["docker", "port", container_name, "22"], text=True
    ).strip()
    # Output format usually: 0.0.0.0:32768
    # We need the port number.
    host_port = int(port_output.split(":")[-1])

    # Wait for SSH to be ready
    time.sleep(5)

    # Ensure ssh is running (just in case systemd didn't start it yet or failed)
    subprocess.run(
        ["docker", "exec", container_name, "service", "ssh", "start"], check=False
    )

    time.sleep(2)

    yield {
        "name": container_name,
        "ip": "127.0.0.1",
        "port": host_port,
        "user": "fujin",
        "password": "fujin",  # Or setup keys here
    }

    # Teardown
    subprocess.run(["docker", "rm", "-f", container_name], check=False)


@pytest.fixture
def ssh_key_setup(vps_container, tmp_path):
    """Generates a temp SSH key and injects it into the container."""
    key_path = tmp_path / "id_rsa"

    # Generate key
    subprocess.run(
        ["ssh-keygen", "-t", "rsa", "-f", str(key_path), "-N", ""],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    # Read public key
    pub_key = key_path.with_suffix(".pub").read_text().strip()

    # Inject into container
    # We use docker exec to append to authorized_keys
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
