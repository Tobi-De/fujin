from __future__ import annotations

import configparser
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, overload

import cappa
from rich.prompt import IntPrompt

from fujin import connection
from fujin import serverops, store, transfer
from fujin.audit import log_operation
from fujin.commands import BaseCommand
from fujin.manifest import Manifest, decode_manifest, manifest_to_dict
from fujin.store import (
    convergence,
    desired_instances,
    gc_plan,
    normalize_digest,
    record_rollback,
    restart_set,
)


def _q(path: str) -> str:
    return shlex.quote(path)


def _read_remote_file(conn, path: str, *, fatal: str | None = None) -> str | None:
    """Read a remote file; ``fatal`` makes failure abort with that message."""
    out, ok = conn.run(f"cat {_q(path)}", warn=True, hide=True)
    if not ok:
        if fatal:
            raise cappa.Exit(fatal, code=1)
        return None
    return out


@overload
def _read_remote_file(conn, path: str, *, fatal: None = None) -> str | None: ...


@overload
def _read_remote_file(conn, path: str, *, fatal: str) -> str: ...


def _read_remote_file(conn, path: str, *, fatal: str | None = None) -> str | None:
    """Read a remote file; ``fatal`` makes failure abort with that message."""
    out, ok = conn.run(f"cat {_q(path)}", warn=True, hide=True)
    if not ok:
        if fatal:
            raise cappa.Exit(fatal, code=1)
        return None
    return out


def _unit_rel_name(path: str) -> str:
    """Strip the ``systemd/`` prefix from a manifest entry path."""
    return path.split("/", 1)[1] if path.startswith("systemd/") else path


def _is_oneshot(service_content: str) -> bool:
    """Type=oneshot detection from a service unit file body."""
    parser = configparser.RawConfigParser(strict=False)
    parser.read_string(service_content)
    if not parser.has_option("Service", "Type"):
        return False
    return parser.get("Service", "Type").strip().lower() == "oneshot"


@cappa.command(
    help="Roll back application to a previous release"
    " (single-level: a failed rollback never rolls back further)",
)
@dataclass
class Rollback(BaseCommand):
    previous: Annotated[
        bool,
        cappa.Arg(
            long="--previous",
            short="-p",
            help="Roll back to the immediately-previous release without prompting",
        ),
    ] = False
    to: Annotated[
        str | None,
        cappa.Arg(
            long="--to",
            help="Roll back to a specific kept release digest"
            " (sha256, optional 'sha256:' prefix)",
        ),
    ] = None
    strict: Annotated[
        bool,
        cappa.Arg(
            long="--strict",
            short="-s",
            help="Exit with an error if no rollback targets are available",
        ),
    ] = False

    def __call__(self):
        with connection.connection(host=self.selected_host) as conn:
            self._ensure_new_layout(conn)
            lock = serverops.acquire_lock(conn, self.config.app_dir)
            try:
                return self._perform_rollback(conn)
            finally:
                serverops.release_lock(conn, self.config.app_dir, lock)

    # ------------------------------------------------------------------
    # Layout / basic remote reads
    # ------------------------------------------------------------------

    def _ensure_new_layout(self, conn) -> None:
        """Old-layout detection: ``store/`` missing -> user must deploy once."""
        app_dir = self.config.app_dir
        _, ok = conn.run(f"test -d {_q(f'{app_dir}/store')}", warn=True, hide=True)
        if ok:
            return
        _, has_old = conn.run(
            f"test -d {_q(self.config.install_dir)}", warn=True, hide=True
        )
        old_note = (
            " Your application still uses the old .install/ layout - it is kept"
            " in place and is cleaned up by `fujin down`."
            if has_old
            else ""
        )
        raise cappa.Exit(
            "This application was not yet deployed with the release-tree layout.\n"
            "Deploy once with this version of fujin to migrate"
            " (the migration is non-destructive).\n"
            f"Then `fujin rollback` becomes available.{old_note}",
            code=1,
        )

    def _release_path(self, digest: str) -> str:
        return f"{self.config.app_dir}/releases/{digest}"

    def _list_releases(self, conn) -> list[str]:
        """Digests present under releases/, newest (mtime) first."""
        out, _ok = conn.run(
            f"cd {_q(self.config.app_dir)} && ls -1t releases/ 2>/dev/null || true",
            warn=True,
            hide=True,
        )
        return [
            line.strip()
            for line in out.splitlines()
            if line.strip() and not line.strip().startswith(".staging-")
        ]

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    def _kept_releases(self, state: store.State, releases: list[str]) -> set[str]:
        """Releases the server's GC keeps (users never see raw ls output)."""
        kill = gc_plan(
            state.current,
            state.previous,
            releases,
            self.config.versions_to_keep,
        )
        return set(releases) - set(kill)

    def _resolve_target(
        self,
        conn,
        state: store.State,
        releases: list[str],
        current_digest: str,
    ) -> str:
        if self.to is not None:
            target = normalize_digest(self.to)
            if target == normalize_digest(current_digest):
                self.output.info(
                    "The requested release is already the current release."
                )
                raise cappa.Exit(code=0)
            _, exists = conn.run(
                f"test -f {_q(f'{self._release_path(target)}/manifest.json')}",
                warn=True,
                hide=True,
            )
            if not exists:
                raise cappa.Exit(
                    f"Release '{target}' is not available on the server - it was"
                    " pruned when old releases were garbage-collected.\nRedeploy"
                    " the old commit/tag instead.",
                    code=1,
                )
            return target

        if self.previous:
            if not state.previous:
                raise cappa.Exit(
                    "No previous release found in the release store.\n"
                    "At least two deploys are required before a rollback.",
                    code=1,
                )
            return state.previous[0]

        return self._prompt_target(state, releases, current_digest)

    def _prompt_target(
        self,
        state: store.State,
        releases: list[str],
        current_digest: str,
    ) -> str:
        """Interactive selection among kept releases (GC'd digests never offered)."""
        keep = self._kept_releases(state, releases)
        current_q = normalize_digest(current_digest)
        candidates: list[str] = []
        if state.previous and state.previous[0] in keep:
            candidates.append(state.previous[0])
        for release in releases:
            release = normalize_digest(release)
            if release == current_q or release in candidates:
                continue
            if release in keep:
                candidates.append(release)
        if not candidates:
            if self.strict:
                raise cappa.Exit(
                    "No rollback targets are available (no kept releases besides"
                    " the current one).",
                    code=1,
                )
            self.output.info("No previous versions available for rollback")
            raise cappa.Exit(code=0)

        self.output.output("Available rollback targets:")
        for i, digest in enumerate(candidates, 1):
            self.output.output(f"  {i}. {digest}")

        try:
            index = IntPrompt.ask(
                "Select release to roll back to",
                choices=[str(i) for i in range(1, len(candidates) + 1)],
                show_choices=False,
                default=1,
            )
        except KeyboardInterrupt:
            self.output.info("Rollback cancelled.")
            raise cappa.Exit(code=1) from None
        return candidates[index - 1]

    # ------------------------------------------------------------------
    # Rollback execution
    # ------------------------------------------------------------------

    def _perform_rollback(self, conn) -> None:
        app_dir = self.config.app_dir

        state = serverops.read_state(conn, app_dir)
        if state is None or not state.current:
            raise cappa.Exit(
                "No release store state found (store/state.json missing or"
                " empty). Deploy at least once with this version of fujin.",
                code=1,
            )

        current_manifest = serverops.read_current_manifest(conn, app_dir)
        if current_manifest is None:
            raise cappa.Exit(
                "The live release's manifest could not be read - the release"
                " tree is incomplete.",
                code=1,
            )

        releases = self._list_releases(conn)
        target = self._resolve_target(conn, state, releases, state.current)
        target_path = self._release_path(target)

        target_manifest = self._read_target_manifest(conn, target, target_path)
        self._verify_target(conn, target, target_path, target_manifest)

        self.output.info(f"Rolling back to release {target}...")
        log_operation(
            conn,
            self.config.app_name,
            "rollback",
            host=self.selected_host.name or self.selected_host.address,
            from_version=current_manifest.version,
            to_version=target_manifest.version,
        )

        transfer.swap_current(conn, app_dir, target)

        unit_files, dropins, oneshots = self._collect_units(
            conn, target_path, target_manifest
        )
        if unit_files or dropins:
            serverops.install_units(conn, self.config.app_name, unit_files, dropins)

        self._converge(conn, target_manifest)
        self._restart(conn, current_manifest, target_manifest, unit_files, oneshots)
        self._install_caddyfile(conn, target_path, target_manifest)

        at = datetime.now(UTC).isoformat()
        new_state = record_rollback(state, target, at)
        serverops.write_state(conn, app_dir, new_state)
        self._gc(conn, new_state, releases)

        self.output.success(f"Rollback to release {target} completed successfully!")

    def _read_target_manifest(self, conn, target: str, target_path: str) -> Manifest:
        raw = _read_remote_file(
            conn,
            f"{target_path}/manifest.json",
            fatal=(
                f"Release '{target}' is incomplete: its manifest.json is missing"
                " or unreadable, so it cannot be rolled back to."
            ),
        )
        try:
            return decode_manifest(raw) if raw is not None else None
        except Exception as exc:
            raise cappa.Exit(
                f"Release '{target}' manifest is corrupt: {exc}", code=1
            ) from None

    def _verify_target(
        self, conn, target: str, target_path: str, manifest: Manifest
    ) -> None:
        result = transfer.verify_staging(
            conn, target_path, manifest_to_dict(manifest, include_created_at=False)
        )
        if result.ok:
            return
        parts = [f"Release tree {target_path} failed verification:"]
        for path in result.missing:
            parts.append(f"  missing: {path}")
        for path, mismatch in result.mismatch.items():
            parts.append(
                f"  hash mismatch: {path}"
                f" (expected {mismatch.expected}, got {mismatch.actual})"
            )
        for path, want in result.link_mismatch.items():
            parts.append(f"  symlink mismatch: {path} (expected link to {want})")
        for path, mismatch in result.mode_mismatch.items():
            parts.append(
                f"  mode mismatch: {path}"
                f" (expected {mismatch.expected}, got {mismatch.actual})"
            )
        raise cappa.Exit("\n".join(parts), code=1)

    # ------------------------------------------------------------------
    # Units + services
    # ------------------------------------------------------------------

    def _collect_units(
        self, conn, target_path: str, manifest: Manifest
    ) -> tuple[dict[str, str], dict[str, dict[str, str]], set[str]]:
        """Unit files + dropins of the target release, read from its systemd/ tree.

        Returns ``(unit_files, dropins, oneshots)``; ``dropins`` expands
        ``common.d`` per service unit (install_units' contract).
        """
        unit_files: dict[str, str] = {}
        service_dropins: dict[str, dict[str, str]] = {}
        common_dropins: dict[str, str] = {}
        systemd_path = f"{target_path}/systemd"

        unit_paths = sorted(
            path for path, entry in manifest.entries.items() if entry.kind == "unit"
        )
        for path in unit_paths:
            rel = _unit_rel_name(path)
            content = _read_remote_file(
                conn,
                f"{systemd_path}/{rel}",
                fatal=f"Cannot read unit file '{rel}' from the target release tree.",
            )
            if rel.startswith("common.d/"):
                common_dropins[rel.split("/", 1)[1]] = content
            elif ".d/" in rel and rel.endswith(".conf"):
                # Generic dropin: any unit type (``*.service.d/``,
                # ``*.socket.d/``, ``*.timer.d/``) -> dropins dict keyed by
                # the full unit name, same contract as deploy-side
                # collect_remote_units.
                unit_dir, _, dropin_name = rel.rpartition("/")
                unit = unit_dir.removesuffix(".d")
                service_dropins.setdefault(unit, {})[dropin_name] = content
            else:
                unit_files[rel] = content

        for unit in list(unit_files):
            if unit.endswith(".service"):
                for name, content in common_dropins.items():
                    service_dropins.setdefault(unit, {}).setdefault(name, content)

        oneshots = {
            unit
            for unit, content in unit_files.items()
            if unit.endswith(".service") and _is_oneshot(content)
        }
        return unit_files, service_dropins, oneshots

    def _desired_units(self, manifest: Manifest) -> list[str]:
        """Desired instance/unit names implied by the target release tree."""
        app_name = self.config.app_name
        prefix = f"{app_name}-"
        desired: list[str] = []
        for path in sorted(manifest.entries):
            entry = manifest.entries[path]
            if entry.kind != "unit":
                continue
            name = _unit_rel_name(path)
            if name.endswith("@.service") and name.startswith(prefix):
                base = name[len(prefix) : -len("@.service")]
                replicas = self.config.replicas.get(base, 1)
                desired.extend(
                    sorted(desired_instances(name.removesuffix("@.service"), replicas))
                )
            elif name.endswith(".service"):
                desired.append(name)
        return desired

    def _converge(self, conn, target_manifest: Manifest) -> None:
        """Start/stop template instances to the target release's replica counts.

        Singular service units are enabled/started by the original deploy and
        stay untouched here (they keep running across a rollback).
        """
        desired = [unit for unit in self._desired_units(target_manifest) if "@" in unit]
        app_glob = self.config.app_name
        out, _ok = conn.run(
            f"systemctl list-unit-files --no-legend"
            f" '{app_glob}-*@*.service' 2>/dev/null || true",
            warn=True,
            hide=True,
        )
        present: list[str] = []
        for line in out.splitlines():
            parts = line.split()
            if parts and not parts[0].endswith("@.service"):
                present.append(parts[0])

        start, stop = convergence(desired, present)
        if stop and not serverops.stop_and_disable(conn, sorted(stop)):
            raise cappa.Exit(
                "Rollback failed while stopping extra service instances"
                " (no nested rollback is attempted); inspect the server state.",
                code=1,
            )
        if start and not serverops.enable_and_start(conn, sorted(start)):
            raise cappa.Exit(
                "Rollback failed while starting missing service instances"
                " (no nested rollback is attempted); inspect the server state.",
                code=1,
            )

    def _restart(
        self,
        conn,
        current_manifest: Manifest,
        target_manifest: Manifest,
        unit_files: dict[str, str],
        oneshots: set[str],
    ) -> None:
        restart_units = restart_set(
            current_manifest,
            target_manifest,
            env_fed_units=[name for name in unit_files if name.endswith(".service")],
            oneshot_units=oneshots,
        )
        specs = sorted(
            name.replace("@.service", "@*.service")
            if name.endswith("@.service")
            else name
            for name in restart_units
        )
        if not specs:
            self.output.info("No unit needs a restart for this rollback.")
            return
        _out, ok = conn.run(
            f"sudo systemctl restart --quiet {' '.join(_q(s) for s in specs)}",
            warn=True,
        )
        if not ok:
            raise cappa.Exit(
                "Rollback failed while restarting services (no nested rollback is"
                " attempted); inspect the application for a failed state.",
                code=1,
            )

    def _install_caddyfile(self, conn, target_path: str, manifest: Manifest) -> None:
        caddy_entry = manifest.entries.get("Caddyfile")
        if caddy_entry is None or caddy_entry.kind != "caddy":
            return
        content = _read_remote_file(conn, f"{target_path}/Caddyfile")
        if content is None:
            self.output.warning(
                "Target release declares a Caddyfile but it could not be read;"
                " caddy config unchanged."
            )
            return
        try:
            installed = serverops.install_caddyfile(
                conn, content, self.config.caddy_config_path
            )
        except Exception:
            self.output.warning(
                "Failed to apply the target release's Caddyfile; caddy config"
                " unchanged."
            )
        else:
            if installed:
                self.output.info("Caddy reloaded with the target release's Caddyfile.")

    # ------------------------------------------------------------------
    # GC
    # ------------------------------------------------------------------

    def _gc(self, conn, new_state: store.State, releases: list[str]) -> None:
        kill = gc_plan(
            new_state.current,
            new_state.previous,
            releases,
            self.config.versions_to_keep,
        )
        for digest in kill:
            conn.run(f"rm -rf {_q(self._release_path(digest))}", warn=True, hide=True)
        if kill:
            self.output.info(f"Garbage-collected {len(kill)} old release(s).")
