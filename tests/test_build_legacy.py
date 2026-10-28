"""Legacy ``.install`` reference handling in the build (beta 2).

0.27 wrote units with ``{install_dir}/.venv/...`` and
``EnvironmentFile={install_dir}/.env``. An upgraded host keeps ``.install``,
so deploying such a unit verbatim silently keeps running the old venv. The
build rewrites the known references to the release tree and fails closed on
anything else, before any SSH I/O.
"""

from __future__ import annotations

import pytest

import fujin.build as build
from fujin.config import InstallationMode
from fujin.errors import ImproperlyConfiguredError


def _write_unit(config, content: str, name: str = "web.service"):
    systemd = config.local_config_dir / "systemd"
    systemd.mkdir(parents=True, exist_ok=True)
    path = systemd / name
    path.write_text(content)
    return path


def test_legacy_template_refs_rewritten_to_release_tree(minimal_config):
    unit = _write_unit(
        minimal_config,
        "[Service]\n"
        "EnvironmentFile={install_dir}/.env\n"
        "ExecStart={install_dir}/.venv/bin/python -m testapp\n",
    )

    pre = build.prebuild(minimal_config)

    rendered = pre.unit_tree["systemd/testapp-web.service"]
    assert "EnvironmentFile=/opt/fujin/testapp/current/.env" in rendered
    assert "ExecStart=/opt/fujin/testapp/current/venv/bin/python" in rendered
    assert ".install" not in rendered
    # The rewrite is reported back so deploy can warn once, before transfer.
    assert pre.legacy_rewrites == (str(unit),)


def test_legacy_literal_env_in_dropin_is_rewritten(minimal_config):
    _write_unit(minimal_config, "[Service]\nType=simple\n")
    dropin = minimal_config.local_config_dir / "systemd" / "web.service.d" / "env.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("[Service]\nEnvironmentFile=/opt/fujin/testapp/.install/.env\n")

    pre = build.prebuild(minimal_config)

    rendered = pre.unit_tree["systemd/testapp-web.service.d/env.conf"]
    assert "EnvironmentFile=/opt/fujin/testapp/current/.env" in rendered
    assert pre.legacy_rewrites == (str(dropin),)


def test_binary_install_path_rewritten(minimal_config):
    minimal_config.installation_mode = InstallationMode.BINARY
    unit = _write_unit(
        minimal_config,
        "[Service]\n"
        "ExecStart=/opt/fujin/testapp/.install/testapp serve\n"
        "ExecReload={install_dir}/testapp reload\n",
    )

    pre = build.prebuild(minimal_config)

    rendered = pre.unit_tree["systemd/testapp-web.service"]
    assert "ExecStart=/opt/fujin/testapp/current/bin/testapp serve" in rendered
    assert "ExecReload=/opt/fujin/testapp/current/bin/testapp reload" in rendered
    assert pre.legacy_rewrites == (str(unit),)


def test_binary_prefix_rewrite_keeps_longer_app_names(minimal_config):
    """``.install/testapp-worker`` is not the app binary and stays unknown."""
    minimal_config.installation_mode = InstallationMode.BINARY
    _write_unit(
        minimal_config,
        "[Service]\nExecStart=/opt/fujin/testapp/.install/testapp-worker run\n",
    )

    with pytest.raises(ImproperlyConfiguredError):
        build.prebuild(minimal_config)


def test_unknown_install_reference_fails_closed(minimal_config):
    _write_unit(
        minimal_config,
        "[Service]\nExecStart={install_dir}/data/tool run\n",
    )

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        build.prebuild(minimal_config)

    message = exc_info.value.message
    assert "web.service" in message
    assert "{app_dir}/current/venv" in message
    assert "{app_dir}/current/bin/<app>" in message
    assert "{shared_dir}" in message


def test_caddyfile_install_reference_fails_closed(minimal_config):
    _write_unit(minimal_config, "[Service]\nType=simple\n")
    minimal_config.caddyfile_path.write_text(
        "example.com {\n  root * /opt/fujin/testapp/.install/static\n}\n"
    )

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        build.prebuild(minimal_config)

    assert "Caddyfile" in exc_info.value.message
