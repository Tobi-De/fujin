from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import cappa
from rich.prompt import IntPrompt

from fujin import connection
from fujin import serverops, store, transfer
from fujin.audit import log_operation
from fujin.commands import BaseCommand
from fujin.errors import CommandError
from fujin.exit_codes import EXIT_OK, EXIT_ROLLBACK_FAILED, EXIT_USER_ABORTED
from fujin.manifest import Manifest, decode_manifest, manifest_to_dict
from fujin.store import (
    desired_instances,
    gc_plan,
    normalize_digest,
    record_rollback,
    restart_set,
)


def _is_digest(value: str) -> bool:
    """64 hex lowercase chars: a release digest, not a version string."""
    return bool(
        value and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


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
        _, ok = conn.run(
            f"test -d {serverops.q(f'{app_dir}/store')}", warn=True, hide=True
        )
        if ok:
            return
        _, has_old = conn.run(
            f"test -d {serverops.q(self.config.install_dir)}", warn=True, hide=True
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
            code=EXIT_USER_ABORTED,
        )

    def _release_path(self, digest: str) -> str:
        return f"{self.config.app_dir}/releases/{digest}"

    def _list_releases(self, conn) -> list[str]:
        """Digests present under releases/, newest (mtime) first."""
        return serverops.list_releases(conn, self.config.app_dir)

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

    def _version_matches(self, manifest_version: str, version: str) -> bool:
        """Exact match, or a git-suffixed manifest version's base version.

        In git checkouts the manifest records ``"<version>-<short-hash>"``
        (see ``Config.local_version``), so ``--to 1.2.3`` never exact-matches
        ``1.2.3-4b8a2c1``. The prefix compare only kicks in when the manifest
        version really carries a hex (optionally ``g``-prefixed) short-hash
        suffix; any other ``<version>-<word>`` must match exactly.
        """
        if manifest_version == version:
            return True
        _, sep, suffix = manifest_version.rpartition("-")
        if sep and suffix and all(c in "0123456789abcdefABCDEFg" for c in suffix):
            return manifest_version.rpartition("-")[0] == version
        return False

    def _resolve_target(
        self,
        conn,
        state: store.State,
        releases: list[str],
        current_digest: str,
    ) -> str:
        if self.to is not None:
            raw = normalize_digest(self.to)
            # Digests only exist under releases/<digest>/; anything else is
            # probed as a release dir first (release dirs are digest-named
            # in practice), then tried as a version string against kept
            # release manifests.
            # A digest-shaped value is case-normalized so the release path
            # probe cannot silently miss an uppercase SHA (release dirs are
            # always lowercase hex).
            if _is_digest(raw.lower()):
                raw = raw.lower()
                target = self._check_digest_target(conn, raw)
            elif self._digest_dir_exists(conn, raw):
                target = self._check_digest_target(conn, raw)
            else:
                target = self._target_by_version(conn, state, releases, raw)
            if target == normalize_digest(current_digest):
                self.output.info(
                    "The requested release is already the current release."
                )
                raise cappa.Exit(code=EXIT_OK)
            return target

        if self.previous:
            if not state.previous:
                raise cappa.Exit(
                    "No previous release found in the release store.\n"
                    "At least two deploys are required before a rollback.",
                    code=EXIT_USER_ABORTED,
                )
            return state.previous[0]

        return self._prompt_target(state, releases, current_digest)

    def _digest_dir_exists(self, conn, value: str) -> bool:
        """Digest-vs-version discrimination: a release dir exists per digest."""
        _, is_dir = conn.run(
            f"test -d {serverops.q(self._release_path(value))}",
            warn=True,
            hide=True,
        )
        return bool(is_dir)

    def _check_digest_target(self, conn, target: str) -> str:
        """Discriminate presence of an exact digest target on the server."""
        if not self._digest_dir_exists(conn, target):
            raise cappa.Exit(
                f"Release '{target}' is not available on the server - it was"
                " pruned when old releases were garbage-collected.\nRedeploy"
                " the old commit/tag instead.",
                code=EXIT_USER_ABORTED,
            )
        _, exists = conn.run(
            f"test -f {serverops.q(f'{self._release_path(target)}/manifest.json')}",
            warn=True,
            hide=True,
        )
        if not exists:
            raise cappa.Exit(
                f"Release directory '{target}' exists but is incomplete (no"
                " manifest.json) - it cannot be activated. Re-run"
                " `fujin deploy --force` to rebuild it.",
                code=EXIT_USER_ABORTED,
            )
        return target

    def _target_by_version(
        self, conn, state: store.State, releases: list[str], version: str
    ) -> str:
        """Resolve a ``--to <version>`` against kept release manifests only.

        Only releases the server's GC keeps are candidates (users never see
        raw ``ls releases/`` output); reading every kept release's manifest
        is cheap (few releases kept). The target discrimen is ambiguous on
        no or multiple matches.
        """
        kept = self._kept_releases(state, releases)
        candidates: list[str] = []
        for digest in releases:
            if digest not in kept:
                continue  # garbage-collected releases are never targets
            raw = serverops.read_remote_file(
                conn, f"{self._release_path(digest)}/manifest.json"
            )
            if raw is None:
                continue  # staging residue or corrupt dir: not a candidate
            try:
                manifest_version = decode_manifest(raw).version
            except Exception:
                continue
            if self._version_matches(manifest_version, version):
                candidates.append(normalize_digest(digest))
        if not candidates:
            raise cappa.Exit(
                f"No kept release with version '{version}' on the server"
                " (it may be garbage-collected).",
                code=EXIT_USER_ABORTED,
            )
        if len(candidates) > 1:
            raise cappa.Exit(
                f"Version '{version}' is ambiguous; multiple releases carry it:"
                f" {', '.join(candidates)} -- pass the digest instead.",
                code=EXIT_USER_ABORTED,
            )
        return self._check_digest_target(conn, candidates[0])

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
                    code=EXIT_USER_ABORTED,
                )
            self.output.info("No previous versions available for rollback")
            raise cappa.Exit(code=EXIT_OK)

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
            raise cappa.Exit(code=EXIT_USER_ABORTED) from None
        return candidates[index - 1]

    # ------------------------------------------------------------------
    # Rollback execution
    # ------------------------------------------------------------------

    def _activation_step(self, description: str, func, *args) -> None:
        """Run an activation step, classifying any failure as exit code 6.

        Every step after the target release is chosen is past the point of
        no return: the ``current`` symlink may already point at the target.
        A failure here is never a user error (1) or a plain deploy failure
        (4) - the server state needs manual inspection.
        """
        try:
            func(*args)
        except (transfer.TransferError, serverops.ServerOpsError, CommandError) as exc:
            message = getattr(exc, "message", None) or str(exc)
            raise cappa.Exit(
                f"Rollback failed while {description}: {message}\n"
                "The server may be in a partially rolled back state and needs"
                " manual inspection; fujin does not attempt a second rollback.",
                code=EXIT_ROLLBACK_FAILED,
            ) from None

    def _perform_rollback(self, conn) -> None:
        app_dir = self.config.app_dir

        state = serverops.read_state(conn, app_dir)
        if state is None or not state.current:
            raise cappa.Exit(
                "No release store state found (store/state.json missing or"
                " empty). Deploy at least once with this version of fujin.",
                code=EXIT_USER_ABORTED,
            )

        current_manifest = serverops.read_current_manifest(conn, app_dir)
        if current_manifest is None:
            raise cappa.Exit(
                "The live release's manifest could not be read - the release"
                " tree is incomplete.",
                code=EXIT_USER_ABORTED,
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

        self._activation_step(
            "swapping the current release symlink",
            transfer.swap_current,
            conn,
            app_dir,
            target,
        )

        unit_files, dropins, oneshots = self._collect_units(
            conn, target_path, target_manifest
        )
        if unit_files or dropins:
            self._activation_step(
                "installing the target release's systemd units",
                serverops.install_units,
                conn,
                self.config.app_name,
                unit_files,
                dropins,
            )

        self._activation_step(
            "converging replica instances",
            self._converge,
            conn,
            target_manifest,
        )
        self._activation_step(
            "restarting the target release's units",
            self._restart,
            conn,
            current_manifest,
            target_manifest,
            unit_files,
            oneshots,
        )
        self._install_caddyfile(conn, target_path, target_manifest)

        at = datetime.now(UTC).isoformat()
        new_state = record_rollback(state, target, at)
        self._activation_step(
            "recording the rollback state",
            serverops.write_state,
            conn,
            app_dir,
            new_state,
        )
        self._gc(conn, new_state, releases)

        self.output.success(f"Rollback to release {target} completed successfully!")

    def _read_target_manifest(self, conn, target: str, target_path: str) -> Manifest:
        raw = serverops.read_remote_file(
            conn,
            f"{target_path}/manifest.json",
            fatal=(
                f"Release '{target}' is incomplete: its manifest.json is missing"
                " or unreadable, so it cannot be rolled back to."
            ),
        )
        try:
            return decode_manifest(raw)
        except Exception as exc:
            raise cappa.Exit(
                f"Release '{target}' manifest is corrupt: {exc}",
                code=EXIT_USER_ABORTED,
            ) from None

    def _verify_target(
        self, conn, target: str, target_path: str, manifest: Manifest
    ) -> None:
        result = transfer.verify_staging(
            conn, target_path, manifest_to_dict(manifest, include_created_at=False)
        )
        if not result.ok:
            raise cappa.Exit(
                transfer.verify_report(target_path, result),
                code=EXIT_USER_ABORTED,
            )

    # ------------------------------------------------------------------
    # Units + services
    # ------------------------------------------------------------------

    def _collect_units(
        self, conn, target_path: str, manifest: Manifest
    ) -> tuple[dict[str, str], dict[str, dict[str, str]], set[str]]:
        """Unit files + dropins of the target release, read from its systemd/ tree.

        An unreadable unit aborts the rollback (a silently partial unit set
        would install a broken application state).
        """
        unit_files, dropins, oneshots, failed = serverops.collect_release_units(
            conn, target_path, manifest
        )
        if failed:
            raise cappa.Exit(
                "Cannot read unit file(s) "
                f"'{', '.join(failed)}' from the target release tree.",
                code=EXIT_ROLLBACK_FAILED,
            )
        return unit_files, dropins, oneshots

    def _desired_units(self, manifest: Manifest) -> list[str]:
        """Desired instance/unit names implied by the target release tree."""
        app_name = self.config.app_name
        prefix = f"{app_name}-"
        desired: list[str] = []
        for path in sorted(manifest.entries):
            entry = manifest.entries[path]
            if entry.kind != "unit":
                continue
            name = serverops.unit_rel_name(path)
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

        Presence is the union of enabled and running instances (a manually
        stopped instance is restarted); singular service units are enabled/
        started by the original deploy and stay untouched here (they keep
        running across a rollback).
        """
        desired = [unit for unit in self._desired_units(target_manifest) if "@" in unit]
        start, stop = serverops.template_instance_plan(
            conn, self.config.app_name, desired
        )
        if stop and not serverops.stop_and_disable(conn, stop):
            raise cappa.Exit(
                "Rollback failed while stopping extra service instances"
                " (no nested rollback is attempted); inspect the server state.",
                code=EXIT_ROLLBACK_FAILED,
            )
        if start and not serverops.enable_and_start(conn, start):
            raise cappa.Exit(
                "Rollback failed while starting missing service instances"
                " (no nested rollback is attempted); inspect the server state.",
                code=EXIT_ROLLBACK_FAILED,
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
            f"sudo systemctl restart --quiet {' '.join(serverops.q(s) for s in specs)}",
            warn=True,
        )
        if not ok:
            raise cappa.Exit(
                "Rollback failed while restarting services (no nested rollback is"
                " attempted); inspect the application for a failed state.",
                code=EXIT_ROLLBACK_FAILED,
            )

    def _install_caddyfile(self, conn, target_path: str, manifest: Manifest) -> None:
        caddy_entry = manifest.entries.get("Caddyfile")
        if caddy_entry is None or caddy_entry.kind != "caddy":
            return
        content = serverops.read_remote_file(conn, f"{target_path}/Caddyfile")
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
        serverops.collect_garbage(
            conn,
            self.config.app_dir,
            new_state,
            releases,
            self.config.versions_to_keep,
            self.output,
        )
