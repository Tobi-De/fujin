from __future__ import annotations

import logging
import os
import shlex
import signal
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import cappa
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from fujin import build, connection
from fujin import manifest as m
from fujin import serverops, store, transfer
from fujin.audit import log_operation
from fujin.commands import BaseCommand
from fujin.config import get_git_short_hash
from fujin.errors import DeploymentError
from fujin.formatting import safe_format
from fujin.redaction import clear_secrets, redact, register_secrets
from fujin.secrets import (
    env_values,
    render_template,
    resolve_secrets,
    secret_refs,
)
from rich.prompt import Confirm

logger = logging.getLogger(__name__)

EXIT_SERVICE_START_FAILED = 3


def _resolve_live_digest(conn, app_dir: str, state: store.State | None) -> str | None:
    """Digest of the release the ``current`` symlink actually points at.

    The symlink is the source of truth (state.json is a cache that can be
    stale); falls back to ``state.current`` when the symlink is missing.
    """
    out, ok = conn.run(
        f"readlink {shlex.quote(app_dir + '/current')}", warn=True, hide=True
    )
    target = out.strip() if ok and out.strip() else ""
    if target:
        digest = target.rstrip("/").rsplit("/", 1)[-1]
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return digest
    return state.current if state and state.current else None


def collect_local_units(
    files: Mapping[str, m.InputFile], config
) -> tuple[dict[str, str], dict[str, dict[str, str]], set[str]]:
    """Unit files + dropins + oneshot units from the local staging tree.

    See :func:`serverops.expand_dropins` for the return contract.
    """
    unit_contents = [
        (serverops.unit_rel_name(path), (spec.content or b"").decode("utf-8"))
        for path, spec in sorted(files.items())
        if path.startswith("systemd/")
    ]
    return serverops.expand_dropins(unit_contents)


def merge_server_built(
    client_manifest: m.Manifest, server_built: Mapping[str, dict]
) -> m.Manifest:
    """Merge server-hashed ``venv/**`` / ``bin/**`` entries into the manifest.

    The server reports ``{"sha256": hex, "mode": octalStr|int}`` for regular
    files and ``{"link": target, "mode": ...}`` for symlinks, keyed by
    staging-relative path. Rebuilt deterministically; the final
    :func:`fujin.manifest.release_digest` covers the complete tree.
    """
    payload = m.manifest_to_dict(client_manifest, include_created_at=False)
    entries: dict[str, dict] = dict(payload["entries"])
    for path, info in dict(server_built).items():
        mode = info.get("mode", 0)
        if isinstance(mode, int):
            mode = f"{mode:04o}"
        entry: dict = {
            "path": path,
            "sha256": info.get("sha256"),
            "mode": mode,
            "kind": m.classify_path(path).value,
        }
        if "link" in info:
            entry["link"] = info["link"]
        entries[path] = entry
    payload["entries"] = entries
    return m.manifest_from_dict(payload)


@cappa.command(
    help="Deploy your application to the server",
)
@dataclass
class Deploy(BaseCommand):
    no_input: Annotated[
        bool,
        cappa.Arg(
            long="--no-input",
            help="Do not prompt for input",
        ),
    ] = False
    full_restart: Annotated[
        bool,
        cappa.Arg(
            long="--full-restart",
            help="Restart every unit instead of reload-or-restart",
        ),
    ] = False
    no_rollback: Annotated[
        bool,
        cappa.Arg(
            long="--no-rollback",
            help="Disable automatic rollback when services fail to start",
        ),
    ] = False
    force: Annotated[
        bool,
        cappa.Arg(
            long="--force",
            help="Perform a full deploy even when nothing changed "
            "(e.g. to re-run post-install hooks and migrations)",
        ),
    ] = False
    no_cache: Annotated[
        bool,
        cappa.Arg(
            long="--no-cache",
            help="Rebuild from scratch instead of reusing the local build cache",
        ),
    ] = False
    compress_zstd: Annotated[
        bool,
        cappa.Arg(
            long="--compress-zstd",
            help="Use zstd compression for the rsync transfer",
        ),
    ] = False

    def __call__(self):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))

        config = self.config
        host = self.selected_host
        app_dir = config.app_dir

        # A fresh deploy starts with an empty redaction registry; resolved
        # secrets are registered right after resolution below.
        clear_secrets()

        logger.debug("Starting deployment for %s", config.app_name)
        logger.debug("Target host: %s", host.address)

        secret_config = config.effective_secrets(host)
        # The host env overlays the project template (#84); both are raw
        # template text, so the merge precedes parsing/rendering.
        env_template = config.effective_env(host)
        # Refs come from the env *template*, not the rendered file: `$$REF`
        # renders a literal `$REF` and must not be treated as unresolved.
        secret_aliases = secret_refs(env_template)
        if secret_config:
            self.output.info("Resolving secrets from configuration...")
            parsed_env = resolve_secrets(env_template, secret_config)
            # Track every resolved secret value so all output paths render it
            # as `<$REF>` instead of the raw value (never registered in the
            # build cache; #77).
            # Register only values that really are secret references;
            # registering every env value would make unrelated output
            # (DEBUG=1, etc.) redact as a false positive.
            resolved_values = env_values(parsed_env)
            register_secrets(
                {k: resolved_values[k] for k in secret_aliases if k in resolved_values},
                secret_aliases,
            )
        else:
            parsed_env = render_template(env_template)

        context = build._interpolation_context(config, host)
        resolved_env, unresolved = safe_format(parsed_env, **context)
        if unresolved:
            self.output.error(
                f"Unresolved template variables in env file:"
                f" {', '.join(sorted(unresolved))}."
            )
            raise DeploymentError(
                "Unresolved template variables in env file:"
                f" {', '.join(sorted(unresolved))}. Deployment aborted"
                " (fail-closed)."
            )
        self._fail_closed_on_unresolved_secrets(
            resolved_env, secret_aliases, adapter_configured=secret_config is not None
        )

        with connection.connection(host=host, compress=False) as conn:
            self._notice_old_layout(conn)
            serverops.ensure_app_user(conn, config.app_user or config.app_name)
            serverops.ensure_layout(
                conn, app_dir, config.app_user or config.app_name, host.user
            )
            lock = serverops.acquire_lock(conn, app_dir)
            try:
                # Inside the lock: a concurrent deploy's staging dir is not
                # ours to delete.
                transfer.sweep_stale_staging(conn, app_dir)
                self._deploy(conn, resolved_env, context)
            finally:
                serverops.release_lock(conn, app_dir, lock)

    # =======================================================================================
    # Orchestration
    # =======================================================================================

    def _deploy(self, conn, resolved_env: str, context: Mapping[str, object]) -> None:
        config = self.config
        app_dir = config.app_dir

        self.output.info("Building application ...")
        try:
            result = build.run_build(
                config, no_cache=self.no_cache, host=self.selected_host
            )
        except DeploymentError as exc:
            raw = str(exc.message)
            wrapped = redact(raw)
            # If the value only lived in the original exception's message,
            # the cause chain must not carry it into tracebacks either.
            raise DeploymentError(wrapped) from (exc if wrapped == raw else None)
        except cappa.Exit as exc:
            # Build failures surface as cappa.Exit too (BuildError derives
            # from it): preserve the exit code, but never echo a secret
            # through the message.
            if exc.message:
                raise cappa.Exit(redact(str(exc.message)), code=exc.code) from None
            raise
        except Exception as exc:
            raw = str(exc)
            wrapped = redact(raw)
            raise DeploymentError(wrapped) from (exc if wrapped == raw else None)
        files = dict(result.files)

        # The rendered .env is deploy-time state (kind ``env``), never
        # build-cache content (#77): it is written into the staging tree for
        # the transfer only, and removed again afterwards so the cache stays
        # secret-free.
        client_files: dict[str, m.InputFile | str | bytes] = {
            **files,
            m.ENV_PATH: m.InputFile(content=resolved_env.encode()),
        }
        client_manifest = m.build_manifest(
            app_name=config.app_name,
            version=config.local_version,
            config_fingerprint=m.config_fingerprint(config),
            dist_sha256=result.dist_sha256,
            files=client_files,
        )
        client_digest = m.release_digest(client_manifest)

        state = serverops.read_state(conn, app_dir)
        if state is not None and not self.force and store.is_noop(client_digest, state):
            self.output.info(
                f"No changes detected; release {client_digest} is already live."
            )
            if self.full_restart:
                # --full-restart still restarts everything, but nothing is
                # transferred or committed: reload the live release's units
                # over the (unchanged) manifests (#79 excludes oneshots).
                self.output.info(
                    "--full-restart: restarting all units without a transfer."
                )
                prev_manifest = serverops.read_current_manifest(conn, app_dir)
                _unit_files, _dropins, oneshots = collect_local_units(files, config)
                self._restart(
                    conn,
                    prev_manifest or client_manifest,
                    client_manifest,
                    oneshots,
                    force_full_restart=True,
                )
            self._converge(conn, client_manifest)
            self._run_hooks(conn, "post_start", context, release_path=None)
            self._gc(conn, state, self._list_releases(conn))
            log_operation(
                conn,
                config.app_name,
                "noop",
                host=self.selected_host.name or self.selected_host.address,
                version=config.local_version,
            )
            self.output.success("Deployment already up to date!")
            return

        self._show_deployment_summary()
        staging_dir = result.staging_dir
        try:
            # The rendered .env is deploy-time state (kind ``env``), written
            # into the staging tree here: rsync_to_staging mirrors the on-disk
            # tree and the SCP fallback walks it, so the server-side chmod
            # 0640 and the verify step both need the real bytes present. The
            # ``finally`` below removes it (and manifest.json) again so the
            # build cache stays secret-free (#77).
            (staging_dir / m.ENV_PATH).write_bytes(resolved_env.encode())
            self._transfer_and_activate(
                conn,
                staging_dir,
                result,
                files,
                client_manifest,
                client_digest,
                state,
                context,
            )
        finally:
            for name in (m.MANIFEST_FILENAME, m.ENV_PATH):
                try:
                    (staging_dir / name).unlink()
                except OSError:  # pragma: no cover - best effort
                    pass

    def _transfer_and_activate(
        self,
        conn,
        staging_dir: Path,
        result: build.BuildResult,
        files: dict[str, m.InputFile],
        client_manifest,
        client_digest: str,
        state: store.State | None,
        context: Mapping[str, object],
    ) -> None:
        config = self.config
        app_dir = config.app_dir

        # Normalize staged file modes to their manifest entry modes, then
        # write the client manifest into the local staging tree so the remote
        # verify script (and the merged rewrite below) can find it.
        for path, entry in client_manifest.entries.items():
            staged = staging_dir / path
            if staged.exists():
                staged.chmod(entry.mode)
        (staging_dir / m.MANIFEST_FILENAME).write_bytes(
            m.canonical_manifest_bytes(client_manifest)
        )

        # The live release's manifest is read BEFORE any swap: it is the diff
        # base for the restart set and the rollback target on failure.
        prev_manifest = serverops.read_current_manifest(conn, app_dir)
        live_digest = _resolve_live_digest(conn, app_dir, state)
        first_deploy = live_digest is None and prev_manifest is None

        remote_staging = f"{app_dir}/releases/.staging-{os.getpid()}"
        conn.run(f"mkdir -p {serverops.q(remote_staging)}", warn=True, hide=True)

        mfile = m.CADDYFILE_PATH
        try:
            # Dedup: hardlink every client-known entry that is byte-identical
            # (same sha256) to the live release. rsync's own --link-dest is
            # unusable (RsyncProject/rsync#715), so this is explicit.
            unchanged: dict[str, int] = {}
            if prev_manifest is not None and live_digest:
                for path, entry in client_manifest.entries.items():
                    prev_entry = prev_manifest.entries.get(path)
                    if (
                        prev_entry is not None
                        and prev_entry.sha256 == entry.sha256
                        and not path.startswith("venv/")
                    ):
                        unchanged[path] = entry.mode
            if unchanged:
                linked, failed = serverops.link_unchanged(
                    conn,
                    remote_staging,
                    f"{app_dir}/releases/{live_digest}",
                    unchanged,
                )
                if failed:
                    logger.info(
                        "Hardlink dedup missed %d file(s); rsync will transfer them",
                        len(failed),
                    )
                else:
                    self.output.info(
                        f"Deduped {linked} unchanged file(s) against the live release."
                    )

            self.output.info("Uploading release tree...")
            # ``built.json`` is local build-cache bookkeeping, not release
            # content: nothing in the tree references it and it would land
            # in the immutable release dir otherwise.
            transfer.rsync_to_staging(
                conn,
                str(staging_dir),
                remote_staging,
                ignore_existing=bool(unchanged),
                compress_zstd=self.compress_zstd,
                modes={
                    path: entry.mode for path, entry in client_manifest.entries.items()
                },
                exclude={build.BUILT_FILENAME},
            )
            serverops.set_envfile_mode(conn, remote_staging)

            self.output.info("Installing application code on the server...")
            tree_requirements = staging_dir / build.REQUIREMENTS_TREE_PATH
            dist_tree_path = next(
                path
                for path in sorted(result.files)
                if path.startswith(build.BIN_DIR + "/")
            )
            serverops.install_code_into_staging(
                conn,
                remote_staging,
                config.installation_mode.value,
                config.python_version,
                (str(tree_requirements) if tree_requirements.is_file() else None),
                dist_tree_path,
                app_name=config.app_name,
            )

            serverops.normalize_staging_ownership(
                conn, remote_staging, config.app_user or config.app_name
            )

            server_built = serverops.hash_server_built(
                conn, remote_staging, config.installation_mode.value
            )
            final_manifest = merge_server_built(client_manifest, server_built)
            final_digest = m.release_digest(final_manifest)
            self._write_remote_manifest(conn, remote_staging, final_manifest)

            release_path = f"{app_dir}/releases/{final_digest}"
            # Identical content can already be committed on the server
            # (e.g. `deploy --force` with unchanged content, or the first
            # deploy after a rollback): commit_release refuses to overwrite
            # an existing release dir, so reuse the committed tree instead
            # of letting the deploy die after the upload.
            reuse_verdict = self._try_reuse_committed_release(
                conn, release_path, remote_staging, final_manifest
            )
            # Pre-install hooks: the old release is still live.
            self._run_hooks(conn, "pre_install", context, release_path=None)

            if reuse_verdict == "reuse":
                transfer.cleanup_staging(conn, remote_staging)
            elif reuse_verdict == "replaced-staging":
                # Staging already verified and the corrupt release dir was
                # removed: commit directly.
                self.output.info("Committing release...")
                transfer.commit_release(conn, remote_staging, final_digest)
            else:
                self.output.info("Verifying staged release tree...")
                verify = transfer.verify_staging(
                    conn, remote_staging, m.manifest_to_dict(final_manifest)
                )
                if not verify.ok:
                    raise DeploymentError(
                        redact(transfer.verify_report(remote_staging, verify))
                    )
                self.output.info("Committing release...")
                transfer.commit_release(conn, remote_staging, final_digest)

            unit_files, dropins, oneshots = collect_local_units(files, config)
            if unit_files or dropins:
                self.output.info("Installing systemd units...")
                serverops.install_units(conn, config.app_name, unit_files, dropins)

            caddy_entry = final_manifest.entries.get(m.CADDYFILE_PATH)
            if caddy_entry is not None and caddy_entry.kind is m.EntryKind.CADDY:
                self._install_caddyfile(conn, staging_dir / mfile)

            # Post-install hooks: committed, NOT live yet; they see the new
            # release dir and its rendered .env.
            self._run_hooks(conn, "post_install", context, release_path=release_path)

            self.output.info("Activating release...")
            transfer.swap_current(conn, app_dir, final_digest)

            self._restart(
                conn,
                prev_manifest,
                final_manifest,
                oneshots,
                force_full_restart=self.full_restart or first_deploy,
            )
            self._converge(conn, final_manifest)
            self._wait_for_units(conn, final_manifest, oneshots)
            self._run_hooks(conn, "post_start", context, release_path=release_path)

            at = datetime.now(UTC).isoformat()
            new_state = store.record_deploy(
                state if state is not None else store.State(),
                final_digest,
                at,
                client_digest=client_digest,
            )
            serverops.write_state(conn, app_dir, new_state)
            self._gc(conn, new_state, self._list_releases(conn))
            log_operation(
                conn,
                config.app_name,
                "deploy",
                host=self.selected_host.name or self.selected_host.address,
                version=config.local_version,
                git_commit=get_git_short_hash(),
            )
        except DeploymentError as exc:
            transfer.cleanup_staging(conn, remote_staging)
            wrapped_msg = str(exc.message)
            raise DeploymentError(redact(wrapped_msg)) from (
                exc if redact(wrapped_msg) == wrapped_msg else None
            )
        except cappa.Exit:
            # A plain cappa.Exit (e.g. a service-start failure offering
            # rollback) carries its own exit code and is never re-wrapped.
            transfer.cleanup_staging(conn, remote_staging)
            raise
        except Exception as exc:
            transfer.cleanup_staging(conn, remote_staging)
            wrapped_msg = f"Deployment failed: {exc}"
            raise DeploymentError(redact(wrapped_msg)) from (
                exc if redact(wrapped_msg) == wrapped_msg else None
            )

        self.output.success("Deployment completed successfully!")
        domain = config.get_domain_name()
        if domain and final_manifest.entries.get(m.CADDYFILE_PATH) is not None:
            self.output.info(f"Application is available at: https://{domain}")

    def _write_remote_manifest(
        self, conn, remote_staging: str, final_manifest: m.Manifest
    ) -> None:
        """Overwrite ``manifest.json`` in staging with the merged (server +
        client) manifest, before verify + commit.

        Transferred via SCP: the merged manifest can be hundreds of KB
        (thousands of ``venv/**`` entries), which exceeds what can safely
        travel inside a shell command string (a manifest sent that way hung
        the SSH channel forever).
        """
        manifest_bytes = m.canonical_manifest_bytes(final_manifest)
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(manifest_bytes)
            tmp_path = tmp.name
        try:
            conn.put(tmp_path, f"{remote_staging}/{m.MANIFEST_FILENAME}")
        finally:
            os.unlink(tmp_path)

    def _try_reuse_committed_release(
        self,
        conn,
        release_path: str,
        remote_staging: str,
        final_manifest: m.Manifest,
    ) -> bool:
        """Reuse an already-committed release dir with identical content.

        Returns ``"reuse"`` when ``releases/<digest>`` exists, verifies
        against the fresh manifest, and is byte-identical: the freshly
        uploaded staging tree is then redundant. Returns None when no dir
        exists.

        When the existing dir verifies as INCOMPLETE (e.g. a crashed
        earlier deploy left a partially committed tree), the freshly
        uploaded staging tree is verified instead and, on success, the
        corrupt dir is removed; the helper returns ``"replaced-staging"``
        (the staging tree is then already verified and only needs the
        commit). A verification failure of both raises a DeploymentError
        (the old release is still live).
        """
        _, dir_exists = conn.run(
            f"test -d {serverops.q(release_path)}", warn=True, hide=True
        )
        if not dir_exists:
            return None

        self.output.info(
            f"Release {release_path.rsplit('/', 1)[-1]} already committed;"
            " verifying the existing tree..."
        )
        existing_verify = transfer.verify_staging(
            conn, release_path, m.manifest_to_dict(final_manifest)
        )
        if existing_verify.ok:
            self.output.info(
                f"Release {release_path.rsplit('/', 1)[-1]} already committed;"
                " reusing it."
            )
            return "reuse"

        self.output.warning(
            f"Existing release directory {release_path} is incomplete"
            " (failed verification); replacing it with the freshly"
            " uploaded tree."
        )
        staging_verify = transfer.verify_staging(
            conn, remote_staging, m.manifest_to_dict(final_manifest)
        )
        if not staging_verify.ok:
            raise DeploymentError(
                redact(transfer.verify_report(remote_staging, staging_verify))
            )
        conn.run(f"rm -rf {serverops.q(release_path)}", warn=True, hide=True)
        return "replaced-staging"

    # =======================================================================================
    # Restart set / convergence / readiness
    # =======================================================================================

    def _restart(
        self,
        conn,
        prev_manifest: m.Manifest | None,
        final_manifest: m.Manifest,
        oneshots: set[str],
        *,
        force_full_restart: bool,
    ) -> None:
        """Restart per the diff-based restart set (#79): a changed unit file
        restarts exactly that unit (its instances), env changes restart the
        env-fed service units, code changes restart all app units; sockets and
        timers restart only when their own file changed; oneshots excluded."""
        if force_full_restart:
            # Full restart covers every app unit incl. sockets/timers (a
            # restarted timer starts; the old installer did the same), but
            # Type=oneshot units are still excluded: they complete and exit,
            # restarting them only causes spurious failures (#79).
            unit_names: Iterable[str] = sorted(
                (
                    set(serverops.service_unit_names(final_manifest))
                    | set(serverops.socket_timer_unit_names(final_manifest))
                )
                - set(oneshots)
            )
        else:
            unit_names = store.restart_set(
                prev_manifest,
                final_manifest,
                env_fed_units=[
                    name for name in serverops.service_unit_names(final_manifest)
                ],
                oneshot_units=oneshots,
            )
        specs = serverops.expand_patterns(unit_names)
        if not specs:
            self.output.info("No unit needs a restart for this deploy.")
            return
        verb = "restart" if self.full_restart else "reload-or-restart"
        # systemctl enable does not accept globs (restart does) and hard-fails
        # on units without an [Install] section: enable only concrete
        # non-template units, non-fatal (boot survival is best-effort here);
        # template instances are enabled by the convergence step right after.
        enable_specs = sorted(
            name
            for name in set(serverops.service_unit_names(final_manifest))
            | serverops.socket_timer_unit_names(final_manifest)
            if not name.endswith("@.service") and name not in oneshots
        )
        if enable_specs:
            _, enable_ok = conn.run(
                f"sudo systemctl enable {serverops.q(*enable_specs)}", warn=True
            )
            if not enable_ok:
                self.output.warning(
                    "Could not enable some units (missing [Install] section?); "
                    "they will not start on boot."
                )
        _, ok = conn.run(
            f"sudo systemctl {verb} {serverops.q(*specs)}",
            warn=True,
        )
        if not ok:
            # Activation steps declare restart failure -> rollback (#76);
            # _handle_failed_units raises a cappa.Exit in all paths.
            self._handle_failed_units(conn, specs)

    def _converge(self, conn, manifest: m.Manifest) -> None:
        """Start missing / stop extra replica instances, both directions.

        Desired instances are ``svc@1..N`` from ``config.replicas``; singular
        service units keep running across deploys and are untouched here.
        Presence is the union of enabled and running instances, so a manually
        stopped (but still enabled) instance is started again on every deploy,
        including a no-op deploy, and leftovers of a removed template stop.
        """
        app_name = self.config.app_name
        desired: list[str] = []
        for path in sorted(manifest.entries):
            entry = manifest.entries[path]
            if entry.kind is not m.EntryKind.UNIT:
                continue
            name = serverops.unit_rel_name(path)
            if not name.endswith("@.service"):
                continue
            if not name.startswith(f"{app_name}-"):
                continue
            base = name.removeprefix(f"{app_name}-").removesuffix("@.service")
            replicas = self.config.replicas.get(base, 1)
            desired.extend(
                sorted(
                    store.desired_instances(name.removesuffix("@.service"), replicas)
                )
            )
        start, stop = serverops.template_instance_plan(conn, app_name, desired)
        if stop and not serverops.stop_and_disable(conn, stop):
            if getattr(self, "_rolling_back", False):
                # Convergence failure while rolling back: state.current still
                # points at the target, another rollback would be a no-op loop.
                raise DeploymentError(
                    "Failed while stopping extra service instances; inspect the server."
                )
            self._handle_failed_units(conn, stop)
        if start and not serverops.enable_and_start(conn, start):
            if getattr(self, "_rolling_back", False):
                raise DeploymentError(
                    "Failed while starting missing service instances; inspect the server."
                )
            self._handle_failed_units(conn, start)

    def _readiness_units(self, manifest: m.Manifest, oneshots: set[str]) -> list[str]:
        """Runtime service units to health-check, derived from the release
        manifest (same semantics the old installer used): oneshots and
        services that have a socket or timer in the tree are exempt (the
        socket/timer governs their lifecycle); template services poll their
        ``svc@1..N`` instances."""
        unit_names: set[str] = set()
        for path, entry in manifest.entries.items():
            if entry.kind is not m.EntryKind.UNIT:
                continue
            if name := store.unit_entry_name(path):
                unit_names.add(name)
        units_to_check: list[str] = []
        app_prefix = f"{self.config.app_name}-"
        for name in sorted(unit_names):
            if not name.endswith(".service"):
                continue
            if name in oneshots:
                continue
            base = name.removesuffix(".service").removesuffix("@")
            if f"{base}.socket" in unit_names or f"{base}.timer" in unit_names:
                continue
            if name.endswith("@.service"):
                service = name.removesuffix("@.service")
                if not service.startswith(app_prefix):
                    continue
                replicas = self.config.replicas.get(service[len(app_prefix) :], 1)
                units_to_check.extend(
                    sorted(
                        f"{i}.service"
                        for i in store.desired_instances(service, replicas)
                    )
                )
            else:
                units_to_check.append(name)
        return units_to_check

    def _wait_for_units(self, conn, manifest: m.Manifest, oneshots: set[str]) -> None:
        """Poll ``systemctl is-failed`` for immediately-running service units
        until every unit reports a status; any unit reported ``failed``
        triggers the failure/rollback path immediately."""
        units_to_check = self._readiness_units(manifest, oneshots)
        if not units_to_check:
            return
        for attempt in range(10):
            out, ok = conn.run(
                f"systemctl is-failed {' '.join(serverops.q(u) for u in sorted(units_to_check))}",
                warn=True,
                hide=True,
            )
            statuses = [line.strip() for line in out.splitlines() if line.strip()]
            failed = [
                unit
                for unit, status in zip(sorted(units_to_check), statuses, strict=False)
                if status == "failed"
            ]
            if failed:
                self._handle_failed_units(conn, failed)
                return
            if len(statuses) >= len(units_to_check):
                return
            if attempt < 9:
                time.sleep(0.3)
        self._handle_failed_units(conn, sorted(units_to_check))

    # =======================================================================================
    # Failure handling / auto rollback
    # =======================================================================================

    def _handle_failed_units(self, conn, failed: list[str]) -> None:
        self.output.error("Services failed to start!")
        for unit in failed:
            self.output.error(f"{unit} failed to start")
            log_out, _ = conn.run(
                f"journalctl -u {serverops.q(unit)} -n 30 --no-pager || true",
                warn=True,
                hide=True,
            )
            if log_out:
                self.output.output(log_out)

        if self.no_rollback:
            raise cappa.Exit(
                "Services failed to start. Rollback disabled via --no-rollback.",
                code=EXIT_SERVICE_START_FAILED,
            )
        state = serverops.read_state(conn, self.config.app_dir)
        # state.json is only written AFTER a successful deploy, so on a
        # failed deploy ``state.current`` is the release that was live
        # before this deploy started: that is the correct rollback target
        # (``previous[0]`` may point two deploys back and be stale).
        prev_digest = state.current if state and state.current else None
        if prev_digest is None:
            raise cappa.Exit(
                "Services failed to start and there is no previous release to"
                " roll back to.",
                code=EXIT_SERVICE_START_FAILED,
            )
        if not (self.no_input or self._confirm_rollback()):
            raise cappa.Exit(
                "Services failed to start; rollback cancelled.",
                code=EXIT_SERVICE_START_FAILED,
            )
        self._auto_rollback(conn, prev_digest)

    def _confirm_rollback(self) -> bool:
        try:
            return bool(
                Confirm.ask(
                    "\n[bold yellow]Services failed to start; proceed with"
                    " rollback?[/bold yellow]",
                    default=True,
                )
            )
        except (KeyboardInterrupt, EOFError):
            self.output.info("\nRollback cancelled.")
            return False

    def _auto_rollback(self, conn, prev_digest: str) -> None:
        """Swap back to the previous release, reinstall its units, restart it,
        record the failure in state, and exit 3."""
        self._rolling_back = True
        self.output.warning("Rolling back to the previous release...")
        app_dir = self.config.app_dir
        prev_path = f"{app_dir}/releases/{prev_digest}"

        prev_raw = serverops.read_remote_file(conn, f"{prev_path}/manifest.json")
        if prev_raw is None:
            raise cappa.Exit(
                f"Auto-rollback failed: previous release {prev_digest} is"
                " incomplete (no manifest.json); inspect the server manually.",
                code=EXIT_SERVICE_START_FAILED,
            )
        prev_manifest = m.decode_manifest(prev_raw)

        transfer.swap_current(conn, app_dir, prev_digest)

        unit_files, dropins, _oneshots, failed_units = serverops.collect_release_units(
            conn, prev_path, prev_manifest
        )
        if failed_units:
            # Best effort during auto-rollback: install what was readable.
            self.output.warning(
                "Could not read unit file(s) from the previous release: "
                + ", ".join(failed_units)
            )
        if unit_files or dropins:
            serverops.install_units(conn, self.config.app_name, unit_files, dropins)
        caddy_entry = prev_manifest.entries.get(m.CADDYFILE_PATH)
        if caddy_entry is not None and caddy_entry.kind is m.EntryKind.CADDY:
            # Caddy is running the new release's config; restore the previous
            # one before the restart (#80: env/units/code always restored
            # together).
            caddy_raw = serverops.read_remote_file(conn, f"{prev_path}/Caddyfile")
            if caddy_raw is None:
                self.output.warning(
                    "Previous release declares a Caddyfile but it could not be"
                    " read; caddy config unchanged."
                )
            else:
                installed = serverops.install_caddyfile(
                    conn, caddy_raw, self.config.caddy_config_path
                )
                if installed:
                    self.output.info(
                        "Caddy reloaded with the previous release's Caddyfile."
                    )
                else:
                    self.output.warning(
                        "Failed to apply the previous release's Caddyfile;"
                        " caddy config unchanged."
                    )
        specs = serverops.expand_patterns(
            name
            for path, entry in prev_manifest.entries.items()
            if entry.kind is m.EntryKind.UNIT
            and (name := serverops.unit_rel_name(path)).endswith(".service")
        )
        if specs:
            conn.run(
                f"sudo systemctl restart --quiet {' '.join(serverops.q(s) for s in specs)}",
                warn=True,
            )
            self._converge(conn, prev_manifest)

        state = serverops.read_state(conn, app_dir)
        if state is not None:
            new_state = store.record_rollback(
                state, prev_digest, datetime.now(UTC).isoformat()
            )
            serverops.write_state(conn, app_dir, new_state)
            self._gc(conn, new_state, self._list_releases(conn))
        log_operation(
            conn,
            self.config.app_name,
            "rollback",
            host=self.selected_host.name or self.selected_host.address,
        )
        raise cappa.Exit(
            f"Deployment failed; rolled back to release {prev_digest}.",
            code=EXIT_SERVICE_START_FAILED,
        )

    # =======================================================================================
    # Caddy
    # =======================================================================================

    def _install_caddyfile(self, conn, local_caddy: Path) -> None:
        if not local_caddy.is_file():
            self.output.warning(
                "The release declares a Caddyfile but it is unavailable; caddy"
                " config unchanged."
            )
            return
        content = local_caddy.read_text()
        installed = serverops.install_caddyfile(
            conn, content, self.config.caddy_config_path
        )
        if not installed:
            self.output.warning(
                "Failed to apply the new Caddyfile; the previous configuration"
                " was restored."
            )
        else:
            self.output.info("Caddy reloaded with the new configuration.")

    # =======================================================================================
    # Store / helpers
    # =======================================================================================

    def _list_releases(self, conn) -> list[str]:
        """Digests under releases/, newest (mtime) first; staging excluded."""
        return serverops.list_releases(conn, self.config.app_dir)

    def _gc(self, conn, state: store.State, releases: list[str]) -> None:
        """GC via store.gc_plan: keep current + previous[0] + newest releases."""
        serverops.collect_garbage(
            conn,
            self.config.app_dir,
            state,
            releases,
            self.config.versions_to_keep,
            self.output,
        )

    # =======================================================================================
    # Hooks
    # =======================================================================================

    def _run_hooks(
        self,
        conn,
        phase: str,
        context: Mapping[str, object],
        *,
        release_path: str | None,
    ) -> None:
        """Run a hook phase through the connection.

        ``release_path=None``: the old release is live — env comes from the
        live release's .env (or the legacy ``.install/.env``). Otherwise the
        env is the NEW release's .env, the hook runs in the release dir and
        ``{app_dir}`` inside hook commands resolves to the release dir.
        """
        hooks_config = self.config.hooks_config
        if not hooks_config:
            return
        commands = list(getattr(hooks_config, phase) or [])
        if not commands:
            return
        label = phase.replace("_", "-")
        self.output.info(f"Running {label} hooks...")

        if release_path is None:
            app_dir = self.config.app_dir
            select_env = (
                f"if [ -f {serverops.q(f'{app_dir}/current/.env')} ]; then "
                f"source {serverops.q(f'{app_dir}/current/.env')}; else "
                f"source {serverops.q(f'{self.config.install_dir}/.env')} 2>/dev/null; fi"
            )
            cwd: str | None = None
        else:
            select_env = f"source {serverops.q(f'{release_path}/.env')}"
            cwd = release_path
            context = {
                **context,
                "app_dir": release_path,
                "install_dir": release_path,
            }

        for raw in commands:
            cmd, unresolved_vars = safe_format(raw, **context)
            if unresolved_vars:
                # Fail closed on interpolation gaps: an unresolved variable
                # in a hook command is a broken template, not something to
                # run with a literal ``{name}`` placeholder in it. (Secret
                # values are never interpolated into hook commands.) Note:
                # raise before any execution; server ops would just echo it
                # into deploy logs.
                raise DeploymentError(
                    f"{label} hook '{raw}' references unknown variables"
                    f" {', '.join(sorted(unresolved_vars))}. Deployment"
                    " aborted (fail-closed)."
                )
            self.output.info(f"  [{label}] {cmd}")
            full_cmd = f"set -a; {select_env}; set +a; "
            if cwd:
                full_cmd += f"cd {serverops.q(cwd)} && "
            full_cmd += cmd
            conn.run(full_cmd, pty=True)

    # =======================================================================================
    # Secrets
    # =======================================================================================

    def _fail_closed_on_unresolved_secrets(
        self,
        env: str,
        secret_aliases: dict[str, str],
        *,
        adapter_configured: bool,
    ) -> None:
        """Abort when a ``$REF`` secret line survived the adapters unresolved.

        The refs come from the env *template* (``secret_aliases``), not from
        a scan of the rendered file: ``$$REF`` renders a literal ``$REF`` and
        must not abort. The ``system`` adapter resolves ``$REF`` values from
        the environment; a rendered value still starting with ``$`` (or a ref
        key missing from the output) means the secret was NOT resolved, and
        shipping it would leak the reference into the release tree instead of
        removing it. This is fail-closed: any unresolved secret ref aborts
        the deploy before any server I/O, and only the key/ref name is
        reported (never the raw value).

        ``adapter_configured`` distinguishes "the adapter left it unresolved"
        from "this host has no secret adapter at all" so the message does not
        blame a configuration that does not exist.
        """
        values = env_values(env)
        for key, ref in secret_aliases.items():
            value = values.get(key)
            if value is not None and not value.startswith("$"):
                continue
            if adapter_configured:
                shown = (
                    f"Unresolved secret reference '<${ref}>' for"
                    f" '{key}': the configured secret adapter did"
                    " not resolve it and deployment aborted (fail-closed)."
                )
                raised = (
                    f"Unresolved secret reference in '{key}':"
                    f" '<${ref}>' could not be resolved by the configured"
                    " secret adapter. Deployment aborted (fail-closed)."
                )
            else:
                shown = (
                    f"Unresolved secret reference '<${ref}>' for"
                    f" '{key}': no secret adapter is configured for this"
                    " host, so a '$REF' value cannot be resolved (use '$$'"
                    " for a literal '$'). Deployment aborted (fail-closed)."
                )
                raised = (
                    f"Unresolved secret reference in '{key}':"
                    f" '<${ref}>' cannot be resolved because no secret"
                    " adapter is configured for this host (use '$$' for a"
                    " literal '$'). Deployment aborted (fail-closed)."
                )
            self.output.error(shown)
            raise DeploymentError(raised)

    # =======================================================================================
    # Presentation
    # =======================================================================================

    def _notice_old_layout(self, conn) -> None:
        """Non-destructive old-layout notice: `.install/` present, `store/` absent."""
        _, has_old = conn.run(
            f"test -d {serverops.q(self.config.install_dir)}", warn=True, hide=True
        )
        _, has_store = conn.run(
            f"test -d {serverops.q(f'{self.config.app_dir}/store')}",
            warn=True,
            hide=True,
        )
        if has_old and not has_store:
            self.output.warning(
                "This application uses the legacy `.install/` layout. It is kept"
                " untouched; a fresh release tree (``current``, ``releases/``,"
                " ``store/``) is created next. The old directories are removed"
                " by `fujin down`."
            )

    def _show_deployment_summary(self):
        console = Console()
        config = self.config

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Key", style="bold cyan", width=12)
        table.add_column("Value")

        table.add_row("App", config.app_name)
        table.add_row("Version", config.local_version)
        host_display = self.selected_host.name if self.selected_host.name else "default"
        table.add_row("Host", f"{host_display} ({self.selected_host.address})")

        services_summary = []
        for du in config.deployed_units:
            if du.replicas > 1:
                services_summary.append(f"{du.name} ({du.replicas})")
            else:
                services_summary.append(du.name)
        if services_summary:
            table.add_row("Services", ", ".join(services_summary))
        table.add_row("Mode", config.installation_mode.value)

        panel = Panel(
            table,
            title="[bold]Deployment Summary[/bold]",
            border_style="blue",
            padding=(1, 1),
            width=60,
        )
        console.print(panel)
