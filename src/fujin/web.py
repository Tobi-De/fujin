from __future__ import annotations

import json
import shlex
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse

from fujin.config import Config
from fujin.connection import SSH2Connection
from fujin.connection import connection as host_connection

# Global config reference, set by create_app()
_config: Config | None = None
_host_name: str | None = None


def _get_config() -> Config:
    assert _config is not None, "Config not initialized"
    return _config


def _set_host(name: str | None) -> None:
    global _host_name
    _host_name = name


@contextmanager
def _connect() -> Generator[SSH2Connection, None, None]:
    """Create a new SSH connection using the configured host."""
    config = _get_config()
    host = config.select_host(_host_name)
    with host_connection(host=host) as conn:
        yield conn


def _read_template() -> str:
    """Read the HTML template."""
    template_path = Path(__file__).parent / "templates" / "ui.html"
    return template_path.read_text()


async def homepage(request: Request) -> HTMLResponse:
    return HTMLResponse(_read_template())


async def api_hosts(request: Request) -> JSONResponse:
    """List all configured hosts."""
    config = _get_config()
    hosts = []
    for h in config.hosts:
        hosts.append(
            {
                "name": h.name,
                "address": h.address,
                "user": h.user,
            }
        )
    return JSONResponse(
        {
            "hosts": hosts,
            "current": _host_name
            or (
                config.hosts[0].name
                if config.hosts[0].name
                else config.hosts[0].address
            ),
        }
    )


async def api_switch_host(request: Request) -> JSONResponse:
    """Switch the active host."""
    body = await request.json()
    host_name = body.get("host")
    config = _get_config()

    # Validate host exists
    try:
        config.select_host(host_name)
    except Exception:
        return JSONResponse({"error": f"Unknown host: {host_name}"}, status_code=400)

    _set_host(host_name)
    return JSONResponse({"ok": True, "host": host_name})


async def api_status(request: Request) -> JSONResponse:
    """Get app status: version, services, domain, audit log."""
    config = _get_config()
    host = config.select_host(_host_name)

    # Collect all unit names
    service_names = []
    for du in config.deployed_units:
        service_names.extend(du.service_instances())
        if du.template_socket_name:
            service_names.append(du.template_socket_name)
        if du.template_timer_name:
            service_names.append(du.template_timer_name)

    with _connect() as conn:
        # Get remote version
        fujin_dir = shlex.quote(config.install_dir)
        remote_version, _ = conn.run(
            f"cat {fujin_dir}/.version 2>/dev/null || echo N/A",
            warn=True,
            hide=True,
        )
        remote_version = remote_version.strip()

        # Get service statuses
        if service_names:
            statuses_output, _ = conn.run(
                f"sudo systemctl is-active {' '.join(service_names)} 2>/dev/null || true",
                warn=True,
                hide=True,
            )
            statuses = (
                statuses_output.strip().split("\n") if statuses_output.strip() else []
            )
            services_status = dict(zip(service_names, statuses, strict=False))
        else:
            services_status = {}

        # Build per-unit status
        services = []
        for du in config.deployed_units:
            instances = du.service_instances()
            running = sum(
                1 for name in instances if services_status.get(name) == "active"
            )
            total = len(instances)

            unit_info = {
                "name": du.name,
                "is_template": du.is_template,
                "replicas": du.replicas,
                "running": running,
                "total": total,
                "status": "active"
                if running == total
                else ("partial" if running > 0 else "inactive"),
                "instances": [
                    {"name": n, "status": services_status.get(n, "unknown")}
                    for n in instances
                ],
            }

            if du.template_socket_name:
                unit_info["socket"] = {
                    "name": du.template_socket_name,
                    "status": services_status.get(du.template_socket_name, "unknown"),
                }
            if du.template_timer_name:
                unit_info["timer"] = {
                    "name": du.template_timer_name,
                    "status": services_status.get(du.template_timer_name, "unknown"),
                }

            services.append(unit_info)

        # Get audit log (last 10 entries)
        log_file = f"/opt/fujin/.audit/{config.app_name}.log"
        audit_output, success = conn.run(
            f"test -f {log_file} && tail -n 10 {log_file} || echo ''",
            warn=True,
            hide=True,
        )
        audit_entries = []
        for line in audit_output.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    audit_entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        audit_entries.reverse()

    # Domain
    domain = config.get_domain_name() if config.caddyfile_exists else None

    return JSONResponse(
        {
            "app_name": config.app_name,
            "local_version": config.local_version,
            "remote_version": remote_version,
            "domain": domain,
            "host": {
                "name": host.name or host.address,
                "address": host.address,
                "user": host.user,
            },
            "services": services,
            "audit": audit_entries,
        }
    )


async def api_service_detail(request: Request) -> JSONResponse:
    """Get detailed info for a single service: status, config, dropins."""
    config = _get_config()
    service_name = request.path_params["name"]

    # Find the deployed unit
    du = None
    for unit in config.deployed_units:
        if unit.name == service_name:
            du = unit
            break

    if not du:
        return JSONResponse(
            {"error": f"Unknown service: {service_name}"}, status_code=404
        )

    with _connect() as conn:
        # Get detailed status for each instance
        instances = []
        for unit_name in du.service_instances():
            status_cmd = (
                f"sudo systemctl show {unit_name} "
                f"--property=ActiveState,SubState,LoadState,ActiveEnterTimestamp --no-pager"
            )
            status_output, success = conn.run(status_cmd, warn=True, hide=True)

            props = {}
            if success:
                for line in status_output.strip().split("\n"):
                    if "=" in line:
                        key, value = line.split("=", 1)
                        props[key] = value

            instances.append(
                {
                    "name": unit_name,
                    "active_state": props.get("ActiveState", "unknown"),
                    "sub_state": props.get("SubState", "unknown"),
                    "load_state": props.get("LoadState", "unknown"),
                    "active_since": props.get("ActiveEnterTimestamp", ""),
                }
            )

        # Get unit file content
        unit_content, _ = conn.run(
            f"sudo systemctl cat {du.template_service_name} --no-pager 2>/dev/null || echo ''",
            warn=True,
            hide=True,
        )

        # Socket content
        socket_content = None
        if du.template_socket_name:
            socket_content, _ = conn.run(
                f"sudo systemctl cat {du.template_socket_name} --no-pager 2>/dev/null || echo ''",
                warn=True,
                hide=True,
            )

        # Timer content
        timer_content = None
        if du.template_timer_name:
            timer_content, _ = conn.run(
                f"sudo systemctl cat {du.template_timer_name} --no-pager 2>/dev/null || echo ''",
                warn=True,
                hide=True,
            )

    return JSONResponse(
        {
            "name": du.name,
            "is_template": du.is_template,
            "replicas": du.replicas,
            "template_service_name": du.template_service_name,
            "instances": instances,
            "unit_content": unit_content.strip() if unit_content else "",
            "socket_content": socket_content.strip() if socket_content else None,
            "timer_content": timer_content.strip() if timer_content else None,
        }
    )


async def api_service_action(request: Request) -> JSONResponse:
    """Execute start/stop/restart on a service."""
    config = _get_config()
    service_name = request.path_params["name"]
    body = await request.json()
    action = body.get("action")

    if action not in ("start", "stop", "restart"):
        return JSONResponse({"error": f"Invalid action: {action}"}, status_code=400)

    # Find the deployed unit
    du = None
    for unit in config.deployed_units:
        if unit.name == service_name:
            du = unit
            break

    if not du:
        return JSONResponse(
            {"error": f"Unknown service: {service_name}"}, status_code=404
        )

    units = du.service_instances()
    # For restart, use reload-or-restart unless it's a forced restart
    systemctl_action = action
    if action == "restart" and not body.get("force"):
        systemctl_action = "reload-or-restart"

    with _connect() as conn:
        _, success = conn.run(
            f"sudo systemctl {systemctl_action} {' '.join(units)}",
            warn=True,
            hide=True,
        )

    return JSONResponse(
        {
            "ok": success,
            "action": action,
            "service": service_name,
            "units": units,
        }
    )


async def api_config_caddy(request: Request) -> JSONResponse:
    """Get Caddyfile content."""
    config = _get_config()
    if not config.caddyfile_exists:
        return JSONResponse({"content": None, "exists": False})

    with _connect() as conn:
        output, success = conn.run(
            f"cat {config.caddy_config_path} 2>/dev/null || echo 'File not found on server'",
            warn=True,
            hide=True,
        )
    return JSONResponse({"content": output.strip(), "exists": True})


async def api_config_units(request: Request) -> JSONResponse:
    """Get systemd unit file contents."""
    config = _get_config()
    units = {}

    with _connect() as conn:
        for du in config.deployed_units:
            # Service file
            output, success = conn.run(
                f"sudo systemctl cat {du.template_service_name} --no-pager 2>/dev/null || echo 'Not found'",
                warn=True,
                hide=True,
            )
            units[du.template_service_name] = output.strip()

            # Socket file
            if du.template_socket_name:
                output, success = conn.run(
                    f"sudo systemctl cat {du.template_socket_name} --no-pager 2>/dev/null || echo 'Not found'",
                    warn=True,
                    hide=True,
                )
                units[du.template_socket_name] = output.strip()

            # Timer file
            if du.template_timer_name:
                output, success = conn.run(
                    f"sudo systemctl cat {du.template_timer_name} --no-pager 2>/dev/null || echo 'Not found'",
                    warn=True,
                    hide=True,
                )
                units[du.template_timer_name] = output.strip()

    return JSONResponse({"units": units})


async def api_logs(request: Request) -> JSONResponse:
    """Get recent logs."""
    config = _get_config()
    service = request.query_params.get("service")
    lines = int(request.query_params.get("lines", "100"))
    level = request.query_params.get("level")
    grep = request.query_params.get("grep")

    # Build unit list
    if service == "__caddy__":
        units = ["caddy.service"]
    elif service:
        units = []
        for du in config.deployed_units:
            if du.name == service:
                units = du.service_instances()
                break
        if not units:
            return JSONResponse({"logs": "", "error": f"Unknown service: {service}"})
    else:
        units = config.systemd_units

    if not units:
        return JSONResponse({"logs": "", "error": "No services found"})

    unit_args = " ".join(f"-u {n}" for n in units)
    cmd_parts = [
        "sudo journalctl",
        unit_args,
        f"-n {lines}",
        "--no-pager",
        "-o short-iso",
    ]
    if level:
        cmd_parts.append(f"-p {level}")
    if grep:
        cmd_parts.append(f"-g {shlex.quote(grep)}")

    with _connect() as conn:
        output, _ = conn.run(" ".join(cmd_parts), warn=True, hide=True)

    return JSONResponse({"logs": output.strip()})


async def api_logs_stream(request: Request) -> EventSourceResponse:
    """SSE endpoint for streaming logs."""
    config = _get_config()
    service = request.query_params.get("service")
    level = request.query_params.get("level")

    # Build unit list
    if service == "__caddy__":
        units = ["caddy.service"]
    elif service:
        units = []
        for du in config.deployed_units:
            if du.name == service:
                units = du.service_instances()
                break
        if not units:
            units = config.systemd_units
    else:
        units = config.systemd_units

    unit_args = " ".join(f"-u {n}" for n in units)

    async def event_generator() -> AsyncGenerator[dict, None]:
        import asyncio

        # Poll with cursor-based approach for new log entries
        last_cursor = ""
        with _connect() as conn:
            # Get initial cursor position
            cursor_output, _ = conn.run(
                "sudo journalctl --show-cursor -n 0 --no-pager -o short-iso "
                + unit_args
                + " 2>/dev/null | tail -1",
                warn=True,
                hide=True,
            )
            for line in cursor_output.strip().split("\n"):
                if line.startswith("-- cursor:"):
                    last_cursor = line.replace("-- cursor:", "").strip()

            while True:
                if await request.is_disconnected():
                    break

                # Poll for new entries since last cursor
                poll_cmd_parts = [
                    "sudo journalctl",
                    unit_args,
                    "--no-pager",
                    "-o short-iso",
                    "--show-cursor",
                ]
                if level:
                    poll_cmd_parts.append(f"-p {level}")
                if last_cursor:
                    poll_cmd_parts.append(f"--after-cursor={shlex.quote(last_cursor)}")
                else:
                    poll_cmd_parts.append("-n 0")

                output, _ = conn.run(" ".join(poll_cmd_parts), warn=True, hide=True)

                log_lines = output.strip().split("\n") if output.strip() else []

                # Extract cursor from last line
                new_lines = []
                for line in log_lines:
                    if line.startswith("-- cursor:"):
                        last_cursor = line.replace("-- cursor:", "").strip()
                    elif line.strip():
                        new_lines.append(line)

                if new_lines:
                    yield {"data": "\n".join(new_lines)}

                await asyncio.sleep(2)

    return EventSourceResponse(event_generator())


routes = [
    Route("/", homepage),
    Route("/api/hosts", api_hosts),
    Route("/api/hosts/switch", api_switch_host, methods=["POST"]),
    Route("/api/status", api_status),
    Route("/api/services/{name}", api_service_detail),
    Route("/api/services/{name}/action", api_service_action, methods=["POST"]),
    Route("/api/config/caddy", api_config_caddy),
    Route("/api/config/units", api_config_units),
    Route("/api/logs", api_logs),
    Route("/api/logs/stream", api_logs_stream),
]


def create_app(config: Config, host_name: str | None = None) -> Starlette:
    global _config, _host_name
    _config = config
    _host_name = host_name
    return Starlette(routes=routes)
