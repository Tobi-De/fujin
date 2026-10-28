"""Tests for the local build cache (#81): bundle_key, hit/miss, secret exclusion."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import msgspec
import pytest

from fujin.build import (
    BUILT_FILENAME,
    clean_build_cache,
    prebuild,
    run_build,
)
from fujin.config import Config
from fujin.errors import BuildError, DeploymentError


def make_config(tmp_path: Path, **overrides) -> Config:
    base = {
        "app": "testapp",
        "version": "1.0.0",
        "build_command": "echo building",
        "installation_mode": "python-package",
        "python_version": "3.11",
        "distfile": "dist/testapp-{version}-py3-none-any.whl",
        "hosts": [{"address": "example.com", "user": "deploy"}],
    }
    base.update(overrides)
    return msgspec.convert(base, type=Config)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A fake project: dist dir + a systemd unit."""
    monkeypatch.chdir(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "testapp-1.0.0-py3-none-any.whl").write_bytes(b"wheel-bytes")
    (tmp_path / "pyproject.toml").write_text("[project]\nversion = '1.0.0'\n")
    unit = tmp_path / ".fujin" / "systemd"
    unit.mkdir(parents=True)
    (unit / "testapp.service").write_text(
        "[Unit]\nDescription=testapp\n[Service]\nExecStart=uv run testapp\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    return tmp_path


def build_cached_bundles(project: Path) -> int:
    cache = project / ".fujin" / "cache" / "bundles"
    return len([p for p in cache.iterdir() if p.is_dir()])


def test_caddyfile_shared_dir_is_interpolated(project):
    """{shared_dir} resolves in a Caddyfile like any other template variable."""
    from fujin.build import _gather_sources

    (project / ".fujin" / "Caddyfile").write_text(
        "example.com {\n    root * {shared_dir}\n    file_server\n}\n"
    )
    config = make_config(project)

    _sources, _tree, caddyfile, _assets, unresolved, _rewrites = _gather_sources(
        config, host=config.hosts[0], assets=None
    )

    assert unresolved == set()
    assert caddyfile is not None
    assert "root * /opt/fujin/testapp/shared" in caddyfile


def test_build_produces_staging_tree(project):
    config = make_config(project)
    result = run_build(config)

    staging = result.staging_dir
    assert (staging / "systemd" / "testapp-testapp.service").read_text() == (
        (project / ".fujin" / "systemd" / "testapp.service").read_text()
    )
    dist_path = staging / "bin" / "testapp-1.0.0-py3-none-any.whl"
    assert dist_path.read_bytes() == b"wheel-bytes"
    assert result.dist_path == dist_path
    assert (
        result.dist_sha256.startswith(("a", "b", "c")) or len(result.dist_sha256) == 64
    )
    assert len(result.dist_sha256) == 64
    assert result.bundle_key
    assert result.files["bin/testapp-1.0.0-py3-none-any.whl"]


def test_cache_hit_skips_build_command(project, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "fujin.build.subprocess.run",
        lambda *a, **kw: calls.append(1) or type("R", (), {"returncode": 0})(),
    )
    config = make_config(project)
    first = run_build(config)
    assert calls  # miss runs the build command
    calls.clear()

    second = run_build(config)
    assert not calls  # hit skips the build command
    assert second.bundle_key == first.bundle_key
    assert second.dist_sha256 == first.dist_sha256
    assert second.staging_dir == first.staging_dir


def test_cache_hit_reads_each_staged_file_once(project, monkeypatch):
    """Verification bytes are reused: no staged file is read twice on a hit."""
    config = make_config(project)
    first = run_build(config)
    reads: dict[Path, int] = {}
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text

    def counting_read_bytes(self):
        reads[self] = reads.get(self, 0) + 1
        return real_read_bytes(self)

    def counting_read_text(self, *args, **kwargs):
        reads[self] = reads.get(self, 0) + 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    monkeypatch.setattr(Path, "read_text", counting_read_text)
    second = run_build(config)
    assert second.bundle_key == first.bundle_key

    staged_reads = {
        path: count
        for path, count in reads.items()
        if first.staging_dir in path.parents
    }
    assert staged_reads, "cache hit did not read the staging tree"
    assert {path: n for path, n in staged_reads.items() if n != 1} == {}


def test_each_input_busts_cache(project):
    config = make_config(project)
    first_key = run_build(config).bundle_key

    # unit source change busts
    unit = project / ".fujin" / "systemd" / "testapp.service"
    unit.write_text(unit.read_text() + "# comment\n")
    assert run_build(config).bundle_key != first_key

    # caddyfile change busts
    caddy = project / ".fujin" / "Caddyfile"
    caddy.write_text("localhost {\n reverse_proxy {testapp}\n}\n")
    keys = {run_build(config).bundle_key}
    caddy.write_text("other.domain {\n reverse_proxy testapp\n}\n")
    keys |= {run_build(config).bundle_key}
    assert len(keys) == 2

    # requirements change busts
    config = make_config(project, requirements="requirements.txt")
    req = project / "requirements.txt"
    req.write_text("flask==1.0\n")
    key_with_req = run_build(config).bundle_key
    req.write_text("flask==2.0\n")
    assert run_build(config).bundle_key != key_with_req

    # config fingerprint busts (build_command change)
    assert run_build(make_config(project, build_command="echo other")).bundle_key != (
        run_build(config).bundle_key
    )


def test_corrupt_cache_rebuilds(project, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "fujin.build.subprocess.run",
        lambda *a, **kw: calls.append(1) or subprocess.CompletedProcess([], 0),
    )
    config = make_config(project)
    first = run_build(config)
    staged_dist = first.dist_path
    # corrupt the staged dist file: hashes no longer match built.json
    staged_dist.write_bytes(b"tampered")
    calls.clear()

    rebuilt = run_build(config)
    assert calls  # verification failure forced a rebuild
    assert rebuilt.bundle_key == first.bundle_key
    assert rebuilt.dist_path.read_bytes() == b"wheel-bytes"
    assert rebuilt.dist_sha256 == first.dist_sha256


def test_no_cache_bypasses(project, monkeypatch):
    calls = []
    real_run = subprocess.run

    def spy(cmd, *a, **kw):
        calls.append(str(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr("fujin.build.subprocess.run", spy)
    config = make_config(project)
    run_build(config)
    calls.clear()
    result = run_build(config, no_cache=True)
    assert any("echo building" in c for c in calls)
    # staging tree rebuilt in place under the same bundle key
    assert result.files
    assert build_cached_bundles(project) == 1


def test_cache_never_contains_env_or_secrets(project):
    host_env = {
        "address": "example.com",
        "user": "deploy",
        "env": "SECRET_TOKEN=supersecret123\nDB_PASS=dbpass\n",
    }
    config = make_config(project, hosts=[host_env])
    run_build(config)

    cache_root = project / ".fujin" / "cache" / "bundles"
    for bundle in cache_root.iterdir():
        for path in bundle.rglob("*"):
            if path.is_file():
                content = path.read_text(errors="ignore").lower()
                assert "supersecret123" not in content
                assert ".env" not in path.name and path.name != ".env"
                for needle in ("supersecret123", "dbpass"):
                    assert needle not in content
    assert (
        project
        / ".fujin"
        / "cache"
        / "bundles"
        / list(cache_root.iterdir())[0].name
        / BUILT_FILENAME
    ).exists()


def test_unit_tree_uses_deployed_names(project):
    """Local file names are rewritten to the deployed unit identity in the tree."""
    project.joinpath(".fujin/systemd/web.service").write_text("[Service]\n")
    project.joinpath(".fujin/systemd/web.service.d").mkdir()
    project.joinpath(".fujin/systemd/web.service.d/hardening.conf").write_text(
        "[Unit]\n"
    )
    config = make_config(project, replicas={"web": 2})
    result = run_build(config)

    staging = result.staging_dir
    assert (staging / "systemd" / "testapp-web@.service").exists()
    assert (staging / "systemd" / "testapp-web@.service.d" / "hardening.conf").exists()
    assert "systemd/testapp-web@.service" in result.files
    # bundle_key source keys stay local-named
    assert "service/web.service" not in result.files  # tree paths, not sources


def test_missing_distfile_raises(project):
    (project / "dist" / "testapp-1.0.0-py3-none-any.whl").unlink()
    # build_command echo does not create the distfile -> BuildError
    with pytest.raises(BuildError):
        run_build(make_config(project, build_command="true"))


def test_clean_build_cache(project):
    config = make_config(project)
    run_build(config)
    assert build_cached_bundles(project) == 1
    assert clean_build_cache(config) == 1
    assert build_cached_bundles(project) == 0
    assert clean_build_cache(config) == 0


def test_pyproject_change_busts_cache(project):
    """pyproject.toml is a build input: edits that drive a generated
    requirements.txt (or dist metadata) must not cache-hit a stale tree."""
    first_key = run_build(make_config(project)).bundle_key
    (project / "pyproject.toml").write_text(
        '[project]\nversion = "1.0.0"\n[dependency-groups]\nfoo = "pip"\n'
    )
    assert run_build(make_config(project)).bundle_key != first_key


def test_generated_requirements_cache_lands_on_post_build_key(project, monkeypatch):
    """Requirements generated by build_command: the first deploy has no
    pre-build file (hashes as absent); after the build the cache must be
    stored under the key a repeat deploy computes (with the file present
    and its content)."""
    project / "requirements.txt"
    monkeypatch.delenv("GENERATED", raising=False)
    config = make_config(project, requirements="requirements.txt")

    # Simulate: build_command generates the requirements file from
    # pyproject content that is part of the tree.
    monkeypatch.setattr(
        "fujin.build._run_build_command",
        lambda _config: (
            (project / "requirements.txt").write_text("django==5.1\n"),
            (project / "sub" if False else None),
        ),
    )
    first = run_build(config)
    assert (project / "requirements.txt").exists()
    second = run_build(config)
    assert second.bundle_key == first.bundle_key, (
        "a repeat deploy with the generated file present must reuse the "
        "post-build-keyed cache, else every deploy rebuilds"
    )
    staged_req = (second.staging_dir / "requirements.txt").read_text()
    assert staged_req == "django==5.1\n"


def test_host_scoped_build_cache(project):
    """Each selected host gets its own bundle: a dist built for staging is
    never the cache entry reused when deploying prod."""
    config = make_config(
        project,
        hosts=[
            {"name": "staging", "address": "staging.example.com", "user": "deploy"},
            {"name": "prod", "address": "prod.example.com", "user": "deploy"},
        ],
    )
    staging = run_build(config, host=config.select_host("staging"))
    prod = run_build(config, host=config.select_host("prod"))

    assert staging.bundle_key != prod.bundle_key
    assert build_cached_bundles(project) == 2
    # A repeat build for the same host stays a cache hit.
    assert (
        run_build(config, host=config.select_host("staging")).bundle_key
        == staging.bundle_key
    )


def test_run_build_uses_the_shared_prebuild(project):
    """run_build and deploy --dry-run compute the same pre-build key."""
    config = make_config(project)

    pre = prebuild(config)
    result = run_build(config)

    assert result.bundle_key == pre.bundle_key
    assert pre.unit_tree == {
        "systemd/testapp-testapp.service": (
            project / ".fujin/systemd/testapp.service"
        ).read_text()
    }


def test_built_record_stores_the_local_version(project):
    config = make_config(project)
    result = run_build(config)

    record = json.loads((result.staging_dir / BUILT_FILENAME).read_text())
    assert record["version"] == config.local_version


def test_prebuild_and_run_build_reject_empty_systemd_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = make_config(tmp_path)

    with pytest.raises(DeploymentError) as pre_err:
        prebuild(config)
    with pytest.raises(DeploymentError) as build_err:
        run_build(config)

    assert str(pre_err.value.message) == "No systemd units found, nothing to build"
    assert build_err.value.message == pre_err.value.message
    assert build_err.value.code == pre_err.value.code


def test_run_build_locks_by_default(project, monkeypatch):
    """Direct callers get the lock; the deploy flow opts out because it holds
    the same lock around build + transfer."""
    import contextlib

    acquisitions = []

    @contextlib.contextmanager
    def fake_lock(_config):
        acquisitions.append("acquire")
        try:
            yield
        finally:
            acquisitions.append("release")

    monkeypatch.setattr("fujin.build.build_lock", fake_lock)

    run_build(make_config(project), no_cache=True)

    assert acquisitions == ["acquire", "release"]


def test_run_build_lock_false_does_not_acquire(project, monkeypatch):
    """``lock=False`` must not take the lock a second time (nested flock on a
    second descriptor would deadlock)."""

    def exploding_lock(_config):  # pragma: no cover - assertion below
        raise AssertionError("run_build(lock=False) must not take the lock")

    monkeypatch.setattr("fujin.build.build_lock", exploding_lock)

    result = run_build(make_config(project), no_cache=True, lock=False)

    assert result.staging_dir.is_dir()


def test_concurrent_builds_serialize_on_the_local_lock(project, monkeypatch):
    """The cache is written in place, so parallel builds of the same project
    must serialize on ``.fujin/cache/.build.lock`` (no interleaved runs)."""
    import fcntl
    import threading
    import time

    counter_lock = threading.Lock()
    state = {"active": 0, "max_active": 0}
    errors: list[BaseException] = []

    def slow_run(*_a, **_kw):
        with counter_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.2)
        with counter_lock:
            state["active"] -= 1
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr("fujin.build.subprocess.run", slow_run)

    def worker() -> None:
        try:
            run_build(make_config(project), no_cache=True)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors
    assert state["max_active"] == 1, "build commands overlapped despite the lock"
    # Both finished; the lock is released and free to re-acquire.
    lock_path = project / ".fujin" / "cache" / ".build.lock"
    assert lock_path.exists()
    with lock_path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
