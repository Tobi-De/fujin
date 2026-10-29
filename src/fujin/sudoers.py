"""Scoped passwordless sudo rules for fujin deployment users.

Fujin used to grant deploying users ``NOPASSWD:ALL``.  Instead, a deployment
user gets a drop-in at :file:`/etc/sudoers.d/fujin-<user>` naming the exact
executables and argument shapes the deploy flow uses, plus a second Runas
entry that lets the deploy user impersonate the (unprivileged) app user::

    Defaults:<user> env_keep += "DEBIAN_FRONTEND"
    Cmnd_Alias FUJIN_DEPLOY = ...
    <user> ALL=(root) NOPASSWD: FUJIN_DEPLOY
    <user> ALL=(<app_user>) NOPASSWD: FUJIN_DEPLOY_APP

The command list is rendered for one app: file-mutating commands are limited
to that app's tree, its systemd units, its Caddy config and its runtime
directory, and ``systemctl`` is limited to unit-globbing verbs for
``<app>-*``.  There is no interpreter (``python3``), editor (``visudo``) or
generic stream-decoder (``base64``) grant.

Honesty note: this is *not* a security boundary against a malicious deploy
credential.  Writing ``/etc/systemd/system/<app>-*.service`` is equivalent to
executing code as root, so a stolen deploy credential remains effectively
privileged.  The rule removes the trivial ``sudo anything`` capability and
makes the normal deploy path least-privilege; it cannot make a credential
that installs units unprivileged.

Administrative actions are deliberately **not** covered by this rule and
must be run by an operator with full sudo: ``server bootstrap``,
``server create-user``, ``server harden``, ``server upgrade``'s package
installation is the exception (it is scoped, see ``packages`` below).

Every drop-in (and any rewrite of ``/etc/sudoers``) is produced inside a
single root shell script: unpredictable root-owned ``mktemp`` files inside
``/etc/sudoers.d`` (never ``/tmp``), ``visudo -c -f`` before installing,
``install -m 0440 -o root -g root``, a final ``visudo -c``, and a ``trap``
that restores the previous files on any failure.
"""

from __future__ import annotations

import base64
import re
import shlex
from collections.abc import Sequence
from typing import Final

from fujin.prerequisites import CORE_PACKAGES

DROPIN_DIR: Final = "/etc/sudoers.d"
SUDOERS_PATH: Final = "/etc/sudoers"
SALT: Final = "fujin"

DROPIN_OK: Final = "FUJIN_DROPIN_OK"
DROPIN_FAILED: Final = "FUJIN_DROPIN_FAILED"
HARDEN_OK: Final = "FUJIN_HARDEN_OK"
HARDEN_FAILED: Final = "FUJIN_HARDEN_FAILED"

#: Binaries that live in the sbin directories on Debian/Ubuntu.
_SBIN_BINS: Final = frozenset(
    {"groupadd", "useradd", "userdel", "usermod", "adduser", "chpasswd"}
)

#: Commands that may run as the *app* user via ``sudo -u``.  The app user is
#: unprivileged; this entry does not grant root.
APP_COMMANDS: Final[tuple[str, ...]] = (
    "/usr/bin/bash *",
    "/bin/bash *",
)

#: User names appear in the drop-in *file name*; sudo skips includedir files
#: whose name contains ``.`` or ends in ``~``.  Dots are rejected outright.
_USERNAME_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
#: App names appear inside sudoers command specs (paths and unit globs), so
#: reject whitespace, commas, colons, equals signs and wildcards.
_APPNAME_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_INCLUDEDIR_RE: Final = re.compile(
    r"^[ \t]*(?:#|@)includedir[ \t]+/etc/sudoers\.d/?[ \t]*(?:#.*)?$",
    re.MULTILINE,
)

#: Shell check that the main sudoers file actually loads the includedir.
#: A drop-in installed into an unread directory would simply be ignored.
_INCLUDEDIR_CHECK: Final = (
    "grep -Eq '^[[:space:]]*(#|@)includedir[[:space:]]+/etc/sudoers\\.d/?"
    '([[:space:]]|$)\' "$SUDOERS"'
)


def _bin_dirs(binary: str) -> tuple[str, str]:
    return ("/usr/sbin", "/sbin") if binary in _SBIN_BINS else ("/usr/bin", "/bin")


def _cmds(binary: str, *args: str) -> tuple[str, ...]:
    """The same command spec for the merged-usr and split-usr directories."""
    suffix = (" " + " ".join(args)) if args else ""
    return tuple(f"{directory}/{binary}{suffix}" for directory in _bin_dirs(binary))


def _apt_options() -> tuple[str, ...]:
    """Dpkg options escaped for sudoers, which treats ``:`` and ``=`` specially."""
    return (
        "-o",
        "Dpkg\\:\\:Options\\:\\=--force-confdef",
        "-o",
        "Dpkg\\:\\:Options\\:\\=--force-confold",
    )


def _package_commands(packages: Sequence[str]) -> tuple[str, ...]:
    """One exact ``apt-get install`` command per package name."""
    specs: list[str] = []
    seen: set[str] = set()
    for package in packages:
        if package in seen:
            continue
        seen.add(package)
        specs.extend(_cmds("apt-get", "-y", "-qq", *_apt_options(), "install", package))
    return tuple(specs)


def command_groups(
    app_name: str,
    user: str,
    app_user: str,
    *,
    packages: Sequence[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Command groups rendered for one app/user pair.

    Every file-mutating command is restricted to an app-owned path:
    ``/opt/fujin/<app>``, ``/etc/systemd/system/<app>``,
    ``/etc/caddy/conf.d/<app>.caddy`` or ``/run/<app>`` (the systemd runtime
    directory).

    ``packages`` is the exact apt package list granted through the ``packages``
    group; ``None`` keeps the core set fujin always needs.
    """
    _validate_app_name(app_name)
    _validate_user(user)
    _validate_app_name(app_user)
    if packages is None:
        packages = CORE_PACKAGES
    app = app_name
    app_dir = f"/opt/fujin/{app}"
    units = f"/etc/systemd/system/{app}"
    caddy = f"/etc/caddy/conf.d/{app}.caddy"
    audit = f"/opt/fujin/.audit/{app}.log"

    groups: dict[str, tuple[str, ...]] = {
        "systemd": (
            *_cmds("systemctl", "start", f"{app}-*"),
            *_cmds("systemctl", "stop", f"{app}-*"),
            *_cmds("systemctl", "restart", f"{app}-*"),
            *_cmds("systemctl", "restart", "--quiet", f"{app}-*"),
            *_cmds("systemctl", "reload-or-restart", f"{app}-*"),
            *_cmds("systemctl", "enable", f"{app}-*"),
            *_cmds("systemctl", "enable", "--now", f"{app}-*"),
            *_cmds("systemctl", "disable", f"{app}-*"),
            *_cmds("systemctl", "disable", "--now", f"{app}-*"),
            *_cmds("systemctl", "disable", "--now", "--quiet", f"{app}-*"),
            *_cmds("systemctl", "disable", "--quiet", f"{app}-*"),
            *_cmds("systemctl", "reset-failed", f"{app}-*"),
            *_cmds("systemctl", "is-active", f"{app}-*"),
            *_cmds("systemctl", "is-failed", f"{app}-*"),
            *_cmds("systemctl", "status", f"{app}-*"),
            *_cmds("systemctl", "daemon-reload"),
            *_cmds("systemctl", "reload", "caddy"),
            *_cmds("journalctl", "-u", f"{app}-*"),
        ),
        "app-files": (
            *_cmds("mkdir", "-p", f"{app_dir}*"),
            *_cmds("touch", f"{app_dir}*"),
            *_cmds("tee", f"{app_dir}*"),
            *_cmds("mv", f"{app_dir}*", f"{app_dir}*"),
            *_cmds("rm", "-f", f"{app_dir}*"),
            *_cmds("rm", "-rf", f"{app_dir}*"),
            *_cmds("chown", f"{user}\\:{app_user}", f"{app_dir}*"),
            *_cmds("chown", "-R", f"{user}\\:{app_user}", f"{app_dir}*"),
            *_cmds("chgrp", "-R", app_user, f"{app_dir}*"),
            *_cmds("chmod", "2755", f"{app_dir}*"),
            *_cmds("chmod", "2775", f"{app_dir}*"),
            *_cmds("chmod", "0664", f"{app_dir}*"),
            *_cmds("chmod", "0640", f"{app_dir}*"),
            *_cmds("chmod", "g+s", f"{app_dir}*"),
            *_cmds("find", f"{app_dir}*"),
        ),
        "unit-files": (
            # Dropin directories (``<unit>.d``) must exist before the first
            # file is written; ``/etc/systemd/system`` itself always exists.
            *_cmds("mkdir", "-p", f"{units}*"),
            *_cmds("tee", f"{units}*"),
            *_cmds("mv", f"{units}*", f"{units}*"),
            *_cmds("rm", "-rf", f"{units}*"),
            *_cmds("rm", "-f", f"/etc/systemd/system/*.wants/{app}*"),
        ),
        "caddy": (
            *_cmds("tee", f"{caddy}*"),
            *_cmds("mv", f"{caddy}*", f"{caddy}*"),
            *_cmds("cp", f"{caddy}*", f"{caddy}*"),
            *_cmds("rm", "-f", f"{caddy}*"),
            *_cmds("chown", "caddy\\:caddy", f"{caddy}*"),
            *_cmds("caddy", "validate", "--config", caddy, "--adapter", "caddyfile"),
            *_cmds("caddy", "upgrade"),
        ),
        "audit": (
            *_cmds("mkdir", "-p", "/opt/fujin/.audit"),
            *_cmds("tee", "-a", audit),
        ),
        "runtime": (*_cmds("chmod", "g+rx", f"/run/{app}*"),),
        "accounts": (
            *_cmds("groupadd", "-f", app_user),
            *_cmds(
                "useradd",
                "--system",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                "-g",
                app_user,
                app_user,
            ),
            *_cmds("userdel", app_user),
            *_cmds("pkill", "-u", app_user),
            *_cmds("pkill", "-9", "-u", app_user),
        ),
        # `fujin server upgrade` runs these as the deploy user.  The Dpkg
        # options are escaped for sudoers, which treats `:` and `=` as
        # special characters in command arguments.
        "packages": (
            *_cmds("apt-get", "update", "-qq"),
            *_cmds(
                "apt-get",
                "-y",
                "-qq",
                *_apt_options(),
                "upgrade",
            ),
            *_package_commands(packages),
        ),
    }
    return groups


def deploy_commands(
    app_name: str,
    user: str,
    app_user: str,
    *,
    packages: Sequence[str] | None = None,
) -> list[str]:
    """Every command spec the deploy user may run as root."""
    return [
        spec
        for group in command_groups(
            app_name, user, app_user, packages=packages
        ).values()
        for spec in group
    ]


def app_commands() -> list[str]:
    """Every command spec the deploy user may run as the app user."""
    return list(APP_COMMANDS)


def DROPIN_PATH(user: str) -> str:
    """Path of the scoped sudoers drop-in for ``user``."""
    _validate_user(user)
    return f"{DROPIN_DIR}/{SALT}-{user}"


def render_rule(
    user: str,
    app_user: str,
    app_name: str,
    *,
    packages: Sequence[str] | None = None,
) -> str:
    """Render the complete sudoers drop-in for ``user``.

    Deterministic: the same inputs always produce byte-identical output, so
    existing drop-ins can be diffed before being replaced. ``packages`` names
    the exact apt packages granted to the deploy user.
    """
    _validate_user(user)
    _validate_app_name(app_user)
    _validate_app_name(app_name)
    if packages is None:
        packages = CORE_PACKAGES

    lines = [
        f"# Managed by fujin. Scoped passwordless root commands for {user}.",
        "# Regenerate with `fujin server create-user` / `fujin server harden`.",
        f"# Inspect with `sudo -l -U {user}`.",
        "#",
        "# This credential can install systemd units, so it is effectively",
        "# privileged; it is scoped to make the normal path least-privilege,",
        "# not to contain a malicious deploy user.",
        "#",
        f'Defaults:{user} env_keep += "DEBIAN_FRONTEND"',
    ]
    alias_names: list[str] = []
    for group, specs in command_groups(
        app_name, user, app_user, packages=packages
    ).items():
        alias = f"FUJIN_DEPLOY_{group.replace('-', '_').upper()}"
        alias_names.append(alias)
        lines.extend(
            (
                f"# {group}",
                f"Cmnd_Alias {alias} = {', '.join(specs)}",
            )
        )
    lines.append(f"Cmnd_Alias FUJIN_DEPLOY = {', '.join(alias_names)}")
    lines.append(f"Cmnd_Alias FUJIN_DEPLOY_APP = {', '.join(APP_COMMANDS)}")
    lines.append("")
    lines.append(f"{user} ALL=(root) NOPASSWD: FUJIN_DEPLOY")
    lines.append(f"{user} ALL=({app_user}) NOPASSWD: FUJIN_DEPLOY_APP")
    return "\n".join(lines) + "\n"


def _write_payload(var: str, content: str) -> str:
    """Shell snippet decoding ``content`` into the shell variable ``$var``.

    ``base64`` here runs inside the root shell script (not as a scoped sudo
    command), so arbitrary content cannot break out of quoting.
    """
    encoded = base64.b64encode(content.encode()).decode("ascii")
    return f"printf '%s' '{encoded}' | base64 -d > \"${var}\""


def install_dropin_script(user: str, rule: str) -> str:
    """Root shell script installing the drop-in for ``user``.

    Transactional: the rule is written to an unpredictable root-owned
    ``mktemp`` file inside ``/etc/sudoers.d`` (not ``/tmp``), validated with
    ``visudo -c -f``, installed with ``install -m 0440 -o root -g root`` and
    the complete configuration is validated again.  On any failure a trap
    restores the previous drop-in (or removes the new one) and re-validates.
    """
    dropin = DROPIN_PATH(user)
    return f"""set -euo pipefail
umask 077
SUDOERS={shlex.quote(SUDOERS_PATH)}
DROPIN={shlex.quote(dropin)}
TMP=''
DROPIN_BAK=''
FAIL_REASON=error
cleanup() {{
    rc=$?
    rm -f "$TMP"
    if [ "$rc" -ne 0 ]; then
        if [ -n "$DROPIN_BAK" ] && [ -f "$DROPIN_BAK" ]; then
            mv -f "$DROPIN_BAK" "$DROPIN" || true
        else
            rm -f "$DROPIN" || true
        fi
        visudo -c >/dev/null 2>&1 || true
        echo "{DROPIN_FAILED} reason=$FAIL_REASON"
    else
        rm -f "$DROPIN_BAK"
        echo "{DROPIN_OK}"
    fi
    exit "$rc"
}}
trap cleanup EXIT
if ! {_INCLUDEDIR_CHECK}; then
    FAIL_REASON=includedir-missing
    exit 1
fi
if [ -f "$DROPIN" ]; then
    DROPIN_BAK=$(mktemp /etc/sudoers.d/.fujin-backup-XXXXXXXX)
    cp -a "$DROPIN" "$DROPIN_BAK"
fi
TMP=$(mktemp /etc/sudoers.d/.fujin-new-XXXXXXXX)
{_write_payload("TMP", rule)}
chmod 0440 "$TMP"
visudo -c -f "$TMP" >/dev/null
install -m 0440 -o root -g root "$TMP" "$DROPIN"
visudo -c >/dev/null
"""


def harden_script(
    user: str,
    app_user: str,
    app_name: str,
    rule: str,
) -> str:
    """Root shell script migrating a box from ``NOPASSWD:ALL`` to ``rule``.

    This is one transaction in one root shell:

    1. verify ``/etc/sudoers`` includes ``/etc/sudoers.d``;
    2. install the validated drop-in (previous one backed up);
    3. if the user's exact legacy line exists, back up ``/etc/sudoers`` and
       replace it with a copy filtered by an exact-line ``sed`` that passed
       ``visudo -c -f``;
    4. ``visudo -c`` the result and verify with ``sudo -l -U <user>`` that
       the alias is visible and no ``NOPASSWD: ALL`` rule remains;
    5. print ``FUJIN_HARDEN_OK`` or ``FUJIN_HARDEN_FAILED reason=...`` and,
       on any failure, restore both files from the in-script backups.

    The legacy line is matched inside the script against the *current*
    ``/etc/sudoers``, so a change between the client's dry-run read and this
    script cannot be half-rewritten.
    """
    _validate_user(user)
    _validate_app_name(app_user)
    _validate_app_name(app_name)
    dropin = DROPIN_PATH(user)
    # Exact whole-line legacy rule: `<user> ALL=(ALL) NOPASSWD:ALL` and the
    # `(ALL:ALL)` spelling.  User names are validated above, so embedding is
    # safe; the expression is quoted for both grep and sed.
    pattern = (
        rf"^{re.escape(user)}[[:space:]]+ALL=\(ALL(:ALL)?\)[[:space:]]+"
        r"NOPASSWD:[[:space:]]*ALL[[:space:]]*(#.*)?$"
    )
    # grep takes a bare regex; sed needs an address delimited by slashes.
    sed_address = f"/{pattern}/d"
    return f"""set -euo pipefail
umask 077
SUDOERS={shlex.quote(SUDOERS_PATH)}
DROPIN={shlex.quote(dropin)}
BACKUP=''
TMP=''
TMP_SUDOERS=''
DROPIN_BAK=''
FAIL_REASON=error
cleanup() {{
    rc=$?
    rm -f "$TMP" "$TMP_SUDOERS"
    if [ "$rc" -ne 0 ]; then
        if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
            cp -a "$BACKUP" "$SUDOERS" || true
        fi
        if [ -n "$DROPIN_BAK" ] && [ -f "$DROPIN_BAK" ]; then
            mv -f "$DROPIN_BAK" "$DROPIN" || true
        else
            rm -f "$DROPIN" || true
        fi
        visudo -c >/dev/null 2>&1 || true
        echo "{HARDEN_FAILED} reason=$FAIL_REASON"
    else
        rm -f "$DROPIN_BAK"
        echo "{HARDEN_OK} backup=${{BACKUP:-none}}"
    fi
    exit "$rc"
}}
trap cleanup EXIT
if ! {_INCLUDEDIR_CHECK}; then
    FAIL_REASON=includedir-missing
    exit 1
fi
if [ -f "$DROPIN" ]; then
    DROPIN_BAK=$(mktemp /etc/sudoers.d/.fujin-backup-XXXXXXXX)
    cp -a "$DROPIN" "$DROPIN_BAK"
fi
TMP=$(mktemp /etc/sudoers.d/.fujin-new-XXXXXXXX)
{_write_payload("TMP", rule)}
chmod 0440 "$TMP"
visudo -c -f "$TMP" >/dev/null
install -m 0440 -o root -g root "$TMP" "$DROPIN"
if grep -Eq {shlex.quote(pattern)} "$SUDOERS"; then
    BACKUP="$SUDOERS.{SALT}-backup-$(date -u +%Y%m%dT%H%M%SZ)"
    cp -a "$SUDOERS" "$BACKUP"
    TMP_SUDOERS=$(mktemp "$SUDOERS.{SALT}-XXXXXXXX")
    sed -E {shlex.quote(sed_address)} "$SUDOERS" > "$TMP_SUDOERS"
    chmod 0440 "$TMP_SUDOERS"
    visudo -c -f "$TMP_SUDOERS" >/dev/null
    mv -f "$TMP_SUDOERS" "$SUDOERS"
    TMP_SUDOERS=''
fi
visudo -c >/dev/null
LISTING=$(sudo -l -U {shlex.quote(user)} 2>&1 || true)
# ``sudo -l`` expands aliases: the rule is visible through its app-scoped
# command path, not through the alias name.
if ! printf '%s\\n' "$LISTING" | grep -Fq {shlex.quote(f"/opt/fujin/{app_name}")}; then
    FAIL_REASON=rule-missing
    exit 1
fi
if printf '%s\\n' "$LISTING" | grep -q 'NOPASSWD: ALL'; then
    FAIL_REASON=nopasswd-all
    exit 1
fi
"""


def nopasswd_all_regex(user: str) -> re.Pattern[str]:
    """Match the legacy ``<user> ALL=(ALL) NOPASSWD:ALL`` line exactly."""
    _validate_user(user)
    return re.compile(
        rf"^{re.escape(user)}[ \t]+ALL=\(ALL(?::ALL)?\)[ \t]+"
        r"NOPASSWD:[ \t]*ALL[ \t]*(?:#.*)?(?:\r?\n|$)",
        re.MULTILINE,
    )


def strip_nopasswd_all(content: str, user: str) -> tuple[str, bool]:
    """Remove the legacy rule from ``content``.

    Returns ``(new_content, removed)``.  Only the exact, whole-line rule for
    ``user`` is touched; anything else in ``/etc/sudoers`` is preserved.
    """
    new_content, count = nopasswd_all_regex(user).subn("", content)
    return new_content, count > 0


def has_includedir(content: str) -> bool:
    """True if the sudoers file pulls in ``/etc/sudoers.d``.

    Accepts ``#includedir``/``@includedir``, a trailing slash and a trailing
    comment.
    """
    return bool(_INCLUDEDIR_RE.search(content))


def _validate_user(name: str) -> None:
    if not _USERNAME_RE.match(name):
        raise ValueError(
            f"invalid user name for sudoers rule: {name!r} "
            "(letters, digits, '_' and '-' only; dots are rejected because "
            "sudo ignores includedir files containing '.')"
        )


def _validate_app_name(name: str) -> None:
    if not _APPNAME_RE.match(name):
        raise ValueError(f"invalid app name for sudoers rule: {name!r}")
