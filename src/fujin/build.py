"""Build orchestration and the local content-addressed build cache (#81).

Produces a *staging tree* (release-tree layout minus ``.env``) plus a
``bundle_key`` over all build inputs. Deploy (#81 wave 3b) consumes the
staging tree / ``files`` spec, renders ``.env`` afterwards, and transfers.

Cache layout (``.fujin/cache/bundles/<bundle_key>/``):

- the staging tree itself (``systemd/``, ``Caddyfile``, ``assets/``, ``bin/``)
- ``built.json``: ``{bundle_key, files: {path: sha256}}`` (NO ``.env``, no
  secret values -- the env is rendered later by deploy).

Design notes:

- Interpolation of systemd/Caddyfile sources is done here, so the cached
  staging tree is exactly what deploy previously assembled; unresolved
  template variables are left as-is (same policy as the pre-existing deploy
  flow) and logged as warnings.
- On a cache hit, staged file hashes are re-verified against ``built.json``;
  any mismatch (corrupted/partial cache) forces a full rebuild.
- Interpolated unit/Caddyfile content can legitimately drift between runs
  (e.g. a ``{version}`` reference) without changing ``bundle_key`` (the key
  hashes raw sources); on a verified hit those files are refreshed in place
  from freshly interpolated source so the staging tree never goes stale.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import msgspec

from fujin.config import Config, HostConfig, InstallationMode, get_git_short_hash
from fujin.errors import BuildError, DeploymentError
from fujin.formatting import safe_format
from fujin.manifest import InputFile, bundle_key, hash_bytes

logger = logging.getLogger(__name__)

CACHE_ROOT_DIR = "bundles"  # under ``.fujin/cache/``
BUILD_LOCK_NAME = ".build.lock"  # under ``.fujin/cache/``
BUILT_FILENAME = "built.json"
SYSTEMD_DIR = "systemd"
CADDYFILE_NAME = "Caddyfile"
ASSETS_DIR = "assets"
BIN_DIR = "bin"
REQUIREMENTS_TREE_PATH = "requirements.txt"


class BuildResult(msgspec.Struct, frozen=True):
    """Outcome of :func:`run_build`."""

    #: local staging tree directory (release-tree layout, no ``.env``)
    staging_dir: Path
    #: content-addressed key over all build inputs
    bundle_key: str
    #: release-tree path -> input file spec (what transfer/install consumes)
    files: dict[str, InputFile]
    #: local path of the built dist file inside the staging tree
    dist_path: Path
    #: sha256 of the dist file
    dist_sha256: str


class _BuiltRecord(msgspec.Struct, kw_only=True):
    """Serialized form of ``built.json``."""

    bundle_key: str
    files: dict[str, str]
    #: ``config.local_version`` at build time. Older cache entries lack it;
    #: an empty value means "same as the current local version".
    version: str = ""


# =============================================================================================
# Source gathering
# =============================================================================================


def _interpolation_context(config: Config, host: HostConfig) -> dict[str, object]:
    """Variables available to unit/Caddyfile templates (deploy parity)."""
    context: dict[str, object] = {
        "app_name": config.app_name,
        "app_user": config.app_user,
        "version": config.version,
        "app_dir": config.app_dir,
        "install_dir": config.install_dir,
        # Persistent app data, outside the immutable release tree (#74).
        "shared_dir": f"{config.app_dir}/shared",
        "user": host.user,
    }
    # Add unit names to context for cross-service references (e.g., After={web_socket});
    # mirror deploy.py: singleton socket/timer units always, plus service units only
    # when there is no socket/timer and the service is not a replica template.
    for du in config.deployed_units:
        if du.socket_file:
            context[f"{du.name}_socket"] = du.template_socket_name
        if du.timer_file:
            context[f"{du.name}_timer"] = du.template_timer_name
        if not du.socket_file and not du.timer_file and not du.is_template:
            context[f"{du.name}"] = du.template_service_name
    return context


def _gather_sources(
    config: Config,
    *,
    host: HostConfig,
    assets: dict[str, str | bytes] | None,
) -> tuple[
    dict[str, str],  # raw unit/dropin sources, stable keys
    dict[str, str],  # interpolated unit/dropin tree content: "systemd/<name>" -> text
    str | None,  # interpolated Caddyfile
    dict[str, str],  # interpolated asset content: "assets/<name>" -> text
    set[str],  # unresolved template variables
]:
    """Collect and interpolate every client-known non-code input."""
    context = _interpolation_context(config, host)

    # Staging tree paths use the DEPLOYED unit identity (``{app_name}-{name}[].service``),
    # not the local file names: the tree naming must match what lands in
    # /etc/systemd/system/ and in manifest.json (store unit identity). Local file
    # names are kept as ``unit_sources`` keys, which are the bundle_key inputs.
    unit_sources: dict[str, str] = {}
    unit_tree: dict[str, str] = {}
    unresolved: set[str] = set()

    def resolve(key: str, raw: str) -> str:
        resolved, found = safe_format(raw, **context)
        unresolved.update(found)
        return resolved

    for du in config.deployed_units:
        unit_sources[f"service/{du.service_file.name}"] = du.service_file.read_text()
        unit_tree[f"{SYSTEMD_DIR}/{du.template_service_name}"] = resolve(
            f"unit/{du.service_file.name}", du.service_file.read_text()
        )
        for unit_file, template_name in (
            (du.socket_file, du.template_socket_name),
            (du.timer_file, du.template_timer_name),
        ):
            if unit_file is None:
                continue
            unit_sources[f"unit/{unit_file.name}"] = unit_file.read_text()
            unit_tree[f"{SYSTEMD_DIR}/{template_name}"] = resolve(
                f"unit/{unit_file.name}", unit_file.read_text()
            )

    local_systemd = config.local_config_dir / SYSTEMD_DIR
    common_dir = local_systemd / "common.d"
    if common_dir.exists():
        for dropin in common_dir.glob("*.conf"):
            key = f"dropin/common.d/{dropin.name}"
            unit_sources[key] = dropin.read_text()
            unit_tree[f"{SYSTEMD_DIR}/common.d/{dropin.name}"] = resolve(
                key, unit_sources[key]
            )

    # Per-service dropin dirs are named after the LOCAL unit file
    # ("{name}.service.d"); the staging tree uses the deployed unit identity.
    for du in config.deployed_units:
        local_dropin_dir = local_systemd / f"{du.name}.service.d"
        if not local_dropin_dir.is_dir():
            continue
        for dropin in local_dropin_dir.glob("*.conf"):
            key = f"dropin/{du.name}.service.d/{dropin.name}"
            unit_sources[key] = dropin.read_text()
            unit_tree[f"{SYSTEMD_DIR}/{du.template_service_name}.d/{dropin.name}"] = (
                resolve(key, unit_sources[key])
            )

    caddyfile: str | None = None
    if config.caddyfile_exists:
        caddyfile = resolve("caddy", config.caddyfile_path.read_text())

    asset_tree: dict[str, str] = {}
    for name, content in (assets or {}).items():
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        asset_tree[f"{ASSETS_DIR}/{name}"] = resolve(f"asset/{name}", text)

    return unit_sources, unit_tree, caddyfile, asset_tree, unresolved


def _requirements_content(config: Config, *, after_build: bool) -> str:
    """Read the requirements file.

    A missing file is only fatal after the build ran: deploy parity is that
    build commands may generate the requirements file (see deploy.py).
    """
    if not config.requirements:
        return ""
    req_path = Path(config.requirements)
    if not req_path.exists():
        if after_build:
            raise BuildError(
                f"Requirements file not found: {config.requirements}\n\n"
                "Troubleshooting:\n"
                "  - Ensure your build_command generates the requirements file\n"
                "  - Check that the 'requirements' path in fujin.toml is correct\n"
                f"  - Try running: uv pip compile pyproject.toml -o {config.requirements}"
            )
        return ""
    return req_path.read_text()


# =============================================================================================
# Build command
# =============================================================================================


def _run_build_command(config: Config) -> None:
    """Run ``build_command`` with the invocation semantics of the deploy flow
    (shell, non-maskable errors, ``build_timeout``)."""
    try:
        logger.debug("Build command: %s", config.build_command)
        subprocess.run(
            config.build_command,
            check=True,
            shell=True,
            timeout=config.build_timeout,
            env=_build_command_env(config),
        )
    except subprocess.TimeoutExpired:
        raise BuildError("Build timed out", command=config.build_command) from None
    except subprocess.CalledProcessError as e:
        raise BuildError("Build failed", command=config.build_command) from e


def _build_command_env(config: Config) -> dict[str, str]:
    """Environment for the build command. Inherits ``os.environ`` exactly like
    the pre-existing deploy flow so local build tooling keeps working."""
    return dict(os.environ)


# =============================================================================================
# Staging tree
# =============================================================================================


def _assemble_staging(
    cache_dir: Path,
    config: Config,
    *,
    unit_tree: dict[str, str],
    caddyfile: str | None,
    asset_tree: dict[str, str],
    requirements: str,
) -> tuple[dict[str, InputFile], Path, str]:
    """(Re)assemble the staging tree inside ``cache_dir``.

    Returns ``(files, dist_path, dist_sha256)``. ``files`` is the client-known
    InputFile spec keyed by release-tree path.
    """
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True)

    files: dict[str, InputFile] = {}
    for tree_path, content in unit_tree.items():
        full = cache_dir / tree_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        files[tree_path] = InputFile(content=content.encode("utf-8"))

    if caddyfile is not None:
        (cache_dir / CADDYFILE_NAME).write_text(caddyfile)
        files[CADDYFILE_NAME] = InputFile(content=caddyfile.encode("utf-8"))

    for tree_path, content in asset_tree.items():
        full = cache_dir / tree_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        files[tree_path] = InputFile(content=content.encode("utf-8"))

    distfile_path = config.get_distfile_path(config.version)
    if not distfile_path.exists():
        raise BuildError(
            f"Distfile not found after build: {distfile_path}\n\n"
            "Troubleshooting:\n"
            "  - Verify your build_command in fujin.toml is correct\n"
            "  - Check that the 'distfile' path in fujin.toml matches the build output"
        )

    dist_name = _distfile_name_for(config)
    dist_tree_path = f"{BIN_DIR}/{dist_name}"
    dist_target = cache_dir / dist_tree_path
    dist_target.parent.mkdir()
    shutil.copy(distfile_path, dist_target)
    dist_content = dist_target.read_bytes()
    dist_path, dist_sha256 = dist_target, hash_bytes(dist_content)
    files[dist_tree_path] = InputFile(content=dist_content, mode=0o755)

    if requirements:
        (cache_dir / REQUIREMENTS_TREE_PATH).write_text(requirements)
        files[REQUIREMENTS_TREE_PATH] = InputFile(
            content=requirements.encode("utf-8"), mode=0o644
        )

    return files, dist_path, dist_sha256


def _distfile_name_for(config: Config) -> str:
    """Dist file name in the staging tree: binary mode installs it as
    ``bin/{app_name}``; python mode keeps the distfile's own name."""
    if config.installation_mode is InstallationMode.BINARY:
        return config.app_name
    return config.get_distfile_path(config.version).name


# =============================================================================================
# built.json bookkeeping
# =============================================================================================


def _write_built_record(
    cache_dir: Path, bundle_key: str, files: dict[str, InputFile], version: str
):
    record = _BuiltRecord(
        bundle_key=bundle_key,
        version=version,
        files={
            path: hash_bytes(spec.content or b"")
            for path, spec in sorted(files.items())
        },
    )
    (cache_dir / BUILT_FILENAME).write_bytes(msgspec.json.encode(record))


def _load_built_record(cache_dir: Path) -> _BuiltRecord | None:
    record_path = cache_dir / BUILT_FILENAME
    if not record_path.exists():
        return None
    try:
        record = msgspec.json.decode(record_path.read_bytes(), type=_BuiltRecord)
    except (msgspec.ValidationError, msgspec.DecodeError):
        return None
    if record.bundle_key != cache_dir.name:
        return None
    return record


def _verify_staged_files(
    cache_dir: Path, record: _BuiltRecord
) -> dict[str, bytes] | None:
    """sha256 re-check of every staged file against ``built.json``.

    Returns ``{tree_path: content}`` on success (each file read exactly once,
    so a cache hit does not read the same bytes again), or None on any
    missing/mismatching entry.
    """
    verified: dict[str, bytes] = {}
    for tree_path, expected in record.files.items():
        staged = cache_dir / tree_path
        if not staged.is_file():
            return None
        content = staged.read_bytes()
        if hash_bytes(content) != expected:
            return None
        verified[tree_path] = content
    return verified


def _find_cached_dist(
    files: dict[str, InputFile], staging_dir: Path
) -> tuple[Path, str]:
    """Find the ``bin/...`` dist entry in the cached staging tree."""
    for tree_path in sorted(files):
        if tree_path.startswith(BIN_DIR + "/"):
            spec = files[tree_path]
            content = spec.content if spec.content is not None else b""
            return staging_dir / tree_path, hash_bytes(content)
    raise BuildError(
        "Cached bundle is missing its dist file; clear the build cache and retry"
    )  # pragma: no cover


# =============================================================================================
# Entry point
# =============================================================================================


@dataclass(frozen=True)
class Prebuild:
    """Everything computable about a build before ``build_command`` runs.

    :func:`prebuild` is the single pre-build computation shared by
    :func:`run_build` and ``deploy --dry-run``. It also owns the
    "nothing to build" guard, so both paths fail identically.
    """

    host: HostConfig
    bundle_key: str
    unit_sources: dict[str, str]
    unit_tree: dict[str, str]
    caddyfile: str | None
    caddyfile_source: str | None
    asset_tree: dict[str, str]
    assets: dict[str, str | bytes] | None
    requirements: str
    pyproject_text: str | None
    unresolved: set[str]

    def key_with_requirements(self, config: Config, requirements: str) -> str:
        """Recompute the key with the post-build requirements content."""
        return _compute_bundle_key(
            config,
            self.host,
            unit_sources=self.unit_sources,
            assets=self.assets,
            caddyfile_source=self.caddyfile_source,
            requirements=requirements,
            pyproject_text=self.pyproject_text,
        )


def _compute_bundle_key(
    config: Config,
    host: HostConfig,
    *,
    unit_sources: dict[str, str],
    assets: dict[str, str | bytes] | None,
    caddyfile_source: str | None,
    requirements: str,
    pyproject_text: str | None,
) -> str:
    return bundle_key(
        config,
        host,
        unit_sources=dict(unit_sources),
        assets=assets,
        caddyfile=caddyfile_source,
        requirements=requirements,
        pyproject=pyproject_text,
    )


def prebuild(
    config: Config,
    *,
    host: HostConfig | None = None,
    assets: dict[str, str | bytes] | None = None,
) -> Prebuild:
    """Gather build inputs and compute the pre-build bundle key.

    Raises :class:`DeploymentError` when there is nothing to build, exactly
    like a real deploy (the dry run uses this same call).
    """
    if not config.deployed_units:
        raise DeploymentError("No systemd units found, nothing to build")

    host = host if host is not None else config.select_host(None)
    unit_sources, unit_tree, caddyfile, asset_tree, unresolved = _gather_sources(
        config, host=host, assets=assets
    )
    if unresolved:
        logger.warning(
            "Found unresolved variables in configuration files: %s\n"
            "Available variables: %s\n"
            "These will be left as-is (e.g., {variable_name}) in deployed files.",
            ", ".join(sorted(unresolved)),
            ", ".join(sorted(_interpolation_context(config, host))),
        )

    requirements = _requirements_content(config, after_build=False)
    pyproject_path = Path("pyproject.toml")
    pyproject_text = pyproject_path.read_text() if pyproject_path.exists() else None
    caddyfile_source = None if caddyfile is None else config.caddyfile_path.read_text()
    return Prebuild(
        host=host,
        bundle_key=_compute_bundle_key(
            config,
            host,
            unit_sources=unit_sources,
            assets=assets,
            caddyfile_source=caddyfile_source,
            requirements=requirements,
            pyproject_text=pyproject_text,
        ),
        unit_sources=dict(unit_sources),
        unit_tree=dict(unit_tree),
        caddyfile=caddyfile,
        caddyfile_source=caddyfile_source,
        asset_tree=dict(asset_tree),
        assets=assets,
        requirements=requirements,
        pyproject_text=pyproject_text,
        unresolved=set(unresolved),
    )


@contextlib.contextmanager
def build_lock(config: Config):
    """Serialize concurrent local builds of the same project.

    The cache is written in place (``_assemble_staging`` replaces the bundle
    directory), so two builds of the same project racing on
    ``.fujin/cache/bundles/<key>/`` can corrupt each other. An exclusive
    ``flock`` on ``.fujin/cache/.build.lock`` serializes them; the lock is
    advisory and scope-local to this project's cache directory.
    """
    lock_dir = config.local_config_dir / "cache"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / BUILD_LOCK_NAME).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_build(
    config: Config,
    *,
    no_cache: bool = False,
    host: HostConfig | None = None,
    assets: dict[str, str | bytes] | None = None,
    lock: bool = True,
) -> BuildResult:
    """Build the app (or reuse the local build cache) and return the staging tree.

    Concurrent builds of the same project (e.g. two deploys) serialize on
    :func:`build_lock`; ``--no-cache`` still rebuilds from scratch.

    ``lock=False`` is for a caller that already holds :func:`build_lock`
    (the deploy flow keeps it across build + transfer): ``flock`` is per open
    file description, so acquiring it again on a second descriptor would
    deadlock. Direct callers keep ``lock=True``.
    """
    if not lock:
        return _run_build_locked(config, no_cache=no_cache, host=host, assets=assets)
    with build_lock(config):
        return _run_build_locked(config, no_cache=no_cache, host=host, assets=assets)


def _run_build_locked(
    config: Config,
    *,
    no_cache: bool = False,
    host: HostConfig | None = None,
    assets: dict[str, str | bytes] | None = None,
) -> BuildResult:
    """Build the app (or reuse the local build cache) and return the staging tree.

    ``host`` defaults to the first configured host and scopes the bundle key
    (the config fingerprint is per host, so each target has its own cache
    entry); it is also used for template interpolation of the deploy user.
    ``assets`` is None for now -- the config has no ``[assets]`` section yet
    (#81 note: supported gracefully). Raises BuildError / DeploymentError
    (cappa.Exit subclasses) on failure.
    """
    pre = prebuild(config, host=host, assets=assets)
    host = pre.host
    if commit := get_git_short_hash():
        logger.debug("Git commit: %s", commit)

    key = pre.bundle_key
    cache_dir = config.local_config_dir / "cache" / CACHE_ROOT_DIR / key

    if not no_cache:
        record = _load_built_record(cache_dir)
        verified = (
            _verify_staged_files(cache_dir, record) if record is not None else None
        )
        if record is not None and verified is not None:
            logger.debug("Build cache hit: %s", key)
            # The verification pass already read every file once: reuse those
            # bytes instead of reading the staging tree a second time.
            files: dict[str, InputFile] = {
                tree_path: InputFile(
                    content=content,
                    mode=0o755 if tree_path.startswith(BIN_DIR + "/") else None,
                )
                for tree_path, content in verified.items()
            }
            dist_path, dist_sha256 = _find_cached_dist(files, cache_dir)
            # Refresh interpolated unit/Caddyfile/asset content that drifted
            # without busting the key (the hashed sources did not change).
            for tree_path in (*pre.unit_tree, *pre.asset_tree):
                refreshed = (
                    pre.asset_tree[tree_path]
                    if tree_path in pre.asset_tree
                    else pre.unit_tree[tree_path]
                )
                current = verified.get(tree_path)
                if current is None or current.decode("utf-8") != refreshed:
                    (cache_dir / tree_path).write_bytes(refreshed.encode("utf-8"))
                    files[tree_path] = InputFile(content=refreshed.encode("utf-8"))
            _write_built_record(cache_dir, key, files, config.local_version)
            return BuildResult(
                staging_dir=cache_dir,
                bundle_key=key,
                files=files,
                dist_path=dist_path,
                dist_sha256=dist_sha256,
            )
        if record is not None:
            logger.warning("Build cache verification failed; rebuilding")

    logger.debug("Build cache miss; building")
    _run_build_command(config)
    requirements = _requirements_content(config, after_build=True)
    # The build may have generated the requirements file (or changed it):
    # rebuild the key from the post-build content so the cache is stored
    # under the key a subsequent deploy will actually compute (a generated
    # file that did not exist pre-build hashes to nothing in the pre-key).
    if requirements != pre.requirements:
        repaired_key = pre.key_with_requirements(config, requirements)
        if repaired_key != key:
            logger.info(
                "Requirements changed during build; caching under the post-build key"
            )
            key = repaired_key
            cache_dir = config.local_config_dir / "cache" / CACHE_ROOT_DIR / key
    files, dist_path, dist_sha256 = _assemble_staging(
        cache_dir,
        config,
        unit_tree=pre.unit_tree,
        caddyfile=pre.caddyfile,
        asset_tree=pre.asset_tree,
        requirements=requirements,
    )
    _write_built_record(cache_dir, key, files, config.local_version)
    return BuildResult(
        staging_dir=cache_dir,
        bundle_key=key,
        files=files,
        dist_path=dist_path,
        dist_sha256=dist_sha256,
    )


def clean_build_cache(config: Config) -> int:
    """Remove cached bundles for this app. Returns the number of bundles removed."""
    cache_dir = config.local_config_dir / "cache" / CACHE_ROOT_DIR
    if not cache_dir.is_dir():
        return 0
    removed = 0
    for bundle in cache_dir.iterdir():
        if bundle.is_dir():
            shutil.rmtree(bundle)
            removed += 1
    return removed
