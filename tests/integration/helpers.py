"""Integration test helper functions.

Provides assertion utilities and common operations for integration tests.
"""

from __future__ import annotations

import subprocess
import time


def exec_in_container(container_name: str, cmd: str) -> tuple[str, bool]:
    """Execute command in container and return (stdout, success)."""
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c", cmd],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip(), result.returncode == 0


def assert_service_running(container_name: str, service_name: str) -> None:
    """Verify systemd service is active."""
    stdout, ok = exec_in_container(
        container_name, f"systemctl is-active {service_name}"
    )
    assert ok and stdout == "active", f"Service {service_name} not active: {stdout}"


def assert_service_not_running(container_name: str, service_name: str) -> None:
    """Verify systemd service is not active."""
    stdout, _ = exec_in_container(container_name, f"systemctl is-active {service_name}")
    assert stdout in ["inactive", "unknown", "failed"], (
        f"Service {service_name} should not be running: {stdout}"
    )


def assert_file_exists(container_name: str, path: str) -> None:
    """Verify file exists in container."""
    _, ok = exec_in_container(container_name, f"test -f {path}")
    assert ok, f"File {path} does not exist"


def assert_file_not_exists(container_name: str, path: str) -> None:
    """Verify file does not exist in container."""
    _, ok = exec_in_container(container_name, f"test -f {path}")
    assert not ok, f"File {path} should not exist"


def assert_file_contains(container_name: str, path: str, content: str) -> None:
    """Verify file contains expected content."""
    stdout, ok = exec_in_container(container_name, f"cat {path}")
    assert ok, f"Could not read {path}"
    assert content in stdout, f"Expected '{content}' in {path}, got: {stdout}"


def assert_dir_exists(container_name: str, path: str) -> None:
    """Verify directory exists in container."""
    _, ok = exec_in_container(container_name, f"test -d {path}")
    assert ok, f"Directory {path} does not exist"


def assert_dir_not_exists(container_name: str, path: str) -> None:
    """Verify directory does not exist in container."""
    _, ok = exec_in_container(container_name, f"test -d {path}")
    assert not ok, f"Directory {path} should not exist"


def assert_app_user_exists(container_name: str, username: str) -> None:
    """Verify app user was created."""
    _, ok = exec_in_container(container_name, f"id -u {username}")
    assert ok, f"User {username} does not exist"


def assert_app_user_not_exists(container_name: str, username: str) -> None:
    """Verify app user does not exist."""
    _, ok = exec_in_container(container_name, f"id -u {username}")
    assert not ok, f"User {username} should not exist"


def assert_http_responds(
    container_name: str,
    port: int,
    expected_status: int = 200,
    path: str = "/",
    timeout: int = 10,
) -> None:
    """Verify HTTP endpoint is accessible with retries."""
    cmd = f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}{path}"
    for _ in range(timeout):
        stdout, ok = exec_in_container(container_name, cmd)
        if ok and stdout == str(expected_status):
            return
        time.sleep(1)
    raise AssertionError(
        f"Expected HTTP {expected_status} on port {port}{path}, got: {stdout}"
    )


def assert_socket_listening(container_name: str, socket_path: str) -> None:
    """Verify systemd socket is listening."""
    _, ok = exec_in_container(container_name, f"test -S {socket_path}")
    assert ok, f"Socket {socket_path} not listening"


def assert_systemd_unit_exists(container_name: str, unit_name: str) -> None:
    """Verify systemd unit file exists."""
    _, ok = exec_in_container(
        container_name, f"test -f /etc/systemd/system/{unit_name}"
    )
    assert ok, f"Systemd unit {unit_name} does not exist"


def assert_systemd_unit_not_exists(container_name: str, unit_name: str) -> None:
    """Verify systemd unit file does not exist."""
    _, ok = exec_in_container(
        container_name, f"test -f /etc/systemd/system/{unit_name}"
    )
    assert not ok, f"Systemd unit {unit_name} should not exist"


def read_file(container_name: str, path: str) -> str:
    """Read file contents from container."""
    stdout, ok = exec_in_container(container_name, f"cat {path}")
    assert ok, f"Could not read {path}"
    return stdout


def get_deployed_version(container_name: str, app_name: str) -> str:
    """Get currently deployed version of app."""
    stdout, ok = exec_in_container(
        container_name, f"cat /opt/fujin/{app_name}/.install/.version"
    )
    assert ok, f"Could not read version for {app_name}"
    return stdout.strip()


def wait_for_service(container_name: str, service_name: str, timeout: int = 10) -> None:
    """Wait for service to become active with retries."""
    for attempt in range(timeout):
        stdout, ok = exec_in_container(
            container_name, f"systemctl is-active {service_name}"
        )
        if ok and stdout == "active":
            return
        time.sleep(1)
    # Show debug info on failure
    status, _ = exec_in_container(container_name, f"systemctl status {service_name}")
    logs, _ = exec_in_container(
        container_name, f"journalctl -u {service_name} --no-pager -n 50"
    )
    raise AssertionError(
        f"Service {service_name} not active after {timeout}s: {stdout}\n"
        f"Status: {status}\nLogs: {logs}"
    )
