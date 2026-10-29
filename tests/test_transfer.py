from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from fujin import transfer
from fujin.exit_codes import EXIT_HOST_UNREACHABLE
from fujin.transfer import (
    HostUnreachableError,
    ItemizedChange,
    TransferError,
    commit_release,
    parse_itemize,
    rsync_to_staging,
    sweep_stale_staging,
    swap_current,
    verify_report,
    verify_staging,
)


@dataclass
class RecordingConn:
    """Fake SSH2Connection recording every run/put call."""

    host: object = None
    runs: list = field(default_factory=list)
    puts: list = field(default_factory=list)
    responses: dict = field(default_factory=dict)  # command -> (out, ok)

    def run(
        self, command: str, warn: bool = False, pty: bool = False, hide: bool = False
    ):
        self.runs.append(command)
        if command in self.responses:
            return self.responses[command]
        return "", True

    def put(self, local: str, remote: str, verify: bool = False):
        self.puts.append((local, remote))


def make_host(**kw):
    base = {
        "user": "deploy",
        "address": "203.0.113.7",
        "port": 22,
        "key_filename": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def fake_rsync_run(monkeypatch, *, returncode=0, stdout="", stderr=""):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"argv": cmd, **kwargs})
        ns = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        ns.args = cmd
        return ns

    monkeypatch.setattr(transfer.subprocess, "run", fake_run)
    # avoid probing the real rsync binary for installed-ness
    monkeypatch.setattr(transfer, "_rsync_installed_locally", lambda: True)
    return calls


def local_script_conn(monkeypatch):
    """RecordingConn whose ``run`` executes the heredoc python script locally.

    The remote verification script is just python reading ``manifest.json``
    from the given root, so running it against a local tree exercises the
    real hashing/skip behaviour without SSH.
    """
    conn = RecordingConn(host=make_host())

    def run(command, **kw):
        conn.runs.append(command)
        head, _, rest = command.partition("\n")
        argv = shlex.split(head.split("<<", 1)[0])
        script = rest.split("\n_FUJIN_PY", 1)[0]
        proc = subprocess.run(
            [sys.executable, *argv[1:]],
            input=script,
            text=True,
            capture_output=True,
        )
        return proc.stdout, proc.returncode == 0

    monkeypatch.setattr(conn, "run", run)
    return conn


ITEMIZE_SAMPLE = """sending incremental file list
>f+++++++++ .env
>f.st...... app/main.py
cd+++++++++ assets/
cd+++++++++ scripts/
>f+++++++++ assets/logo.svg
.d..t...... systemd/
.Ls....g... venv/bin/python
total size is 12,345  speedup is 5.67
"""

SAMPLE_PARSED = [
    ItemizedChange(code=">f+++++++++", path=".env"),
    ItemizedChange(code=">f.st......", path="app/main.py"),
    ItemizedChange(code="cd+++++++++", path="assets"),
    ItemizedChange(code="cd+++++++++", path="scripts"),
    ItemizedChange(code=">f+++++++++", path="assets/logo.svg"),
    ItemizedChange(code=".d..t......", path="systemd"),
    ItemizedChange(code=".Ls....g...", path="venv/bin/python"),
]


def test_parse_itemize_real_sample():
    parsed = parse_itemize(ITEMIZE_SAMPLE)
    assert parsed == SAMPLE_PARSED


def test_parse_itemize_deleting_lines():
    parsed = parse_itemize("*deleting stale.txt\n*deleting temp/\n")
    assert parsed == [
        ItemizedChange(code="*deleting", path="stale.txt"),
        ItemizedChange(code="*deleting", path="temp"),
    ]


def test_parse_itemize_empty():
    assert parse_itemize("") == []


class TestLocalRsyncProbe:
    def test_uses_which_without_spawning_subprocess(self, monkeypatch):
        probed = []
        monkeypatch.setattr(
            transfer.shutil,
            "which",
            lambda name: probed.append(name) or "/usr/bin/rsync",
        )

        def no_spawn(*args, **kwargs):
            pytest.fail("local rsync probe must not spawn a subprocess")

        monkeypatch.setattr(transfer.subprocess, "run", no_spawn)

        assert transfer._rsync_installed_locally() is True
        assert probed == ["rsync"]

    def test_missing_rsync(self, monkeypatch):
        monkeypatch.setattr(transfer.shutil, "which", lambda name: None)
        assert transfer._rsync_installed_locally() is False


class TestRsyncToStaging:
    def test_exact_command_first_deploy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True, raising=False)
        calls = fake_rsync_run(monkeypatch)
        conn = RecordingConn(host=make_host())
        local = str(tmp_path / "bundle")

        rsync_to_staging(
            conn, local, "/opt/fujin/myapp/releases/.staging-42", ignore_existing=False
        )

        rsync_cmd = calls[-1]["argv"]
        assert rsync_cmd == [
            "rsync",
            "-rlptD",
            "--chmod=D755",
            "--itemize-changes",
            "-e",
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
            " -o ConnectTimeout=10 -o ControlMaster=auto"
            " -o ControlPath=/tmp/fujin-ssh-%i-%C -o ControlPersist=60s -p 22",
            f"{local}/",
            "deploy@203.0.113.7:/opt/fujin/myapp/releases/.staging-42",
        ]

    def test_exact_command_no_zstd_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True)
        calls = fake_rsync_run(monkeypatch)
        conn = RecordingConn(host=make_host())

        rsync_to_staging(
            conn,
            str(tmp_path),
            "/opt/fujin/a/releases/.staging-1",
            ignore_existing=False,
        )

        assert calls[-1]["argv"][3] == "--itemize-changes"
        assert "--compress-choice=zstd" not in calls[-1]["argv"]

    def test_ignore_existing_when_dedup_prelinked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True)
        calls = fake_rsync_run(monkeypatch)
        conn = RecordingConn(host=make_host())

        rsync_to_staging(
            conn,
            str(tmp_path),
            "/opt/fujin/a/releases/.staging-1",
            ignore_existing=True,
        )

        argv = calls[-1]["argv"]
        assert "--ignore-existing" in argv
        # flags must precede the source and destination
        assert argv.index("--ignore-existing") < argv.index("-e")
        # no --link-dest ever: the CVE-hardened receiver chokes on basis files
        assert all(not a.startswith("--link-dest") for a in argv)

    def test_no_ignore_existing_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True)
        calls = fake_rsync_run(monkeypatch)
        conn = RecordingConn(host=make_host())

        rsync_to_staging(
            conn,
            str(tmp_path),
            "/opt/fujin/a/releases/.staging-1",
            ignore_existing=False,
        )

        assert all(
            not a.startswith("--ignore-existing") and not a.startswith("--link-dest")
            for a in calls[-1]["argv"]
        )  # first deploy

    def test_key_option_included_when_key_filename(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True)
        calls = fake_rsync_run(monkeypatch)
        conn = RecordingConn(host=make_host(key_filename="~/.ssh/id_ed25519"))

        rsync_to_staging(
            conn,
            str(tmp_path),
            "/opt/fujin/a/releases/.staging-1",
            ignore_existing=False,
        )

        argv = calls[-1]["argv"]
        assert argv[argv.index("-e") + 1].startswith(
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
            " -o ConnectTimeout=10"
            " -o ControlMaster=auto"
            " -o ControlPath=/tmp/fujin-ssh-%i-%C"
            " -o ControlPersist=60s -p 22 -i "
        )

    def test_paths_quoted_alongside_local_trailing_slash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True)
        calls = fake_rsync_run(monkeypatch)
        conn = RecordingConn(host=make_host())

        rsync_to_staging(
            conn,
            str(tmp_path),
            "/opt/fujin/with space/releases/.staging-9",
            ignore_existing=False,
        )
        argv = calls[-1]["argv"]
        # spaces in remote transfer spec are passed wholesale (rsync remote
        # syntax requires escaping only within a single remote arg), locally
        # it stays a single argv entry because subprocess.run gets a list
        assert "deploy@203.0.113.7:/opt/fujin/with space/releases/.staging-9" in argv

    def test_itemize_output_parsed_and_returned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True)
        fake_rsync_run(monkeypatch, stdout=ITEMIZE_SAMPLE)
        conn = RecordingConn(host=make_host())

        result = rsync_to_staging(
            conn, str(tmp_path), "/x/releases/.staging-1", ignore_existing=False
        )

        assert result == SAMPLE_PARSED

    def test_missing_local_dir(self, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: False)
        conn = RecordingConn(host=make_host())
        with pytest.raises(TransferError) as exc_info:
            rsync_to_staging(conn, "/nowhere", "/x/.staging-1", ignore_existing=False)
        assert "missing" in exc_info.value.message

    def test_local_rsync_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True)
        monkeypatch.setattr(transfer, "_rsync_installed_locally", lambda: False)
        conn = RecordingConn(host=make_host())
        with pytest.raises(TransferError):
            rsync_to_staging(
                conn, str(tmp_path), "/x/.staging-1", ignore_existing=False
            )

    def test_remote_rsync_missing_actionable_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True)
        monkeypatch.setattr(transfer, "_rsync_installed_locally", lambda: True)
        conn = RecordingConn(
            host=make_host(),
            responses={"command -v rsync": ("", False)},
        )
        calls = fake_rsync_run(monkeypatch)
        with pytest.raises(TransferError) as exc_info:
            rsync_to_staging(
                conn, str(tmp_path), "/x/.staging-1", ignore_existing=False
            )
        assert "apt install rsync" in exc_info.value.message
        assert calls == []  # never invoked rsync locally

    def test_unreachable_host_distinct_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True)
        fake_rsync_run(
            monkeypatch,
            returncode=255,
            stderr="ssh: connect to host 203.0.113.7 port 22: Connection refused",
        )
        conn = RecordingConn(host=make_host())
        with pytest.raises(HostUnreachableError) as exc_info:
            rsync_to_staging(
                conn, str(tmp_path), "/x/.staging-1", ignore_existing=False
            )
        assert exc_info.value.code == EXIT_HOST_UNREACHABLE

    def test_rsync_generic_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transfer.Path, "is_dir", lambda self: True)
        fake_rsync_run(
            monkeypatch, returncode=23, stderr="rsync: partial transfer (1 file)"
        )
        conn = RecordingConn(host=make_host())
        with pytest.raises(TransferError) as exc_info:
            rsync_to_staging(
                conn, str(tmp_path), "/x/.staging-1", ignore_existing=False
            )
        assert not isinstance(exc_info.value, HostUnreachableError)
        assert "exit code 23" in exc_info.value.message


class TestVerifyStaging:
    def make_manifest_dict(self, sha="a" * 64, link=None):
        entry = {
            "path": "app/main.py",
            "sha256": sha,
            "mode": "0644",
            "kind": "code",
        }
        if link:
            entry["link"] = link
            del entry["sha256"]
        return {"entries": {"app/main.py": entry}}

    def test_perfect_match(self, monkeypatch):
        out = '__FUJIN_JSON__{"missing": [], "mismatch": {}, "link_mismatch": {}, "mode_mismatch": {}}'
        conn = RecordingConn(host=make_host())
        monkeypatch.setattr(
            conn, "run", lambda command, **kw: conn.runs.append(command) or (out, True)
        )

        result = verify_staging(
            conn, "/opt/fujin/a/releases/.staging-1", self.make_manifest_dict()
        )

        assert result.ok is True
        command = conn.runs[0]
        # single command (heredoc); the remote script reads manifest.json
        # from the staging dir itself (no oversized argv)
        assert "python3 - " in command
        assert "<<'_FUJIN_PY'" in command
        assert "/opt/fujin/a/releases/.staging-1" in command
        assert "base64" not in command
        script = command.split("\n", 1)[1]
        assert "manifest.json" in script
        assert "mode_mismatch" in script

    def test_mismatch_mapping(self, monkeypatch):
        out = (
            '__FUJIN_JSON__{"missing": ["gone.py"], '
            '"mismatch": {"app/main.py": "%s"}, '
            '"link_mismatch": {"bin/python": "python3.11"}, '
            '"mode_mismatch": {".env": ["0640", "0644"]}}' % ("b" * 64)
        )
        conn = RecordingConn(host=make_host())
        monkeypatch.setattr(
            conn, "run", lambda command, **kw: conn.runs.append(command) or (out, True)
        )
        md = self.make_manifest_dict(sha="a" * 64)
        md["entries"]["bin/python"] = {
            "path": "bin/python",
            "link": "python3.12",
            "mode": "000",
            "kind": "code",
        }

        result = verify_staging(conn, "/x/.staging-1", md)

        assert not result
        assert result.missing == ["gone.py"]
        assert result.mismatch["app/main.py"].expected == "a" * 64
        assert result.mismatch["app/main.py"].actual == "b" * 64
        assert result.link_mismatch == {"bin/python": "python3.11"}
        assert result.mode_mismatch[".env"].expected == "0640"
        assert result.mode_mismatch[".env"].actual == "0644"
        assert not result.ok

    def test_script_failure_raises(self, monkeypatch):
        conn = RecordingConn(host=make_host())
        monkeypatch.setattr(conn, "run", lambda command, **kw: ("", False))
        with pytest.raises(TransferError) as exc_info:
            verify_staging(conn, "/x/.staging-1", self.make_manifest_dict())
        assert "failed to run" in exc_info.value.message

    def test_unparseable_output_raises(self, monkeypatch):
        conn = RecordingConn(host=make_host())
        monkeypatch.setattr(conn, "run", lambda command, **kw: ("garbage", True))
        with pytest.raises(TransferError) as exc_info:
            verify_staging(conn, "/x/.staging-1", self.make_manifest_dict())
        assert "Could not parse" in exc_info.value.message

    def make_tree(self, root: Path, files: dict[str, bytes], modes=None):
        """Write *files* under *root* plus the ``manifest.json`` verify reads."""
        modes = modes or {}
        entries = {}
        for path, content in files.items():
            full = root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(content)
            mode = modes.get(path, 0o644)
            full.chmod(mode)
            entries[path] = {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "mode": format(mode, "04o"),
                "kind": "code",
            }
        (root / "manifest.json").write_text(json.dumps({"entries": entries}))
        return {"entries": entries}

    def test_server_built_corruption_fails_without_skip(self, tmp_path, monkeypatch):
        """The reuse path (no skips) still hashes venv content in full."""
        root = tmp_path / "staging"
        md = self.make_tree(root, {"venv/lib/pkg/mod.py": b"original"})
        (root / "venv/lib/pkg/mod.py").write_bytes(b"tampered")

        result = verify_staging(local_script_conn(monkeypatch), str(root), md)

        assert not result
        assert "venv/lib/pkg/mod.py" in result.mismatch
        assert result.hashed == 1
        assert result.skipped == 0

    def test_skip_prefix_does_not_rehash_server_built(self, tmp_path, monkeypatch):
        root = tmp_path / "staging"
        md = self.make_tree(
            root,
            {"venv/lib/pkg/mod.py": b"original", "app/main.py": b"code"},
        )
        # Corrupt only the server-built file: skipping its digest is the
        # whole point, the client-known entry is still hashed.
        (root / "venv/lib/pkg/mod.py").write_bytes(b"tampered")
        conn = local_script_conn(monkeypatch)

        result = verify_staging(conn, str(root), md, skip_prefixes=("venv/",))

        assert result.ok
        assert result.hashed == 1
        assert result.skipped == 1
        argv = shlex.split(conn.runs[0].split("\n", 1)[0].split("<<", 1)[0])
        assert argv[1:] == ["-", str(root), "venv/"]
        assert "skipped 1 server-built entry" in verify_report(str(root), result)

    def test_skipped_entry_must_still_exist(self, tmp_path, monkeypatch):
        root = tmp_path / "staging"
        md = self.make_tree(
            root,
            {"venv/lib/pkg/mod.py": b"content", "app/main.py": b"code"},
        )
        (root / "venv/lib/pkg/mod.py").unlink()

        result = verify_staging(
            local_script_conn(monkeypatch), str(root), md, skip_prefixes=("venv/",)
        )

        assert not result
        assert result.missing == ["venv/lib/pkg/mod.py"]
        assert result.hashed == 1

    def test_skipped_entry_mode_still_checked(self, tmp_path, monkeypatch):
        root = tmp_path / "staging"
        md = self.make_tree(
            root,
            {"venv/lib/pkg/mod.py": b"content", "app/main.py": b"code"},
            modes={"venv/lib/pkg/mod.py": 0o600},
        )
        (root / "venv/lib/pkg/mod.py").chmod(0o644)

        result = verify_staging(
            local_script_conn(monkeypatch), str(root), md, skip_prefixes=("venv/",)
        )

        assert not result
        assert result.mode_mismatch["venv/lib/pkg/mod.py"].expected == "0600"
        assert result.mode_mismatch["venv/lib/pkg/mod.py"].actual == "0644"
        assert result.skipped == 1
        assert result.hashed == 1

    def test_report_makes_skips_visible(self):
        result = transfer.VerifyResult(skipped=3, missing=[".env"])
        report = verify_report("/opt/fujin/a/releases/.staging-1", result)
        assert "skipped 3 server-built entries" in report
        assert "(hashed on the server during this deploy)" in report


class TestCommitRelease:
    def test_commit_command_string(self, monkeypatch):
        conn = RecordingConn(host=make_host())
        commit_release(conn, "/opt/fujin/myapp/releases/.staging-42", "aa11")
        command = conn.runs[0]
        assert "[ -e /opt/fujin/myapp/releases/aa11 ]" in command
        assert "exit 3" in command
        assert (
            "mv -T /opt/fujin/myapp/releases/.staging-42 "
            "/opt/fujin/myapp/releases/aa11" in command
        )
        assert "__FUJIN_JSON__" in command

    def test_existing_release_refused(self, monkeypatch):
        conn = RecordingConn(host=make_host())

        def run(self, command, **kw):
            conn.runs.append(command)
            if "/opt/fujin/myapp/releases/aa11 ]; then" in command:
                return "__FUJIN_JSON__EXISTS", False
            return "", True

        monkeypatch.setattr(RecordingConn, "run", run)
        with pytest.raises(TransferError) as exc_info:
            commit_release(conn, "/opt/fujin/myapp/releases/.staging-42", "aa11")
        assert "already exists" in exc_info.value.message

    def test_commit_relpaths_resolve_next_to_staging(self, monkeypatch):
        conn = RecordingConn(host=make_host())
        commit_release(conn, "/app/releases/.staging-7", "dd55")
        assert "[ -e /app/releases/dd55 ]" in conn.runs[0]
        assert "mv -T /app/releases/.staging-7 /app/releases/dd55" in conn.runs[0]


class TestSwapCurrent:
    def test_swap_command_string(self, monkeypatch):
        conn = RecordingConn(host=make_host())
        swap_current(conn, "/opt/fujin/myapp", "aa11")
        command = conn.runs[0]
        assert "rm -f /opt/fujin/myapp/current.next" in command
        assert "ln -s releases/aa11 /opt/fujin/myapp/current.next" in command
        assert "mv -T /opt/fujin/myapp/current.next /opt/fujin/myapp/current" in command

    def test_no_stray_current_next_on_failure(self, monkeypatch):
        conn = RecordingConn(host=make_host())

        def run(self, command, **kw):
            conn.runs.append(command)
            if "mv -T" in command:
                return "", False
            return "", True

        monkeypatch.setattr(RecordingConn, "run", run)
        with pytest.raises(TransferError) as exc_info:
            swap_current(conn, "/app", "aa11")
        assert "swap" in exc_info.value.message
        cleanup = [c for c in conn.runs if "rm -f /app/current.next" in c]
        assert len(cleanup) == 2  # pre + post-failure


class TestSweepStaleStaging:
    def test_finds_and_removes(self, monkeypatch):
        conn = RecordingConn(host=make_host())
        out = "/opt/fujin/myapp/releases/.staging-11\n/opt/fujin/myapp/releases/.staging-12"

        def run(self, command, **kw):
            conn.runs.append(command)
            if command.startswith("find "):
                return out, True
            return "", True

        monkeypatch.setattr(RecordingConn, "run", run)
        removed = sweep_stale_staging(conn, "/opt/fujin/myapp")

        assert removed == [
            "/opt/fujin/myapp/releases/.staging-11",
            "/opt/fujin/myapp/releases/.staging-12",
        ]
        # every stale dir is swept in a single remote command
        rm_cmds = [c for c in conn.runs if c.startswith("rm -rf")]
        assert len(rm_cmds) == 1
        assert ".staging-11" in rm_cmds[0]
        assert ".staging-12" in rm_cmds[0]
        assert len(conn.runs) == 2  # find + one rm

    def test_nothing_stale(self, monkeypatch):
        conn = RecordingConn(host=make_host())

        def run(self, command, **kw):
            conn.runs.append(command)
            if command.startswith("find "):
                return "", True
            return "", True

        monkeypatch.setattr(RecordingConn, "run", run)
        assert sweep_stale_staging(conn, "/app") == []
        assert len(conn.runs) == 1  # only the find
