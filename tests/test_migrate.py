"""Tests for configuration migration command."""

from __future__ import annotations


import msgspec

from fujin.commands.migrate import migrate_config


# Test migrate_config function


def test_migrate_single_host_to_hosts_array():
    """Single host object should be converted to hosts array."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "host": {"address": "example.com", "user": "deploy"},
        "processes": {"web": {"command": "run"}},
    }

    migrated = migrate_config(old_config)

    assert "host" not in migrated
    assert "hosts" in migrated
    assert isinstance(migrated["hosts"], list)
    assert len(migrated["hosts"]) == 1
    assert migrated["hosts"][0]["address"] == "example.com"


def test_migrate_host_ip_to_address():
    """Host 'ip' field should be renamed to 'address'."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"ip": "192.168.1.1", "user": "deploy"}],
        "processes": {"web": {"command": "run"}},
    }

    migrated = migrate_config(old_config)

    assert "ip" not in migrated["hosts"][0]
    assert migrated["hosts"][0]["address"] == "192.168.1.1"


def test_migrate_host_domain_name_to_address():
    """Host 'domain_name' field should be renamed to 'address'."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"domain_name": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "run"}},
    }

    migrated = migrate_config(old_config)

    assert "domain_name" not in migrated["hosts"][0]
    assert migrated["hosts"][0]["address"] == "example.com"


def test_migrate_host_ssh_port_to_port():
    """Host 'ssh_port' field should be renamed to 'port'."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy", "ssh_port": 2222}],
        "processes": {"web": {"command": "run"}},
    }

    migrated = migrate_config(old_config)

    assert "ssh_port" not in migrated["hosts"][0]
    assert migrated["hosts"][0]["port"] == 2222


def test_migrate_simple_process_string_to_dict():
    """Simple process strings should be converted to dict with command."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"worker": "celery worker", "beat": "celery beat"},
    }

    migrated = migrate_config(old_config)

    assert isinstance(migrated["processes"]["worker"], dict)
    assert migrated["processes"]["worker"]["command"] == "celery worker"
    assert isinstance(migrated["processes"]["beat"], dict)
    assert migrated["processes"]["beat"]["command"] == "celery beat"


def test_migrate_web_process_gets_listen_from_webserver_upstream():
    """Web process should get listen field from webserver.upstream."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": "gunicorn app:app"},
        "webserver": {"upstream": "localhost:8000"},
    }

    migrated = migrate_config(old_config)

    assert migrated["processes"]["web"]["command"] == "gunicorn app:app"
    assert migrated["processes"]["web"]["listen"] == "localhost:8000"


def test_migrate_non_web_process_no_listen():
    """Non-web processes should not get listen field even with webserver.upstream."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": "gunicorn app:app", "worker": "celery worker"},
        "webserver": {"upstream": "localhost:8000"},
    }

    migrated = migrate_config(old_config)

    assert migrated["processes"]["web"]["listen"] == "localhost:8000"
    assert "listen" not in migrated["processes"]["worker"]


def test_migrate_web_process_dict_without_listen_gets_upstream():
    """Web process as dict without listen should get it from webserver.upstream."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {
            "web": {"command": "gunicorn app:app", "replicas": 1},
            "worker": {"command": "celery worker"},
        },
        "webserver": {"upstream": "unix//run/app/app.sock"},
    }

    migrated = migrate_config(old_config)

    # Web process should get listen from upstream
    assert migrated["processes"]["web"]["command"] == "gunicorn app:app"
    assert migrated["processes"]["web"]["listen"] == "unix//run/app/app.sock"
    assert migrated["processes"]["web"]["replicas"] == 1
    # Worker should not get listen
    assert "listen" not in migrated["processes"]["worker"]


def test_migrate_web_process_dict_with_existing_listen_preserved():
    """Web process with existing listen should not be overwritten."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {
            "web": {"command": "gunicorn app:app", "listen": "localhost:9000"},
        },
        "webserver": {"upstream": "localhost:8000"},
    }

    migrated = migrate_config(old_config)

    # Should preserve existing listen, not overwrite with upstream
    assert migrated["processes"]["web"]["listen"] == "localhost:9000"


def test_migrate_webserver_to_sites():
    """Webserver config should be converted to sites array."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "gunicorn", "listen": "localhost:8000"}},
        "webserver": {"upstream": "localhost:8000"},
    }

    migrated = migrate_config(old_config)

    assert "webserver" not in migrated
    assert "sites" in migrated
    assert len(migrated["sites"]) == 1
    assert migrated["sites"][0]["domains"] == ["example.com"]
    assert migrated["sites"][0]["routes"]["/*"] == "web"


def test_migrate_webserver_statics_to_static_routes():
    """Webserver statics should be converted to static routes."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "gunicorn", "listen": "localhost:8000"}},
        "webserver": {
            "upstream": "localhost:8000",
            "statics": {
                "/static/*": "{app_dir}/static/",
                "/media/*": "{app_dir}/media/",
            },
        },
    }

    migrated = migrate_config(old_config)

    routes = migrated["sites"][0]["routes"]
    assert routes["/static/*"] == {"static": "{app_dir}/static/"}
    assert routes["/media/*"] == {"static": "{app_dir}/media/"}
    assert routes["/*"] == "web"


def test_migrate_webserver_type_is_dropped():
    """Deprecated webserver.type field should be dropped."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "gunicorn", "listen": "localhost:8000"}},
        "webserver": {"type": "caddy", "upstream": "localhost:8000"},
    }

    migrated = migrate_config(old_config)

    assert "webserver" not in migrated
    # Type should not appear anywhere in migrated config
    assert "type" not in str(migrated)


def test_migrate_webserver_enabled_false_no_sites():
    """When webserver.enabled is False, don't create sites."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "gunicorn", "listen": "localhost:8000"}},
        "webserver": {"enabled": False, "upstream": "localhost:8000"},
    }

    migrated = migrate_config(old_config)

    # Should not create sites when enabled was False
    assert "sites" not in migrated
    # But webserver should still be removed
    assert "webserver" not in migrated


def test_migrate_webserver_enabled_true_creates_sites():
    """When webserver.enabled is True (or omitted), create sites."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "gunicorn", "listen": "localhost:8000"}},
        "webserver": {"enabled": True, "upstream": "localhost:8000"},
    }

    migrated = migrate_config(old_config)

    # Should create sites when enabled was True
    assert "sites" in migrated
    assert migrated["sites"][0]["domains"] == ["example.com"]
    assert migrated["sites"][0]["routes"]["/*"] == "web"


def test_migrate_preserves_existing_sites():
    """If sites already exists, don't create from webserver."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "gunicorn", "listen": "localhost:8000"}},
        "sites": [{"domains": ["custom.com"], "routes": {"/": "web"}}],
        "webserver": {"upstream": "localhost:8000"},
    }

    migrated = migrate_config(old_config)

    # Should preserve existing sites, not create new one (but still migrate "/" to "/*")
    assert len(migrated["sites"]) == 1
    assert migrated["sites"][0]["domains"] == ["custom.com"]
    assert migrated["sites"][0]["routes"]["/*"] == "web"


def test_migrate_no_changes_returns_same_config():
    """Config already in new format should not be changed."""
    new_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy", "port": 22}],
        "processes": {"web": {"command": "gunicorn", "listen": "localhost:8000"}},
        "sites": [{"domains": ["example.com"], "routes": {"/*": "web"}}],
    }

    migrated = migrate_config(new_config)

    assert migrated == new_config


def test_migrate_combined_scenario():
    """Test a complex migration with multiple old format features."""
    old_config = {
        "app": "myapp",
        "build_command": "uv build",
        "distfile": "dist/myapp.whl",
        "installation_mode": "python-package",
        "python_version": "3.12",
        "host": {"domain_name": "myapp.com", "user": "deploy", "ssh_port": 2222},
        "processes": {
            "web": "gunicorn myapp.wsgi:application",
            "worker": "celery -A myapp worker",
        },
        "webserver": {
            "type": "caddy",
            "upstream": "unix//run/myapp/myapp.sock",
            "statics": {"/static/*": "{app_dir}/static/"},
        },
    }

    migrated = migrate_config(old_config)

    # Host migrations
    assert "host" not in migrated
    assert len(migrated["hosts"]) == 1
    assert migrated["hosts"][0]["address"] == "myapp.com"
    assert migrated["hosts"][0]["port"] == 2222
    assert "domain_name" not in migrated["hosts"][0]
    assert "ssh_port" not in migrated["hosts"][0]

    # Process migrations
    assert migrated["processes"]["web"]["command"] == "gunicorn myapp.wsgi:application"
    assert migrated["processes"]["web"]["listen"] == "unix//run/myapp/myapp.sock"
    assert migrated["processes"]["worker"]["command"] == "celery -A myapp worker"
    assert "listen" not in migrated["processes"]["worker"]

    # Webserver → Sites migration
    assert "webserver" not in migrated
    assert len(migrated["sites"]) == 1
    assert migrated["sites"][0]["domains"] == ["myapp.com"]
    assert migrated["sites"][0]["routes"]["/static/*"] == {
        "static": "{app_dir}/static/"
    }
    assert migrated["sites"][0]["routes"]["/*"] == "web"


def test_migrate_does_not_mutate_original():
    """Migration should not mutate the original config dict."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "host": {"address": "example.com", "user": "deploy"},
        "processes": {"web": "gunicorn"},
        "webserver": {"upstream": "localhost:8000"},
    }

    original_copy = dict(old_config)
    migrate_config(old_config)

    # Original should be unchanged
    assert old_config == original_copy
    assert "host" in old_config
    assert "hosts" not in old_config


# Integration tests with Migrate command


def test_migrate_command_creates_backup(tmp_path, monkeypatch):
    """Migrate command should create backup when --backup flag is used."""
    monkeypatch.chdir(tmp_path)

    old_toml = """
app = "myapp"
version = "1.0.0"
build_command = "true"
distfile = "app.whl"
installation_mode = "binary"

[host]
domain_name = "example.com"
user = "deploy"

[processes]
web = "gunicorn"

[webserver]
upstream = "localhost:8000"
"""

    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text(old_toml)

    import cappa

    from fujin.commands._base import MessageFormatter
    from fujin.commands.migrate import Migrate

    migrate = Migrate(backup=True, dry_run=False)
    migrate.output = MessageFormatter(cappa.Output())
    migrate()

    # Check backup was created
    backup = tmp_path / "fujin.toml.backup"
    assert backup.exists()
    assert backup.read_text() == old_toml


def test_migrate_command_dry_run_does_not_write(tmp_path, monkeypatch):
    """Migrate command with --dry-run should not write changes."""
    monkeypatch.chdir(tmp_path)

    old_toml = """
app = "myapp"
build_command = "true"
distfile = "app.whl"
installation_mode = "binary"

[host]
ip = "192.168.1.1"
user = "deploy"

[processes]
web = "gunicorn"
"""

    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text(old_toml)
    original_content = fujin_toml.read_text()

    import cappa

    from fujin.commands._base import MessageFormatter
    from fujin.commands.migrate import Migrate

    migrate = Migrate(backup=False, dry_run=True)
    migrate.output = MessageFormatter(cappa.Output())
    migrate()

    # File should be unchanged
    assert fujin_toml.read_text() == original_content
    # No backup should be created
    assert not (tmp_path / "fujin.toml.backup").exists()


def test_migrate_command_validates_new_config(tmp_path, monkeypatch):
    """Migrate command should attempt to validate the new config."""
    monkeypatch.chdir(tmp_path)

    # Valid old config that will become valid new config
    old_toml = """
app = "myapp"
version = "1.0.0"
build_command = "true"
distfile = "app.whl"
installation_mode = "binary"

[host]
address = "example.com"
user = "deploy"

[processes.web]
command = "gunicorn"
listen = "localhost:8000"

[webserver]
upstream = "localhost:8000"
"""

    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text(old_toml)

    import cappa

    from fujin.commands._base import MessageFormatter
    from fujin.commands.migrate import Migrate

    migrate = Migrate(backup=False, dry_run=False)
    migrate.output = MessageFormatter(cappa.Output())
    migrate()

    # Should have written new config
    new_config = msgspec.toml.decode(fujin_toml.read_text())
    assert "sites" in new_config
    assert "webserver" not in new_config


def test_migrate_config_without_hosts_uses_default_domain():
    """When migrating webserver without hosts, use example.com as default."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [],  # Empty hosts
        "processes": {"web": {"command": "gunicorn", "listen": "localhost:8000"}},
        "webserver": {"upstream": "localhost:8000"},
    }

    migrated = migrate_config(old_config)

    # Should not create sites if no hosts
    assert "sites" not in migrated
    # But webserver should still be removed
    assert "webserver" not in migrated


def test_migrate_alias_app_exec_to_exec_app():
    """Alias 'app exec ...' should be migrated to 'exec --app ...'."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "run"}},
        "aliases": {
            "shell": "app exec shell",
            "console": "app exec python manage.py shell",
        },
    }

    migrated = migrate_config(old_config)

    assert migrated["aliases"]["shell"] == "exec --app shell"
    assert migrated["aliases"]["console"] == "exec --app python manage.py shell"


def test_migrate_alias_server_exec_to_exec():
    """Alias 'server exec ...' should be migrated to 'exec ...'."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "run"}},
        "aliases": {
            "bash": "server exec bash",
            "check": "server exec systemctl status myapp",
        },
    }

    migrated = migrate_config(old_config)

    assert migrated["aliases"]["bash"] == "exec bash"
    assert migrated["aliases"]["check"] == "exec systemctl status myapp"


def test_migrate_alias_removes_i_option():
    """Alias with -i option should have it removed."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "run"}},
        "aliases": {
            "shell": "server exec -i bash",
            "console": "app exec -i python",
        },
    }

    migrated = migrate_config(old_config)

    # -i should be removed
    assert migrated["aliases"]["shell"] == "exec bash"
    assert migrated["aliases"]["console"] == "exec --app python"


def test_migrate_alias_combined_transformations():
    """Alias migration with both exec conversion and -i removal."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "run"}},
        "aliases": {
            "shell": "server exec --appenv -i bash",
            "django_shell": "app exec -i python manage.py shell",
        },
    }

    migrated = migrate_config(old_config)

    assert migrated["aliases"]["shell"] == "exec --appenv bash"
    assert migrated["aliases"]["django_shell"] == "exec --app python manage.py shell"


def test_migrate_alias_preserves_other_commands():
    """Alias migration should only affect 'app exec' and 'server exec'."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "run"}},
        "aliases": {
            "status": "app info",
            "logs": "app logs",
            "restart": "app restart",
            "shell": "app exec shell",
        },
    }

    migrated = migrate_config(old_config)

    # These should not be changed
    assert migrated["aliases"]["status"] == "app info"
    assert migrated["aliases"]["logs"] == "app logs"
    assert migrated["aliases"]["restart"] == "app restart"
    # But this one should
    assert migrated["aliases"]["shell"] == "exec --app shell"


def test_migrate_route_slash_to_slash_star():
    """Route '/' should be migrated to '/*' for proper Caddy matching."""
    old_config = {
        "app": "myapp",
        "build_command": "true",
        "distfile": "app.whl",
        "installation_mode": "binary",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "processes": {"web": {"command": "gunicorn", "listen": "localhost:8000"}},
        "sites": [{"domains": ["example.com"], "routes": {"/": "web"}}],
    }

    migrated = migrate_config(old_config)

    # "/" should be converted to "/*"
    assert "/" not in migrated["sites"][0]["routes"]
    assert migrated["sites"][0]["routes"]["/*"] == "web"
