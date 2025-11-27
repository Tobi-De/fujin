import pytest
from inline_snapshot import snapshot
from fujin.commands.app import App


def test_app_start_resolves_process_name(mock_connection, get_commands):
    app = App()
    app.start("web")
    assert get_commands(mock_connection.mock_calls) == snapshot(
        ["sudo systemctl start testapp.service"]
    )


def test_app_start_resolves_worker_replicas(mock_connection, get_commands):
    app = App()
    app.start("worker")
    assert get_commands(mock_connection.mock_calls) == snapshot(
        ["sudo systemctl start testapp-worker@1.service testapp-worker@2.service"]
    )


def test_app_start_fallback_to_service_name(mock_connection, get_commands):
    app = App()
    app.start("custom.service")
    assert get_commands(mock_connection.mock_calls) == snapshot(
        ["sudo systemctl start custom.service"]
    )
