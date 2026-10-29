"""Host prerequisites (#97): commands, markers and service PATH."""

from __future__ import annotations

import base64
import json

import msgspec
import pytest

from fujin import prerequisites
from fujin.config import Config


def make_config(**overrides) -> Config:
    data = {
        "app": "testapp",
        "version": "1.0.0",
        "build_command": "echo building",
        "installation_mode": "python-package",
        "python_version": "3.11",
        "distfile": "dist/testapp-{version}-py3-none-any.whl",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "prerequisites": {
            "packages": ["postgresql-client"],
            "commands": [
                {
                    "id": "tool",
                    "check": "[ -x {shared_dir}/bin/tool ]",
                    "run": "install-tool {shared_dir}/bin",
                }
            ],
        },
    }
    data.update(overrides)
    return msgspec.convert(data, type=Config)


class FakeConn:
    def __init__(self, *, check_ok: bool = True, run_ok: bool = True):
        self.calls: list[str] = []
        self.check_ok = check_ok
        self.run_ok = run_ok

    def run(self, command, **_kwargs):
        self.calls.append(command)
        if command.startswith("mkdir -p "):
            return ("", True)
        if command.startswith("export PATH="):
            return ("", self.check_ok)
        if command.startswith("cd "):
            return ("tool output", self.run_ok)
        return ("", True)


def test_passing_check_skips_run():
    config = make_config()
    conn = FakeConn(check_ok=True)

    prerequisites.apply(conn, config, config.hosts[0])

    assert any("/opt/fujin/testapp/shared/bin/tool ]" in c for c in conn.calls)
    assert not any(c.startswith("cd /opt/fujin/testapp") for c in conn.calls)


def test_failing_check_runs_once_without_sudo():
    config = make_config()
    conn = FakeConn(check_ok=False)

    prerequisites.apply(conn, config, config.hosts[0])

    runs = [c for c in conn.calls if c.startswith("cd /opt/fujin/testapp")]
    assert len(runs) == 1
    assert "install-tool /opt/fujin/testapp/shared/bin" in runs[0]
    assert "sudo" not in runs[0]
    assert "PATH=/opt/fujin/testapp/shared/bin:$PATH" in runs[0]


def test_failing_run_aborts():
    config = make_config()
    conn = FakeConn(check_ok=False, run_ok=False)

    with pytest.raises(RuntimeError, match="Prerequisite command 'tool' failed"):
        prerequisites.apply(conn, config, config.hosts[0])


def test_unknown_template_variable_aborts_before_running():
    config = make_config(
        prerequisites={
            "commands": [{"id": "tool", "check": "true", "run": "echo {missing}"}]
        }
    )
    conn = FakeConn(check_ok=False)

    with pytest.raises(RuntimeError, match="unknown variables: missing"):
        prerequisites.apply(conn, config, config.hosts[0])


def test_system_packages_are_core_plus_configured_without_duplicates():
    config = make_config(prerequisites={"packages": ["curl", "libpq-dev"]})
    assert prerequisites.system_packages(config, config.hosts[0]) == [
        "sqlite3",
        "curl",
        "rsync",
        "libpq-dev",
    ]


def test_fingerprint_tracks_packages_commands_and_shared_dir():
    host = make_config().hosts[0]
    base = make_config()
    same = make_config()

    assert prerequisites.fingerprint(base, host) == prerequisites.fingerprint(
        same, host
    )

    changed_package = make_config(prerequisites={"packages": ["libpq-dev"]})
    assert prerequisites.fingerprint(
        changed_package, host
    ) != prerequisites.fingerprint(base, host)

    changed_command = make_config(
        prerequisites={"commands": [{"id": "x", "check": "true", "run": "true"}]}
    )
    assert prerequisites.fingerprint(
        changed_command, host
    ) != prerequisites.fingerprint(base, host)

    changed_shared = make_config(shared_dir="/srv/testapp-data")
    assert prerequisites.fingerprint(changed_shared, host) != prerequisites.fingerprint(
        base, host
    )


def test_write_marker_uses_root_tee_and_records_hash():
    config = make_config()
    conn = FakeConn()

    prerequisites.write_marker(conn, config, config.hosts[0])

    command = conn.calls[0]
    assert "sudo mkdir -p /etc/fujin/testapp" in command
    assert "sudo tee /etc/fujin/testapp/prerequisites.json" in command
    encoded = command.split("printf '%s' ", 1)[1].split(" | ", 1)[0].strip("'")
    payload = json.loads(base64.b64decode(encoded))
    assert payload["hash"] == prerequisites.fingerprint(config, config.hosts[0])
    assert payload["packages"] == ["postgresql-client"]


# =============================================================================
# Service PATH
# =============================================================================


def test_service_path_is_prepended():
    content = "[Service]\nExecStart=/bin/app\n"
    rendered = prerequisites.ensure_service_path(content, "/opt/fujin/app/shared/bin")
    assert "Environment=PATH=/opt/fujin/app/shared/bin:/usr/local/sbin" in rendered
    assert rendered.index("Environment=PATH=") < rendered.index("ExecStart=")


def test_existing_service_path_is_rewritten_not_duplicated():
    content = (
        '[Service]\nEnvironment="PATH=/usr/local/bin:/usr/bin"\nExecStart=/bin/app\n'
    )
    rendered = prerequisites.ensure_service_path(content, "/shared/bin")
    assert rendered.count("Environment=PATH=") == 1
    assert 'Environment="PATH=' not in rendered
    assert rendered.startswith("[Service]\nEnvironment=PATH=/shared/bin:")


def test_non_service_units_are_untouched():
    content = "[Socket]\nListenStream=127.0.0.1:8000\n"
    assert prerequisites.ensure_service_path(content, "/shared/bin") == content
