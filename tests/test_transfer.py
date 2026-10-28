from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from fujin import transfer
from fujin.exit_codes import EXIT_HOST_UNREACHABLE
from fujin.transfer import (
    HostUnreachableError,
    ItemizedChange,
    RsyncUnavailableError,
    TransferError,
    commit_release,
    parse_itemize,
    rsync_to_staging,
    sweep_stale_staging,
    swap_current,
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
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p 22",
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
            " -o ConnectTimeout=10 -p 22 -i "
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
        with pytest.raises(RsyncUnavailableError):
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
        with pytest.raises(RsyncUnavailableError) as exc_info:
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
        rm_cmds = [c for c in conn.runs if c.startswith("rm -rf")]
        assert len(rm_cmds) == 2
        assert any(".staging-11" in c for c in rm_cmds)

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
