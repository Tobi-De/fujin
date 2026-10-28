"""``fujin doctor``: server integrity checks with safe, opt-in repairs.

The default run is strictly read-only. ``--fix`` repairs the inconsistencies
that can be repaired without guessing: a stale ``state.json`` (the ``current``
symlink is the truth), a missing ``current`` symlink when ``state.json``
names a release that still exists, orphaned release/staging directories,
drifted systemd units (including stray dropins), replica convergence,
permissions and a stale deploy lock. While a deploy lock is present, every
repair is skipped: a running deploy owns the state. Anything that needs a new
release or a user decision is only reported.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Annotated

import cappa

from fujin import connection, serverops, store
from fujin.audit import read_logs
from fujin.commands import BaseCommand
from fujin.exit_codes import EXIT_USER_ABORTED

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

_COLORS = {OK: "green", WARN: "yellow", FAIL: "red"}

#: Audit-record keys that may carry a release digest.
_AUDIT_DIGEST_KEYS = ("digest", "release", "manifest")

#: Lock states that mean "a deploy may be running". While any of them is
#: observed, ``--fix`` must not rewrite state, remove releases, reinstall
#: units, converge instances or delete staging trees: the running deploy owns
#: them. ``stale-legacy`` is a noclobber file whose pid is dead, so it does
#: not block repairs (the lock check removes it).
_LOCK_PRESENT_STATES = frozenset({"live", "held", "stale"})

_LOCK_NOTE = "deploy lock present; fixes skipped"


def _locked(probe: serverops.LockProbe) -> bool:
    return probe.state in _LOCK_PRESENT_STATES


def _is_digest(value: str | None) -> bool:
    return (
        bool(value) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _digest_of(target: str | None) -> str | None:
    """Release digest from a ``current`` symlink target (relative or absolute)."""
    if not target:
        return None
    digest = target.rstrip("/").rsplit("/", 1)[-1]
    return digest if _is_digest(digest) else None


def _short(digest: str | None) -> str:
    return digest[:12] if digest else "-"


def _q(*parts: str) -> str:
    return " ".join(shlex.quote(str(p)) for p in parts)


@dataclass(frozen=True)
class Check:
    """One reported check: ``OK``/``WARN``/``FAIL`` + one-line detail."""

    status: str
    title: str
    detail: str


@cappa.command(
    help="Check server state and repair safe inconsistencies",
    description=(
        "Runs read-only integrity checks over the release store, systemd units,"
        " permissions, Caddy, the deploy lock and the audit log. With --fix,"
        " safe problems are repaired; nothing is mutated without it."
    ),
)
@dataclass
class Doctor(BaseCommand):
    fix: Annotated[
        bool,
        cappa.Arg(
            long="--fix",
            help="Repair safe problems (default: report only)",
        ),
    ] = False

    def __call__(self):
        with connection.connection(host=self.selected_host) as conn:
            checks = self._run(conn)

        for check in checks:
            color = _COLORS[check.status]
            self.output.output(
                f"[{color}]{check.status:<4}[/{color}] {check.title}: {check.detail}"
            )

        remaining = [check for check in checks if check.status == FAIL]
        if remaining:
            raise cappa.Exit(
                f"{len(remaining)} problem(s) remain"
                + ("" if self.fix else "; re-run with --fix to repair safe ones"),
                code=EXIT_USER_ABORTED,
            )

    # ------------------------------------------------------------------
    # Check driver
    # ------------------------------------------------------------------

    def _run(self, conn) -> list[Check]:
        app_dir = self.config.app_dir
        state = serverops.read_state(conn, app_dir)
        link_target = serverops.read_symlink(conn, f"{app_dir}/current")
        symlink_digest = _digest_of(link_target)
        releases = serverops.list_releases(conn, app_dir)
        probe = serverops.probe_lock(conn, app_dir)
        locked = _locked(probe)

        state_check, effective_state, effective_digest = self._check_state(
            conn, app_dir, state, link_target, symlink_digest, releases, locked
        )

        unit_files, dropins, unit_error = self._release_units(
            conn, app_dir, effective_digest
        )

        checks = [state_check]
        checks.extend(
            self._check_releases(
                conn,
                app_dir,
                effective_state,
                effective_digest,
                releases,
                locked,
                state_missing=state is None,
            )
        )
        checks.extend(
            self._check_units(
                conn, unit_files, dropins, unit_error, effective_digest, locked
            )
        )
        checks.append(
            self._check_instances(
                conn, unit_files, unit_error, effective_digest, locked
            )
        )
        checks.extend(self._check_permissions(conn))
        checks.append(self._check_caddy(conn))
        checks.extend(self._check_lock(conn, app_dir, probe))
        checks.append(self._check_audit(conn, releases))
        return checks

    # ------------------------------------------------------------------
    # 1. state.json vs the current symlink
    # ------------------------------------------------------------------

    def _check_state(
        self,
        conn,
        app_dir: str,
        state: store.State | None,
        link_target: str | None,
        symlink_digest: str | None,
        releases: list[str],
        locked: bool,
    ) -> tuple[Check, store.State | None, str | None]:
        """Return the check, the *effective* state and the resolved digest.

        The effective state is the on-disk state, or the reconciled state when
        ``--fix`` rewrote it (so the release checks know the repaired
        current/previous). The resolved digest names the release whose tree
        can be inspected read-only: the symlink target, or the release
        ``state.json`` records when that directory still exists (even before
        ``--fix`` relinks it).
        """
        recorded = state.current if state else None
        present = set(releases)
        resolved = symlink_digest or (
            recorded if recorded and recorded in present else None
        )

        def result(
            check: Check, effective: store.State | None = None
        ) -> tuple[Check, store.State | None, str | None]:
            return check, (state if effective is None else effective), resolved

        if link_target is None:
            if recorded:
                if recorded not in present:
                    return result(
                        Check(
                            FAIL,
                            "state",
                            f"current symlink is missing and state.json records"
                            f" {_short(recorded)}, whose release directory is gone;"
                            " cannot relink",
                        )
                    )
                if locked:
                    return result(
                        Check(
                            FAIL,
                            "state",
                            f"current symlink is missing but state.json records"
                            f" {_short(recorded)}; {_LOCK_NOTE}",
                        )
                    )
                if not self.fix:
                    return result(
                        Check(
                            FAIL,
                            "state",
                            f"current symlink is missing but state.json records"
                            f" {_short(recorded)}; re-run with --fix to relink it",
                        )
                    )
                cmd = (
                    f"ln -sfn {_q(f'releases/{recorded}')} "
                    f"{_q(f'{app_dir}/current.next')}"
                    f" && mv -T {_q(f'{app_dir}/current.next')}"
                    f" {_q(f'{app_dir}/current')}"
                )
                _, ok = conn.run(cmd, warn=True, hide=True)
                if not ok:
                    return result(
                        Check(
                            FAIL,
                            "state",
                            f"failed to restore current -> {_short(recorded)}",
                        )
                    )
                return result(
                    Check(OK, "state", f"restored current -> {_short(recorded)}")
                )
            return result(
                Check(WARN, "state", "no release is deployed (no current symlink)")
            )

        if symlink_digest is None:
            return result(
                Check(
                    FAIL,
                    "state",
                    f"current points outside the release tree ({link_target});"
                    " not repaired",
                )
            )

        if state is None:
            if locked:
                return result(
                    Check(
                        FAIL,
                        "state",
                        f"state.json is missing or corrupt; current symlink points at"
                        f" {_short(symlink_digest)}; {_LOCK_NOTE}",
                    )
                )
            if self.fix:
                check, new_state = self._rewrite_state(
                    conn, app_dir, None, symlink_digest, releases
                )
                return check, new_state, symlink_digest
            return result(
                Check(
                    FAIL,
                    "state",
                    f"state.json is missing or corrupt; current symlink points at"
                    f" {_short(symlink_digest)}",
                )
            )

        if recorded != symlink_digest:
            if locked:
                return result(
                    Check(
                        FAIL,
                        "state",
                        f"state.json current={_short(recorded)} but current symlink"
                        f" points at {_short(symlink_digest)}; {_LOCK_NOTE}",
                    )
                )
            if self.fix:
                check, new_state = self._rewrite_state(
                    conn, app_dir, state, symlink_digest, releases
                )
                return check, new_state, symlink_digest
            return result(
                Check(
                    FAIL,
                    "state",
                    f"state.json current={_short(recorded)} but current symlink points"
                    f" at {_short(symlink_digest)}",
                )
            )

        return result(
            Check(OK, "state", f"state.json matches current ({_short(symlink_digest)})")
        )

    def _rewrite_state(
        self,
        conn,
        app_dir: str,
        state: store.State | None,
        symlink_digest: str,
        releases: list[str],
    ) -> tuple[Check, store.State]:
        present = set(releases)
        old_current = state.current if state else None
        if old_current and old_current != symlink_digest and old_current in present:
            # The previously-recorded release becomes the single-level
            # rollback target; deploys history is preserved verbatim.
            previous = [old_current]
        else:
            # Only record a previous release whose directory still exists:
            # a dangling rollback target is worse than none.
            previous = [
                digest
                for digest in (state.previous if state else [])
                if digest != symlink_digest and digest in present
            ][:1]

        units_from = state.units_installed_from if state else None
        if units_from is None or units_from == old_current:
            units_from = symlink_digest

        new_state = store.State(
            current=symlink_digest,
            previous=previous,
            units_installed_from=units_from,
            deploys=list(state.deploys) if state else [],
            client_digest=state.client_digest if state else None,
            config_fingerprint=state.config_fingerprint if state else None,
        )
        serverops.write_state(conn, app_dir, new_state)
        detail = f"rewrote state.json current={_short(symlink_digest)}"
        if previous:
            detail += f" previous={_short(previous[0])}"
        return Check(OK, "state", detail), new_state

    # ------------------------------------------------------------------
    # 2. referenced / orphaned releases
    # ------------------------------------------------------------------

    def _check_releases(
        self,
        conn,
        app_dir: str,
        state: store.State | None,
        current_digest: str | None,
        releases: list[str],
        locked: bool,
        *,
        state_missing: bool,
    ) -> list[Check]:
        required: set[str] = set()
        if current_digest:
            required.add(current_digest)
        if state:
            if state.current:
                required.add(state.current)
            required.update(state.previous)
        history = {record.manifest for record in state.deploys} if state else set()

        present = set(releases)
        missing_required = sorted(required - present)
        missing_history = sorted((history - present) - required)
        referenced = required | history
        orphans = sorted(present - referenced)

        checks: list[Check] = []

        if missing_required:
            checks.append(
                Check(
                    FAIL,
                    "releases",
                    f"{len(missing_required)} referenced release(s) missing on disk:"
                    f" {', '.join(_short(d) for d in missing_required)}",
                )
            )
        elif missing_history:
            checks.append(
                Check(
                    WARN,
                    "releases",
                    f"{len(missing_history)} historical deploy record(s) were pruned"
                    f" by GC: {', '.join(_short(d) for d in missing_history)}",
                )
            )
        else:
            checks.append(Check(OK, "releases", "every referenced release exists"))

        if not orphans:
            checks.append(Check(OK, "releases", "no orphaned release directories"))
            return checks

        names = ", ".join(_short(d) for d in orphans)
        if state is None or state_missing:
            # Without a readable state.json there is no way to know which
            # releases are the rollback target; never delete anything. Even a
            # reconciled state cannot prove an orphan was not referenced.
            checks.append(
                Check(
                    FAIL,
                    "releases",
                    f"{len(orphans)} orphaned release dir(s) but state.json was"
                    f" unreadable; removal skipped: {names}",
                )
            )
            return checks
        if locked:
            checks.append(
                Check(
                    FAIL,
                    "releases",
                    f"{len(orphans)} orphaned release dir(s): {names}; {_LOCK_NOTE}",
                )
            )
            return checks
        if not self.fix:
            checks.append(
                Check(
                    FAIL,
                    "releases",
                    f"{len(orphans)} orphaned release dir(s) not referenced by state:"
                    f" {names}",
                )
            )
            return checks

        # ``referenced`` is exactly current/previous/deploy records plus the
        # symlink target, so current and previous can never be removed here.
        protected = referenced | {current_digest}
        removed = [
            digest
            for digest in orphans
            if digest not in protected and _remove(conn, f"{app_dir}/releases/{digest}")
        ]
        checks.append(
            Check(
                OK,
                "releases",
                f"removed {len(removed)} orphaned release dir(s):"
                f" {', '.join(_short(d) for d in removed)}",
            )
        )
        return checks

    # ------------------------------------------------------------------
    # 3. installed systemd units vs the live release tree
    # ------------------------------------------------------------------

    def _release_units(
        self, conn, app_dir: str, release_digest: str | None
    ) -> tuple[dict[str, str], dict[str, dict[str, str]], str | None]:
        """Unit files/dropins of the live release, or ``({}, {}, error)``.

        Reads ``releases/<digest>`` (not ``current``): the symlink may be the
        very thing being repaired, and the recorded digest is equally valid
        read-only.
        """
        if release_digest is None:
            return {}, {}, None
        release_path = f"{app_dir}/releases/{release_digest}"
        manifest = serverops.read_manifest_at(conn, release_path)
        if manifest is None:
            return {}, {}, "live release manifest is unreadable"
        unit_files, dropins, _oneshots, failed = serverops.collect_release_units(
            conn, release_path, manifest
        )
        if failed:
            return (
                unit_files,
                dropins,
                f"unreadable unit file(s) in the live release: {', '.join(failed)}",
            )
        return unit_files, dropins, None

    def _check_units(
        self,
        conn,
        unit_files: dict[str, str],
        dropins: dict[str, dict[str, str]],
        unit_error: str | None,
        release_digest: str | None,
        locked: bool,
    ) -> list[Check]:
        if release_digest is None:
            return [Check(WARN, "units", "no release is deployed; units not checked")]
        if unit_error:
            return [Check(FAIL, "units", f"{unit_error}; unit files not checked")]

        app_name = self.config.app_name
        expected: dict[str, str] = dict(unit_files)
        expected_dirs = {f"{unit}.d" for unit in set(unit_files) | set(dropins)}
        for unit, files in dropins.items():
            for name, content in files.items():
                expected[f"{unit}.d/{name}"] = content

        def divergence() -> tuple[list[str], list[str], list[str]]:
            tree = serverops.installed_unit_tree(conn, app_name)
            stray = sorted(
                rel for rel in tree if rel not in expected and rel not in expected_dirs
            )
            missing = sorted(rel for rel in expected if rel not in tree)
            drifted = sorted(
                rel
                for rel, content in expected.items()
                if rel in tree and tree[rel] != content
            )
            return stray, missing, drifted

        stray, missing, drifted = divergence()

        if not (stray or missing or drifted):
            if not expected:
                return [
                    Check(
                        OK,
                        "units",
                        "live release declares no systemd units and none are installed",
                    )
                ]
            return [Check(OK, "units", f"{len(expected)} installed unit file(s) match")]

        problems = []
        if drifted:
            problems.append(f"drifted: {', '.join(drifted)}")
        if missing:
            problems.append(f"missing: {', '.join(missing)}")
        if stray:
            problems.append(f"stray: {', '.join(stray)}")
        detail = "; ".join(problems)

        if locked:
            return [Check(FAIL, "units", f"{detail}; {_LOCK_NOTE}")]
        if not self.fix:
            return [Check(FAIL, "units", detail)]

        stray_count = len(stray)
        serverops.install_units(conn, app_name, unit_files, dropins)
        # Only report what actually converged: install_units prunes stale
        # top-level units *and* stale dropin files in kept ``*.d/`` dirs.
        stray, missing, drifted = divergence()
        if stray or missing or drifted:
            problems = []
            if drifted:
                problems.append(f"drifted: {', '.join(drifted)}")
            if missing:
                problems.append(f"missing: {', '.join(missing)}")
            if stray:
                problems.append(f"stray: {', '.join(stray)}")
            return [
                Check(
                    FAIL,
                    "units",
                    f"repair incomplete; still divergent: {'; '.join(problems)}",
                )
            ]
        return [
            Check(
                OK,
                "units",
                f"re-installed {len(expected)} unit file(s), removed"
                f" {stray_count} stray entry(ies)",
            )
        ]

    # ------------------------------------------------------------------
    # 4. template instances vs config.replicas
    # ------------------------------------------------------------------

    def _check_instances(
        self,
        conn,
        unit_files: dict[str, str],
        unit_error: str | None,
        release_digest: str | None,
        locked: bool,
    ) -> Check:
        if release_digest is None:
            return Check(
                WARN, "instances", "no release is deployed; replica check skipped"
            )
        if unit_error:
            return Check(
                WARN,
                "instances",
                "live release units are unreadable; replica check skipped",
            )

        # Mirror ``deploy._converge``: only template units actually declared
        # by the live release seed the desired set. A key in ``replicas`` for
        # a service with no ``@.service`` must not conjure instances.
        app_name = self.config.app_name
        prefix = f"{app_name}-"
        desired: list[str] = []
        for name in sorted(unit_files):
            if not name.startswith(prefix) or not name.endswith("@.service"):
                continue
            base = name.removeprefix(prefix).removesuffix("@.service")
            replicas = self.config.replicas.get(base, 1)
            desired.extend(
                sorted(store.desired_instances(f"{prefix}{base}", max(1, replicas)))
            )

        start, stop = serverops.template_instance_plan(conn, app_name, desired)
        if not start and not stop:
            return Check(
                OK,
                "instances",
                f"{len(desired)} template instance(s) match the live release",
            )

        parts = []
        if start:
            parts.append(f"start: {', '.join(start)}")
        if stop:
            parts.append(f"stop: {', '.join(stop)}")
        detail = "; ".join(parts)

        if locked:
            return Check(FAIL, "instances", f"{detail}; {_LOCK_NOTE}")
        if not self.fix:
            return Check(FAIL, "instances", detail)

        stopped = serverops.stop_and_disable(conn, stop)
        started = serverops.enable_and_start(conn, start)
        if not (stopped and started):
            return Check(FAIL, "instances", f"convergence failed ({detail})")
        return Check(OK, "instances", f"converged ({detail})")

    # ------------------------------------------------------------------
    # 5. permissions
    # ------------------------------------------------------------------

    def _check_permissions(self, conn) -> list[Check]:
        config = self.config
        app_dir = config.app_dir
        checks: list[Check] = []

        app_stat = serverops.path_stat(conn, app_dir)
        if app_stat is None:
            checks.append(Check(FAIL, "permissions", f"app dir {app_dir} is missing"))
        else:
            mode, owner, group = app_stat
            detail = f"{app_dir} mode {mode:04o} owner {owner}:{group}"
            if mode & 0o2000:
                checks.append(Check(OK, "permissions", detail))
            elif self.fix:
                conn.run(f"sudo chmod g+s {_q(app_dir)}", warn=True, hide=True)
                checks.append(Check(OK, "permissions", f"{detail}; set setgid bit"))
            else:
                checks.append(
                    Check(FAIL, "permissions", f"{detail}; missing setgid bit")
                )

        env_path = f"{app_dir}/current/.env"
        env_stat = serverops.path_stat(conn, env_path)
        if env_stat is None:
            checks.append(Check(WARN, "permissions", f"{env_path} not found; skipped"))
        else:
            mode, owner, group = env_stat
            detail = f"{env_path} mode {mode:04o} owner {owner}:{group}"
            if mode == 0o640:
                checks.append(Check(OK, "permissions", detail))
            elif self.fix:
                conn.run(f"sudo chmod 0640 {_q(env_path)}", warn=True, hide=True)
                checks.append(Check(OK, "permissions", f"{detail}; chmod 0640"))
            else:
                checks.append(
                    Check(FAIL, "permissions", f"{detail}; expected mode 0640")
                )

        run_dir = f"/run/{config.app_name}"
        run_stat = serverops.path_stat(conn, run_dir)
        if run_stat is None:
            checks.append(
                Check(
                    WARN,
                    "permissions",
                    f"socket dir {run_dir} not present (no socket units?); skipped",
                )
            )
        else:
            mode, _owner, _group = run_stat
            detail = f"{run_dir} mode {mode:04o}"
            if mode & 0o050 == 0o050:
                checks.append(Check(OK, "permissions", f"{detail}; group readable"))
            elif self.fix:
                conn.run(f"sudo chmod g+rx {_q(run_dir)}", warn=True, hide=True)
                checks.append(
                    Check(OK, "permissions", f"{detail}; made group readable")
                )
            else:
                checks.append(
                    Check(
                        FAIL,
                        "permissions",
                        f"{detail}; group cannot traverse/read it",
                    )
                )

        out, ok = conn.run("id -nG caddy", warn=True, hide=True)
        if not ok:
            checks.append(
                Check(
                    WARN, "permissions", "caddy user not present; group check skipped"
                )
            )
        elif config.app_user and config.app_user not in out.split():
            checks.append(
                Check(
                    WARN,
                    "permissions",
                    f"caddy is not a member of group {config.app_user};"
                    " socket access may fail",
                )
            )
        else:
            checks.append(
                Check(
                    OK,
                    "permissions",
                    f"caddy is a member of group {config.app_user}",
                )
            )
        return checks

    # ------------------------------------------------------------------
    # 6. Caddy
    # ------------------------------------------------------------------

    def _check_caddy(self, conn) -> Check:
        config = self.config
        _, has_caddy = conn.run("command -v caddy", warn=True, hide=True)
        if not has_caddy:
            return Check(WARN, "caddy", "caddy is not installed; config check skipped")

        remote = config.caddy_config_path
        _, exists = conn.run(f"test -f {_q(remote)}", warn=True, hide=True)
        if not exists:
            if config.caddyfile_exists:
                return Check(FAIL, "caddy", f"{remote} is missing")
            return Check(WARN, "caddy", "Caddy is not configured for this app; skipped")

        out, ok = conn.run(
            f"sudo caddy validate --config {_q(remote)} --adapter caddyfile 2>&1",
            warn=True,
            hide=True,
        )
        if ok:
            return Check(OK, "caddy", f"{remote} validates")
        first = next((line.strip() for line in out.splitlines() if line.strip()), "")
        detail = f"caddy validate failed for {remote}"
        if first:
            detail += f": {first[:160]}"
        return Check(FAIL, "caddy", detail)

    # ------------------------------------------------------------------
    # 7. deploy lock + orphaned staging dirs
    # ------------------------------------------------------------------

    def _check_lock(
        self, conn, app_dir: str, probe: serverops.LockProbe
    ) -> list[Check]:
        staging = serverops.list_staging_dirs(conn, app_dir)
        live = probe.state == "live"
        checks: list[Check] = []

        if probe.state in ("stale", "stale-legacy"):
            kind = "holder file" if probe.state == "stale" else "legacy lock file"
            if self.fix:
                conn.run(
                    f"rm -f {_q(f'{app_dir}/{serverops.LOCK_PATH}.holder')}",
                    warn=True,
                    hide=True,
                )
                conn.run(
                    f"rm -f {_q(f'{app_dir}/{serverops.LEGACY_LOCK_PATH}')}",
                    warn=True,
                    hide=True,
                )
                checks.append(
                    Check(
                        OK,
                        "lock",
                        f"removed stale {kind}"
                        + (f" (dead pid {probe.pid})" if probe.pid else ""),
                    )
                )
            else:
                checks.append(
                    Check(
                        FAIL,
                        "lock",
                        f"stale {kind}"
                        + (f" (pid {probe.pid} not running)" if probe.pid else ""),
                    )
                )
        elif live:
            checks.append(
                Check(OK, "lock", f"held by live process {probe.pid} (deploy running)")
            )
        elif probe.state == "held":
            checks.append(
                Check(
                    WARN,
                    "lock",
                    "flock is held but no holder pid is recorded; left untouched",
                )
            )
        else:
            checks.append(Check(OK, "lock", "deploy lock is free"))

        if not staging:
            checks.append(Check(OK, "staging", "no orphaned staging directories"))
            return checks

        names = ", ".join(path.rsplit("/", 1)[-1] for path in staging)
        if probe.state != "free":
            reason = "a deploy is live" if live else "the deploy lock is present"
            checks.append(
                Check(
                    WARN,
                    "staging",
                    f"{len(staging)} staging dir(s) present while {reason};"
                    f" left untouched: {names}",
                )
            )
        elif self.fix:
            for path in staging:
                conn.run(f"rm -rf {_q(path)}", warn=True, hide=True)
            checks.append(
                Check(
                    OK,
                    "staging",
                    f"removed {len(staging)} orphaned staging dir(s): {names}",
                )
            )
        else:
            checks.append(
                Check(
                    FAIL,
                    "staging",
                    f"{len(staging)} orphaned staging dir(s): {names}",
                )
            )
        return checks

    # ------------------------------------------------------------------
    # 8. audit log integrity
    # ------------------------------------------------------------------

    def _check_audit(self, conn, releases: list[str]) -> Check:
        records = read_logs(conn, self.config.app_name)
        if not records:
            return Check(WARN, "audit", "no audit records found")

        digests: set[str] = set()
        legacy = 0
        for record in records:
            recorded = False
            for key in _AUDIT_DIGEST_KEYS:
                value = record.get(key)
                if isinstance(value, str):
                    digest = store.normalize_digest(value)
                    if _is_digest(digest):
                        digests.add(digest)
                        recorded = True
            if not recorded:
                # Records written before digests were logged; a note, never a
                # failure (there is nothing to cross-check).
                legacy += 1

        if not digests:
            return Check(
                WARN,
                "audit",
                f"{len(records)} audit record(s) carry no release digest;"
                " nothing to cross-check",
            )

        present = set(releases)
        missing = sorted(digests - present)
        if missing:
            return Check(
                FAIL,
                "audit",
                f"{len(missing)} audit record digest(s) missing on disk:"
                f" {', '.join(_short(d) for d in missing)}",
            )
        detail = f"{len(records)} audit record(s); release digests consistent"
        if legacy:
            detail += f"; {legacy} legacy record(s) without a release digest"
        return Check(OK, "audit", detail)


def _remove(conn, path: str) -> bool:
    """Remove *path* recursively; returns True on success."""
    _, ok = conn.run(f"rm -rf {_q(path)}", warn=True, hide=True)
    return ok
