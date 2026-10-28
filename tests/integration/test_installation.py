"""Integration tests for installation scenarios.

These tests cover systemd unit kinds (sockets, timers), stale unit cleanup
and dropin directory handling.
"""

from __future__ import annotations

import shutil

from .helpers import (
    assert_dir_exists,
    assert_dir_not_exists,
    assert_file_contains,
    assert_file_exists,
    assert_file_not_exists,
    assert_systemd_unit_exists,
    assert_systemd_unit_not_exists,
    deploy_app,
    exec_in_container,
    wait_for_service,
)


def test_socket_and_timer_units(shared_vps, project, app_port):
    """Socket-activated service and timer-scheduled service in one app."""
    container = shared_vps["name"]
    port = app_port
    units = {
        "web.service": """[Unit]
Description=Socket-activated web server
Requires={app_name}-web.socket
After={app_name}-web.socket

[Service]
Type=simple
ExecStart=/bin/sleep infinity
WorkingDirectory={app_dir}
StandardInput=socket

[Install]
WantedBy=multi-user.target
""",
        "web.socket": f"""[Unit]
Description=Socket for web server

[Socket]
ListenStream={port}
Accept=no

[Install]
WantedBy=sockets.target
""",
        "cleanup.service": """[Unit]
Description=Cleanup task

[Service]
Type=oneshot
ExecStart={app_dir}/current/bin/mixedapp
WorkingDirectory={app_dir}
""",
        "cleanup.timer": """[Unit]
Description=Run cleanup daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
""",
    }
    deploy_app(
        project(
            "mixedapp",
            shared_vps,
            script="#!/bin/bash\nexit 0\n",
            units=units,
        )
    )

    # Unit files for both kinds are installed.
    for unit_name in (
        "mixedapp-web.socket",
        "mixedapp-web.service",
        "mixedapp-cleanup.timer",
        "mixedapp-cleanup.service",
    ):
        assert_systemd_unit_exists(container, unit_name)

    # Socket is listening and activated the service.
    stdout, ok = exec_in_container(container, "systemctl is-active mixedapp-web.socket")
    assert ok and stdout == "active", f"Socket not active: {stdout}"
    wait_for_service(container, "mixedapp-web.service")

    # Timer is active and scheduled.
    stdout, ok = exec_in_container(
        container, "systemctl is-active mixedapp-cleanup.timer"
    )
    assert ok and stdout == "active", f"Timer not active: {stdout}"
    stdout, _ = exec_in_container(
        container, "systemctl list-timers mixedapp-cleanup.timer --no-pager"
    )
    assert "mixedapp-cleanup.timer" in stdout


def test_unit_and_dropin_convergence(shared_vps, project, unit, tmp_path):
    """Stale units disappear, common.d/service.d dropins merge, stale
    dropin directories are removed."""
    container = shared_vps["name"]
    app = "convergeapp"
    systemd_dir = tmp_path / ".fujin" / "systemd"

    deploy_app(
        project(
            app,
            shared_vps,
            version="1.0.0",
            units={
                "web.service": unit(app, "web"),
                "worker.service": unit(app, "worker"),
            },
        )
    )
    # v1 dropins: common limits + stale common dropin + web-specific dropin.
    common_dir = systemd_dir / "common.d"
    common_dir.mkdir()
    limits = common_dir / "limits.conf"
    limits.write_text("[Service]\nLimitNOFILE=65536\n")
    stale = common_dir / "old-config.conf"
    stale.write_text("[Service]\nNice=10\n")
    web_dropins = systemd_dir / "web.service.d"
    web_dropins.mkdir()
    (web_dropins / "memory.conf").write_text("[Service]\nMemoryMax=512M\n")
    deploy_app(
        project(
            app,
            shared_vps,
            version="1.1.0",
            units={"web.service": unit(app, "web")},
        )
    )

    wait_for_service(container, f"{app}-web.service")
    wait_for_service(container, f"{app}-worker.service")
    assert_systemd_unit_exists(container, f"{app}-worker.service")
    assert_dir_exists(container, f"/etc/systemd/system/{app}-web.service.d")
    assert_file_contains(
        container,
        f"/etc/systemd/system/{app}-web.service.d/limits.conf",
        "LimitNOFILE=65536",
    )
    assert_file_contains(
        container,
        f"/etc/systemd/system/{app}-web.service.d/memory.conf",
        "MemoryMax=512M",
    )
    assert_file_contains(
        container,
        f"/etc/systemd/system/{app}-web.service.d/old-config.conf",
        "Nice=10",
    )
    stdout, ok = exec_in_container(
        container, f"systemctl show {app}-web.service -p LimitNOFILE"
    )
    assert ok and "65536" in stdout, f"LimitNOFILE not applied: {stdout}"

    # v2 removes the worker unit and the stale common dropin.
    (systemd_dir / "worker.service").unlink()
    stale.unlink()
    deploy_app(
        project(
            app,
            shared_vps,
            version="2.0.0",
            units={"web.service": unit(app, "web")},
        )
    )

    wait_for_service(container, f"{app}-web.service")
    stdout, _ = exec_in_container(
        container, f"systemctl is-active {app}-worker.service"
    )
    assert stdout in ["inactive", "unknown"], f"Worker should be stopped: {stdout}"
    assert_systemd_unit_not_exists(container, f"{app}-worker.service")
    assert_file_not_exists(
        container, f"/etc/systemd/system/{app}-web.service.d/old-config.conf"
    )
    assert_file_exists(
        container, f"/etc/systemd/system/{app}-web.service.d/limits.conf"
    )
    assert_file_exists(
        container, f"/etc/systemd/system/{app}-web.service.d/memory.conf"
    )

    # v3 drops every dropin: the dropin directory is gone.
    shutil.rmtree(common_dir)
    shutil.rmtree(web_dropins)
    deploy_app(
        project(
            app,
            shared_vps,
            version="3.0.0",
            units={"web.service": unit(app, "web")},
        )
    )

    wait_for_service(container, f"{app}-web.service")
    assert_file_not_exists(
        container, f"/etc/systemd/system/{app}-web.service.d/limits.conf"
    )
    assert_dir_not_exists(container, f"/etc/systemd/system/{app}-web.service.d")


def test_redeploy_does_not_rewrite_unchanged_units(shared_vps, project, unit):
    """A non-noop redeploy with an unchanged unit set leaves the installed
    unit file untouched (same inode and mtime) and keeps the service live."""
    container = shared_vps["name"]
    app = "stableunitsapp"
    deploy_app(
        project(
            app,
            shared_vps,
            version="1.0.0",
            units={"web.service": unit(app, "web")},
        )
    )
    wait_for_service(container, f"{app}-web.service")
    unit_path = f"/etc/systemd/system/{app}-web.service"
    before, ok = exec_in_container(container, f"stat -c '%y %i' {unit_path}")
    assert ok and before
    release_before, ok = exec_in_container(
        container, f"readlink /opt/fujin/{app}/current"
    )
    assert ok and release_before

    # Version bump: the dist entry changes (a real, non-noop deploy) while
    # the unit set stays byte-identical.
    deploy_app(
        project(
            app,
            shared_vps,
            version="2.0.0",
            units={"web.service": unit(app, "web")},
        )
    )
    wait_for_service(container, f"{app}-web.service")

    release_after, _ = exec_in_container(
        container, f"readlink /opt/fujin/{app}/current"
    )
    assert release_after != release_before, "a new release must have been activated"
    after, _ = exec_in_container(container, f"stat -c '%y %i' {unit_path}")
    assert after == before, f"unit file was rewritten: {before} -> {after}"
    stdout, ok = exec_in_container(container, f"systemctl is-active {app}-web.service")
    assert ok and stdout == "active", f"Service not active: {stdout}"
