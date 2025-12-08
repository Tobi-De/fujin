import pytest
import cappa
from inline_snapshot import snapshot
from fujin.commands.app import App
from fujin.config import ProcessConfig


def test_app_start_resolves_process_name(mock_connection, get_commands):
    app = App()
    app.start("web")
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo systemctl start testapp.service'
        ]
    )


def test_app_start_resolves_worker_replicas(mock_connection, get_commands):
    app = App()
    app.start("worker")
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo systemctl start testapp-worker@1.service testapp-worker@2.service'
        ]
    )


def test_app_start_fallback_to_service_name(mock_connection, get_commands):
    app = App()
    with pytest.raises(cappa.Exit) as exc:
        app.start("custom.service")
    assert "Unknown service 'custom.service'" in str(exc.value.message)


def test_resolve_socket_for_process(mock_config):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=True)
    app = App()
    units = app._resolve_active_systemd_units("web.socket")
    assert units == ["testapp.socket"]


def test_resolve_timer_for_process(mock_config):
    mock_config.processes["worker"] = ProcessConfig(command="run worker", timer="*:00")
    app = App()
    units = app._resolve_active_systemd_units("worker.timer")
    assert units == ["testapp-worker.timer"]


def test_resolve_socket_error_if_not_enabled(mock_config):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=False)
    app = App()
    with pytest.raises(cappa.Exit) as exc:
        app._resolve_active_systemd_units("web.socket")
    assert "Process 'web' does not have a socket enabled" in str(exc.value.message)


def test_resolve_timer_error_if_not_enabled(mock_config):
    mock_config.processes["worker"] = ProcessConfig(command="run worker", timer=None)
    app = App()
    with pytest.raises(cappa.Exit) as exc:
        app._resolve_active_systemd_units("worker.timer")
    assert "Process 'worker' does not have a timer enabled" in str(exc.value.message)


def test_resolve_unknown_process_fallback(mock_config):
    app = App()
    with pytest.raises(cappa.Exit) as exc:
        app._resolve_active_systemd_units("unknown.socket")
    assert "Unknown service 'unknown.socket'" in str(exc.value.message)


def test_resolve_legacy_keywords(mock_config):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=True)
    mock_config.processes["worker"] = ProcessConfig(command="run worker", timer="*:00")
    app = App()

    assert app._resolve_active_systemd_units("socket") == ["testapp.socket"]
    # timer returns all timers
    assert "testapp-worker.timer" in app._resolve_active_systemd_units("timer")


def test_app_cat_includes_socket(mock_connection, get_commands, mock_config):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=True)
    app = App()
    app.cat("web")
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo systemctl cat testapp.service testapp.socket'
        ]
    )


def test_app_cat_includes_timer(mock_connection, get_commands, mock_config):
    mock_config.processes["worker"] = ProcessConfig(command="run worker", timer="*:00")
    app = App()
    app.cat("worker")
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo systemctl cat testapp-worker.service testapp-worker.timer'
        ]
    )


def test_resolve_service_suffix(mock_config):
    mock_config.app_name = "testapp"
    mock_config.processes["health"] = ProcessConfig(command="run health")
    app = App()

    # This currently returns ["health.service"] which is wrong, should be ["testapp-health.service"]
    units = app._resolve_active_systemd_units("health.service")
    assert units == ["testapp-health.service"]


from fujin.commands.app import App
from fujin.config import ProcessConfig
from inline_snapshot import snapshot


def test_app_cat_specific_service_excludes_extras(
    mock_connection, get_commands, mock_config
):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=True)
    app = App()
    app.cat("web.service")
    # Should ONLY be testapp.service, NOT testapp.socket
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo systemctl cat testapp.service'
        ]
    )


def test_app_cat_process_name_includes_extras(
    mock_connection, get_commands, mock_config
):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=True)
    app = App()
    app.cat("web")
    # Should include testapp.socket
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo systemctl cat testapp.service testapp.socket'
        ]
    )


def test_app_logs_process_name_includes_extras(
    mock_connection, get_commands, mock_config
):
    mock_config.processes["worker"] = ProcessConfig(command="run worker", timer="*:00")
    app = App()
    app.logs("worker")
    # Should include testapp-worker.timer
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo journalctl -u testapp-worker.service -u testapp-worker.timer -n 50 '
        ]
    )


def test_app_logs_specific_service_excludes_extras(
    mock_connection, get_commands, mock_config
):
    mock_config.processes["worker"] = ProcessConfig(command="run worker", timer="*:00")
    app = App()
    app.logs("worker.service")
    # Should ONLY be testapp-worker.service
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo journalctl -u testapp-worker.service -n 50 '
        ]
    )


import pytest
import cappa
from fujin.commands.app import App
from fujin.config import ProcessConfig


def test_resolve_unknown_service_fails(mock_config):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=True)
    mock_config.processes["worker"] = ProcessConfig(command="run worker")

    app = App()

    with pytest.raises(cappa.Exit) as exc:
        app._resolve_active_systemd_units("unknown")

    msg = str(exc.value.message)
    assert "Unknown service 'unknown'" in msg
    assert "web, web.service, web.socket" in msg
    assert "worker, worker.service" in msg


def test_resolve_unknown_service_fails_no_socket(mock_config):
    mock_config.processes["web"] = ProcessConfig(command="run web")

    app = App()

    with pytest.raises(cappa.Exit) as exc:
        app._resolve_active_systemd_units("unknown")

    msg = str(exc.value.message)
    assert "Unknown service 'unknown'" in msg
    assert "web, web.service" in msg
    assert "socket" not in msg


def test_resolve_unknown_service_fails_only_socket(mock_config):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=True)
    mock_config.processes["worker"] = ProcessConfig(command="run worker")

    app = App()

    with pytest.raises(cappa.Exit) as exc:
        app._resolve_active_systemd_units("unknown")

    msg = str(exc.value.message)
    options_list = msg.split(": ")[1].split(", ")
    assert "socket" in options_list
    assert "timer" not in options_list


def test_resolve_unknown_service_fails_expanded_options(mock_config):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=True)
    mock_config.processes["worker"] = ProcessConfig(command="run worker", timer="*:00")

    app = App()

    with pytest.raises(cappa.Exit) as exc:
        app._resolve_active_systemd_units("unknown")

    msg = str(exc.value.message)
    assert "Unknown service 'unknown'" in msg
    # Check for expanded options
    assert "web, web.service, web.socket" in msg
    assert "worker, worker.service, worker.timer" in msg
    options_list = msg.split(": ")[1].split(", ")
    assert "socket" in options_list
    assert "timer" in options_list


def test_app_start_includes_socket(mock_connection, get_commands, mock_config):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=True)
    app = App()
    app.start("web")
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo systemctl start testapp.service testapp.socket'
        ]
    )


def test_app_start_includes_timer(mock_connection, get_commands, mock_config):
    mock_config.processes["worker"] = ProcessConfig(command="run worker", timer="*:00")
    app = App()
    app.start("worker")
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo systemctl start testapp-worker.service testapp-worker.timer'
        ]
    )


def test_app_start_specific_service_excludes_extras(
    mock_connection, get_commands, mock_config
):
    mock_config.processes["web"] = ProcessConfig(command="run web", socket=True)
    app = App()
    app.start("web.service")
    assert get_commands(mock_connection.mock_calls) == snapshot(
        [
            'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo systemctl start testapp.service'
        ]
    )
