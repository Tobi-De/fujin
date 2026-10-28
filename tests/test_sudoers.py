"""Tests for the scoped sudoers rule (issue #93)."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fujin import sudoers

SRC = Path(__file__).resolve().parents[1] / "src" / "fujin"

DEPLOY_USER = "deploy"
APP_NAME = "testapp"
APP_USER = "testapp"


def _rule() -> str:
    return sudoers.render_rule(DEPLOY_USER, APP_USER, APP_NAME)


def _specs() -> list[str]:
    return sudoers.deploy_commands(APP_NAME, DEPLOY_USER, APP_USER)


def _binary(spec: str) -> str:
    return spec.split()[0].rsplit("/", 1)[-1]


# ============================================================================
# Rendering
# ============================================================================


def test_render_rule_never_grants_nopasswd_all():
    rule = _rule()
    assert not re.search(r"NOPASSWD:\s*ALL", rule)
    assert "ALL=(ALL)" not in rule
    assert "deploy ALL=(root) NOPASSWD: FUJIN_DEPLOY" in rule


def test_render_rule_has_no_interpreter_editor_or_decoder():
    rule = _rule()
    for banned in ("visudo", "python3", "base64"):
        assert banned not in rule, banned
        assert not any(banned in spec for spec in _specs()), banned


def test_render_rule_has_app_user_runas_entry():
    rule = _rule()
    assert "deploy ALL=(testapp) NOPASSWD: FUJIN_DEPLOY_APP" in rule
    alias = next(
        line
        for line in rule.splitlines()
        if line.startswith("Cmnd_Alias FUJIN_DEPLOY_APP = ")
    )
    assert "/usr/bin/bash" in alias
    assert "/usr/bin/systemctl" not in alias

    root_union = next(
        line
        for line in rule.splitlines()
        if line.startswith("Cmnd_Alias FUJIN_DEPLOY =")
    )
    root_aliases = {name.strip() for name in root_union.split("=", 1)[1].split(",")}
    assert "FUJIN_DEPLOY_APP" not in root_aliases
    assert not any("bash" in spec for spec in _specs())


def test_render_rule_enables_noninteractive_apt():
    assert 'Defaults:deploy env_keep += "DEBIAN_FRONTEND"' in _rule()


def test_render_rule_is_deterministic():
    assert _rule() == _rule()


def test_render_rule_covers_every_deploy_command():
    rule = _rule()
    for spec in _specs():
        assert spec.startswith("/"), f"command spec must be absolute: {spec}"
        assert spec in rule, spec


def test_systemctl_is_limited_to_app_units_and_known_verbs():
    for spec in _specs():
        if _binary(spec) != "systemctl":
            continue
        args = spec.split(None, 1)[1]
        assert args in ("daemon-reload", "reload caddy") or any(
            args.startswith(verb)
            for verb in (
                "start ",
                "stop ",
                "restart ",
                "reload-or-restart ",
                "enable ",
                "disable ",
                "reset-failed ",
                "is-active ",
                "is-failed ",
                "status ",
            )
        ), spec
        if args not in ("daemon-reload", "reload caddy"):
            assert f"{APP_NAME}-" in args, spec


def test_file_commands_are_path_restricted_to_the_app():
    file_commands = {
        "tee",
        "rm",
        "mv",
        "cp",
        "chown",
        "chgrp",
        "chmod",
        "find",
        "touch",
        "mkdir",
        "install",
        "cat",
    }
    allowed_roots = (
        f"/opt/fujin/{APP_NAME}",
        f"/etc/systemd/system/{APP_NAME}",
        "/etc/systemd/system/",
        f"/etc/caddy/conf.d/{APP_NAME}.caddy",
        f"/run/{APP_NAME}",
        "/opt/fujin/.audit",
    )
    seen = set()
    for spec in _specs():
        binary = _binary(spec)
        if binary not in file_commands:
            continue
        seen.add(binary)
        args = spec.split(None, 1)[1]
        assert any(root in args for root in allowed_roots), spec
    # The commands the deploy flow actually uses are all present and checked.
    assert {
        "tee",
        "rm",
        "mv",
        "chown",
        "chgrp",
        "chmod",
        "find",
        "touch",
        "mkdir",
    } <= seen


def test_rule_grants_mkdir_only_for_dropin_directories_and_app_tree():
    # ``install_units`` creates ``/etc/systemd/system/<unit>.d`` before the
    # first dropin write, so that exact shape must be granted (both usr-merge
    # directories) and nothing broader.
    specs = _specs()
    for directory in ("/usr/bin", "/bin"):
        assert f"{directory}/mkdir -p /etc/systemd/system/{APP_NAME}*" in specs
        assert f"{directory}/mkdir -p /opt/fujin/{APP_NAME}*" in specs
    mkdir_specs = [spec for spec in specs if _binary(spec) == "mkdir"]
    allowed = {
        f"/{prefix}/mkdir -p {root}{APP_NAME}*"
        for prefix in ("usr/bin", "bin")
        for root in ("/opt/fujin/", "/etc/systemd/system/")
    } | {f"/{prefix}/mkdir -p /opt/fujin/.audit" for prefix in ("usr/bin", "bin")}
    assert set(mkdir_specs) <= allowed


def test_rule_is_rendered_for_the_app_name():
    rule = sudoers.render_rule(DEPLOY_USER, APP_USER, "otherapp")
    assert "/opt/fujin/otherapp" in rule
    assert "/etc/systemd/system/otherapp" in rule
    assert "/etc/caddy/conf.d/otherapp.caddy" in rule
    assert f"/opt/fujin/{APP_NAME}" not in rule


def test_render_rule_rejects_unsafe_user_names():
    with pytest.raises(ValueError):
        sudoers.render_rule("deploy ALL=(ALL) NOPASSWD: ALL", APP_USER, APP_NAME)
    with pytest.raises(ValueError):
        sudoers.render_rule("bad user", APP_USER, APP_NAME)


def test_render_rule_rejects_dots_in_user_names():
    # sudo silently ignores includedir files whose name contains a dot, so a
    # `deploy.prod` drop-in would never be loaded.
    with pytest.raises(ValueError):
        sudoers.render_rule("deploy.prod", APP_USER, APP_NAME)
    with pytest.raises(ValueError):
        sudoers.render_rule(".deploy", APP_USER, APP_NAME)
    with pytest.raises(ValueError):
        sudoers.DROPIN_PATH("deploy.prod")


def test_render_rule_rejects_unsafe_app_names():
    for name in ("bad app", "app,name", "app*", "app:name", "app=name"):
        with pytest.raises(ValueError):
            sudoers.render_rule(DEPLOY_USER, APP_USER, name)


def test_dropin_path():
    assert sudoers.DROPIN_PATH("deploy") == "/etc/sudoers.d/fujin-deploy"


def test_rendered_rule_passes_visudo_syntax_check(tmp_path):
    """The rendered drop-in must be loadable, including escaped arguments."""
    visudo = shutil.which("visudo")
    if visudo is None:
        pytest.skip("visudo not installed")
    rule_path = tmp_path / "fujin-deploy"
    rule_path.write_text(_rule())
    result = subprocess.run(
        [visudo, "-c", "-f", str(rule_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ============================================================================
# Root scripts
# ============================================================================


def test_install_dropin_script_validates_before_installing():
    script = sudoers.install_dropin_script(DEPLOY_USER, _rule())
    assert "/tmp" not in script
    assert "mktemp /etc/sudoers.d/.fujin-new-" in script
    assert "install -m 0440 -o root -g root" in script
    assert 'install -m 0440 -o root -g root "$TMP" "$DROPIN"' in script
    assert "trap cleanup EXIT" in script
    assert "mktemp /etc/sudoers.d/.fujin-backup-" in script
    assert 'mv -f "$DROPIN_BAK" "$DROPIN"' in script
    assert "visudo -c >/dev/null" in script
    assert sudoers.DROPIN_OK in script
    assert sudoers.DROPIN_FAILED in script
    # Validation of the rule strictly precedes the install; the final
    # configuration validation follows it.
    assert script.index("visudo -c -f") < script.index("install -m 0440")
    assert script.index("install -m 0440") < script.rindex("visudo -c >/dev/null")


def test_install_dropin_script_checks_the_include_server_side():
    script = sudoers.install_dropin_script(DEPLOY_USER, _rule())
    check = script.index("includedir")
    assert check < script.index("install -m 0440")


def test_harden_script_is_one_transaction_with_restore_trap():
    script = sudoers.harden_script(DEPLOY_USER, APP_USER, APP_NAME, _rule())
    assert "/tmp" not in script
    assert "trap cleanup EXIT" in script
    # Restores both files on failure and re-validates.
    assert 'cp -a "$BACKUP" "$SUDOERS"' in script
    assert 'mv -f "$DROPIN_BAK" "$DROPIN"' in script
    assert 'rm -f "$DROPIN"' in script
    assert script.count("visudo -c") >= 3
    assert sudoers.HARDEN_OK in script
    assert sudoers.HARDEN_FAILED in script


def test_harden_script_backs_up_only_around_a_rewrite():
    script = sudoers.harden_script(DEPLOY_USER, APP_USER, APP_NAME, _rule())
    install_at = script.index('install -m 0440 -o root -g root "$TMP" "$DROPIN"')
    guard_at = script.index("if grep -Eq")
    backup_at = script.index('BACKUP="$SUDOERS.fujin-backup-')
    assert guard_at < backup_at
    assert install_at < guard_at


def test_harden_script_removes_only_the_exact_legacy_line():
    script = sudoers.harden_script(DEPLOY_USER, APP_USER, APP_NAME, _rule())
    assert "sed -E" in script
    # The pattern is embedded once for the guard and once for sed; both are
    # anchored and only match the deploy user's whole-line rule.
    patterns = re.findall(r"\^deploy\[\[:space:\]\].*?\$", script)
    assert len(patterns) >= 1
    for pattern in patterns:
        assert pattern.startswith("^deploy")
        assert "NOPASSWD:" in pattern


def test_harden_script_verifies_sudo_listing_and_absence_of_nopasswd_all():
    script = sudoers.harden_script(DEPLOY_USER, APP_USER, APP_NAME, _rule())
    assert "sudo -l -U deploy" in script
    assert "grep -q 'FUJIN_DEPLOY'" in script
    assert "grep -q 'NOPASSWD: ALL'" in script


# ============================================================================
# Coverage guard: every sudo call in the deploy path must be in the alias
# ============================================================================


class SudoScanError(RuntimeError):
    """A sudo call the rule cannot be reasoned about statically."""


#: Modules executed as the deploy user during deploy/rollback/app/doctor/
#: down/audit.  Administrative modules (commands/server.py bootstrap and
#: create-user, caddy.py installers, migrate) require full sudo and are not
#: covered by the scoped rule; see the module docstring.
DEPLOY_MODULES = (
    "serverops.py",
    "audit.py",
    "commands/app.py",
    "commands/deploy.py",
    "commands/rollback.py",
    "commands/down.py",
    "commands/doctor.py",
)

_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _string_constants(tree: ast.AST) -> list[str]:
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    strings: list[str] = []
    joined_parts: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            joined_parts.update(id(value) for value in node.values)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and id(node) not in joined_parts
        ):
            strings.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(str(value.value))
                else:
                    parts.append("{}")
            strings.append("".join(parts))
    return strings


def _sudo_invocations(source: str) -> set[tuple[str, str | None]]:
    """Extract ``(binary, runas_user)`` for every literal ``sudo`` call.

    Fails instead of skipping when the command cannot be identified: an
    environment assignment after ``sudo`` or an interpolated command would
    make the rule unauditable.
    """
    found: set[tuple[str, str | None]] = set()
    for text in _string_constants(ast.parse(source)):
        for match in re.finditer(r"(?<![\w.-])sudo\s+", text):
            tokens = text[match.end() :].split()
            index = 0
            runas: str | None = None
            while index < len(tokens):
                token = tokens[index].strip("'\"")
                if token in ("-u", "--user", "-g", "--group"):
                    if index + 1 >= len(tokens):
                        raise SudoScanError(f"sudo option without value: {text!r}")
                    runas = tokens[index + 1].strip("'\"")
                    index += 2
                    continue
                if token.startswith("-"):
                    index += 1
                    continue
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
                    raise SudoScanError(
                        "environment assignment after sudo is not allowed: "
                        f"{text[match.start() : match.start() + 100]!r}"
                    )
                binary = token.strip("'\"")
                if not binary or any(ch in binary for ch in "{}()$"):
                    raise SudoScanError(
                        f"non-literal sudo command: {binary!r} in {text!r}"
                    )
                if "/" in binary and not binary.startswith("/"):
                    raise SudoScanError(f"relative sudo command path: {binary!r}")
                if "/" not in binary and not re.match(r"^[A-Za-z0-9_+-]+$", binary):
                    raise SudoScanError(f"unexpected sudo command token: {binary!r}")
                found.add((binary, runas))
                break
            else:
                raise SudoScanError(f"sudo without a command: {text!r}")
    return found


def _candidate_paths(binary: str) -> set[str]:
    if binary.startswith("/"):
        return {binary}
    return {f"{directory}/{binary}" for directory in _BIN_DIRS}


def _spec_paths(specs: list[str]) -> set[str]:
    return {spec.split()[0] for spec in specs}


def _missing_sudo_commands(source: str, root_specs: set[str], app_specs: set[str]):
    missing: set[tuple[str, str | None]] = set()
    for binary, runas in _sudo_invocations(source):
        allowed = app_specs if runas is not None else root_specs
        # Compare the full command path: granting /usr/bin/<name> must not
        # silently cover a binary resolved from another directory.
        if not (_candidate_paths(binary) & allowed):
            missing.add((binary, runas))
    return missing


def test_every_sudo_command_is_covered_by_the_rule():
    root_specs = _spec_paths(_specs())
    app_specs = _spec_paths(sudoers.app_commands())

    missing: dict[str, set[tuple[str, str | None]]] = {}
    seen = 0
    for module in DEPLOY_MODULES:
        source = (SRC / module).read_text()
        invocations = _sudo_invocations(source)
        seen += len(invocations)
        absent = _missing_sudo_commands(source, root_specs, app_specs)
        if absent:
            missing[module] = absent

    assert seen >= 25, "sudo scanner found suspiciously few invocations"
    assert not missing, f"sudo commands missing from the scoped rule: {missing}"


def test_scanner_rejects_env_assignment_after_sudo():
    source = 'x = "sudo DEBIAN_FRONTEND=noninteractive apt-get update"\n'
    with pytest.raises(SudoScanError):
        _sudo_invocations(source)


def test_scanner_rejects_non_literal_command():
    source = 'x = f"sudo {command} --now"\n'
    with pytest.raises(SudoScanError):
        _sudo_invocations(source)


def test_scanner_resolves_full_command_paths():
    assert _candidate_paths("systemctl") == {
        "/usr/bin/systemctl",
        "/bin/systemctl",
        "/usr/sbin/systemctl",
        "/sbin/systemctl",
    }
    assert not (_candidate_paths("systemctl") & {"/usr/local/bin/systemctl"})


def test_app_user_impersonation_is_covered():
    invocations = _sudo_invocations((SRC / "commands" / "server.py").read_text())
    targets = {runas for _, runas in invocations if runas is not None}
    assert "{}" in targets  # f-string app user placeholder
    app_specs = _spec_paths(sudoers.app_commands())
    assert {binary for binary, runas in invocations if runas is not None} <= {"bash"}
    assert _candidate_paths("bash") & app_specs


# ============================================================================
# Pure helpers
# ============================================================================


def test_strip_nopasswd_all_removes_only_the_exact_line():
    content = (
        "root ALL=(ALL:ALL) ALL\n"
        "deploy ALL=(ALL) NOPASSWD:ALL\n"
        "%sudo ALL=(ALL:ALL) ALL\n"
    )
    new_content, removed = sudoers.strip_nopasswd_all(content, "deploy")
    assert removed is True
    assert "NOPASSWD:ALL" not in new_content
    assert "root ALL=(ALL:ALL) ALL" in new_content
    assert "%sudo ALL=(ALL:ALL) ALL" in new_content


def test_strip_nopasswd_all_ignores_other_users_and_forms():
    content = (
        "other ALL=(ALL) NOPASSWD:ALL\n"
        "deploy ALL=(ALL:ALL) NOPASSWD: ALL\n"
        "deploy ALL=(ALL) NOPASSWD:ALL,/bin/rm\n"
    )
    new_content, removed = sudoers.strip_nopasswd_all(content, "deploy")
    assert removed is True
    assert "other ALL=(ALL) NOPASSWD:ALL" in new_content
    assert "NOPASSWD:ALL,/bin/rm" in new_content


def test_strip_nopasswd_all_reports_absence():
    content = "root ALL=(ALL:ALL) ALL\n"
    new_content, removed = sudoers.strip_nopasswd_all(content, "deploy")
    assert new_content == content
    assert removed is False


def test_has_includedir_variants():
    assert sudoers.has_includedir("#includedir /etc/sudoers.d\n")
    assert sudoers.has_includedir("@includedir /etc/sudoers.d\n")
    assert sudoers.has_includedir("@includedir\t/etc/sudoers.d/\n")
    assert sudoers.has_includedir("#includedir /etc/sudoers.d # fujin\n")
    assert sudoers.has_includedir("#includedir /etc/sudoers.d/  # fujin\n")
    assert not sudoers.has_includedir("#includedir /etc/sudoers.d.bak\n")
    assert not sudoers.has_includedir("root ALL=(ALL:ALL) ALL\n")
