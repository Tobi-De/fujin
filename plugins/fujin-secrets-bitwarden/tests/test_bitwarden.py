"""Tests for the Bitwarden adapter (no network, no real vault).

The ``bw`` process boundary is faked with a real executable placed on
``PATH``, so the adapter runs exactly the code it runs in production and the
tests assert on the CLI calls it makes and on rendered output.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import msgspec
import pytest

import fujin_secrets_bitwarden as adapter
from fujin.config import SecretConfig
from fujin.errors import ImproperlyConfiguredError, SecretResolutionError


# ---------------------------------------------------------------------------
# Fake `bw` CLI
# ---------------------------------------------------------------------------

# Runs as a real child process, so it can only see what a real ``bw`` sees.
# It asserts the observable half of the adapter's subprocess contract:
# ``capture_output=True`` (stdout/stderr are pipes). ``text=True`` is only
# observable in the parent, so the suite pins it with
# ``test_bw_output_is_text``: dropping it makes those calls return ``bytes``.
FAKE_BW_SCRIPT = '''"""Fake ``bw`` CLI used by the adapter tests."""
import json
import os
import stat
import sys


def contract_violation(message):
    sys.stderr.write("fake bw: " + message + "\\n")
    raise SystemExit(97)


if not (
    stat.S_ISFIFO(os.fstat(1).st_mode)
    and stat.S_ISFIFO(os.fstat(2).st_mode)
):
    contract_violation(
        "stdout/stderr must be pipes: subprocess.run(capture_output=True)"
    )

argv = sys.argv[1:]
with open(os.environ["FAKE_BW_STATE"], encoding="utf-8") as fp:
    state = json.load(fp)
with open(os.environ["FAKE_BW_LOG"], "a", encoding="utf-8") as fp:
    fp.write(json.dumps(argv) + "\\n")

if not argv:
    sys.stderr.write("fake bw: no command\\n")
    raise SystemExit(1)

if argv[0] == "unlock":
    if (
        len(argv) != 5
        or argv[1] != "--nointeraction"
        or argv[2] != "--passwordenv"
        or not argv[3]
        or argv[4] != "--raw"
    ):
        contract_violation(
            "unlock must be: unlock --nointeraction --passwordenv NAME --raw"
        )
    print("unlocked-session")
    raise SystemExit(0)

if "--session" not in argv or "--nointeraction" not in argv:
    contract_violation("every non-unlock call needs --session and --nointeraction")
session_index = argv.index("--session")
if session_index + 1 >= len(argv) or not argv[session_index + 1]:
    contract_violation("--session needs a value")

if argv[:2] == ["list", "folders"]:
    print(json.dumps(state["folders"]))
    raise SystemExit(0)

if argv[:2] == ["list", "items"]:
    folder_id = argv[argv.index("--folderid") + 1]
    name = next(
        (f["name"] for f in state["folders"] if f["id"] == folder_id), None
    )
    if name is None:
        sys.stderr.write("fake bw: unknown folder id\\n")
        raise SystemExit(1)
    print(json.dumps(state["items_by_folder"].get(name, [])))
    raise SystemExit(0)

if argv[:2] == ["get", "password"]:
    name = argv[2]
    for items in state["items_by_folder"].values():
        for item in items:
            if item.get("name") != name:
                continue
            login_dict = item.get("login")
            if not isinstance(login_dict, dict):
                continue
            password = login_dict.get("password")
            if isinstance(password, str):
                print(password)
                raise SystemExit(0)
    sys.stderr.write("fake bw: not found\\n")
    raise SystemExit(1)

sys.stderr.write("fake bw: unexpected command\\n")
raise SystemExit(1)
'''


def folder(name: str, folder_id: str) -> dict:
    return {"name": name, "id": folder_id}


def login(name: str, password: str) -> dict:
    return {"name": name, "type": 1, "login": {"password": password}}


def note(name: str) -> dict:
    return {"name": name, "type": 2, "notes": "not a login"}


class FakeBw:
    """Values served by the fake CLI; calls are read back from its log."""

    def __init__(
        self,
        folders: list[dict] | None = None,
        items_by_folder: dict[str, list[dict]] | None = None,
    ):
        self.folders = folders or []
        self.items_by_folder = items_by_folder or {}
        self.log_path: Path | None = None

    @property
    def calls(self) -> list[list[str]]:
        if self.log_path is None or not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def list_items_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["list", "items"]]

    def get_password_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["get", "password"]]


@pytest.fixture
def bw_session(monkeypatch):
    monkeypatch.setenv("BW_SESSION", "session-token")


@pytest.fixture
def install_bw(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bw_path = bin_dir / "bw"
    bw_path.write_text(f"#!{sys.executable}\n{FAKE_BW_SCRIPT}", encoding="utf-8")
    bw_path.chmod(0o755)

    state_path = tmp_path / "bw-state.json"
    log_path = tmp_path / "bw-calls.jsonl"
    monkeypatch.setenv("FAKE_BW_STATE", str(state_path))
    monkeypatch.setenv("FAKE_BW_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    def install(fake: FakeBw) -> FakeBw:
        state_path.write_text(
            json.dumps(
                {
                    "folders": fake.folders,
                    "items_by_folder": fake.items_by_folder,
                }
            ),
            encoding="utf-8",
        )
        fake.log_path = log_path
        return fake

    return install


def secret_config(**options) -> SecretConfig:
    return SecretConfig(adapter="bitwarden", options=options)


def error_message(exc: BaseException) -> str:
    return str(getattr(exc, "message", exc))


def folder_id(call: list[str]) -> str:
    return call[call.index("--folderid") + 1]


PROD = "fujin/app/prod"
SHARED = "fujin/shared"


@pytest.fixture
def vault() -> FakeBw:
    return FakeBw(
        folders=[folder(PROD, "id-prod"), folder(SHARED, "id-shared")],
        items_by_folder={
            PROD: [login("DB_PASSWORD", "db-secret")],
            SHARED: [login("SMTP_PASSWORD", "smtp-secret")],
        },
    )


# ---------------------------------------------------------------------------
# Folder path: batch resolution
# ---------------------------------------------------------------------------


def test_folder_path_lists_each_folder_once(bw_session, install_bw, vault):
    fake = install_bw(vault)

    out = adapter.bitwarden(
        "DB_PASSWORD=$DB_PASSWORD\nSMTP_PASSWORD=$SMTP_PASSWORD\nDEBUG=False",
        secret_config(folders=[PROD, SHARED]),
    )

    assert out == ("DB_PASSWORD=db-secret\nSMTP_PASSWORD=smtp-secret\nDEBUG=False")
    items_calls = fake.list_items_calls()
    assert [folder_id(call) for call in items_calls] == ["id-prod", "id-shared"]
    assert fake.get_password_calls() == []
    assert [c for c in fake.calls if c[:2] == ["list", "folders"]] == [
        [
            "list",
            "folders",
            "--raw",
            "--session",
            "session-token",
            "--nointeraction",
        ]
    ]
    assert [c for c in fake.calls if c[0] == "unlock"] == []


def test_list_items_argv_is_explicit(bw_session, install_bw, vault):
    fake = install_bw(vault)

    adapter.bitwarden("DB_PASSWORD=$DB_PASSWORD", secret_config(folders=[PROD]))

    assert fake.list_items_calls() == [
        [
            "list",
            "items",
            "--folderid",
            "id-prod",
            "--raw",
            "--session",
            "session-token",
            "--nointeraction",
        ]
    ]


def test_first_configured_folder_wins(bw_session, install_bw):
    fake = install_bw(
        FakeBw(
            folders=[folder(PROD, "id-prod"), folder(SHARED, "id-shared")],
            items_by_folder={
                PROD: [login("TOKEN", "prod-token")],
                SHARED: [login("TOKEN", "shared-token")],
            },
        )
    )

    out = adapter.bitwarden("TOKEN=$TOKEN", secret_config(folders=[PROD, SHARED]))

    assert out == "TOKEN=prod-token"
    assert fake.get_password_calls() == []


def test_lookup_order_is_configuration_order(bw_session, install_bw):
    fake = install_bw(
        FakeBw(
            folders=[folder(PROD, "id-prod"), folder(SHARED, "id-shared")],
            items_by_folder={
                PROD: [login("TOKEN", "prod-token")],
                SHARED: [login("TOKEN", "shared-token")],
            },
        )
    )

    out = adapter.bitwarden("TOKEN=$TOKEN", secret_config(folders=[SHARED, PROD]))

    assert out == "TOKEN=shared-token"
    assert [folder_id(c) for c in fake.list_items_calls()] == ["id-shared", "id-prod"]


def test_folder_prefixed_refs_disambiguate_duplicate_names(bw_session, install_bw):
    install_bw(
        FakeBw(
            folders=[folder(PROD, "id-prod"), folder(SHARED, "id-shared")],
            items_by_folder={
                PROD: [login("postgres", "prod-pg")],
                SHARED: [login("postgres", "shared-pg")],
            },
        )
    )

    out = adapter.bitwarden(
        "PROD=$fujin/app/prod/postgres\nSHARED=$fujin/shared/postgres\nFIRST=$postgres",
        secret_config(folders=[PROD, SHARED]),
    )

    assert out == "PROD=prod-pg\nSHARED=shared-pg\nFIRST=prod-pg"


def test_only_login_passwords_resolve(bw_session, install_bw):
    install_bw(
        FakeBw(
            folders=[folder(PROD, "id-prod")],
            items_by_folder={PROD: [note("NOTE_ONLY"), login("REAL", "real-value")]},
        )
    )

    assert (
        adapter.bitwarden("REAL=$REAL", secret_config(folders=[PROD]))
        == "REAL=real-value"
    )

    with pytest.raises(SecretResolutionError) as excinfo:
        adapter.bitwarden("X=$NOTE_ONLY", secret_config(folders=[PROD]))

    assert "NOTE_ONLY" in error_message(excinfo.value)
    assert "non-login" not in error_message(excinfo.value)


def test_folders_option_accepts_a_single_string(bw_session, install_bw, vault):
    install_bw(vault)

    out = adapter.bitwarden(
        "SMTP_PASSWORD=$SMTP_PASSWORD", secret_config(folders=SHARED)
    )

    assert out == "SMTP_PASSWORD=smtp-secret"


def test_duplicate_folder_names_fail_closed(bw_session, install_bw):
    install_bw(
        FakeBw(
            folders=[folder(PROD, "id-prod"), folder(PROD, "id-prod-2")],
            items_by_folder={PROD: [login("DB_PASSWORD", "db-secret")]},
        )
    )

    with pytest.raises(SecretResolutionError) as excinfo:
        adapter.bitwarden("DB_PASSWORD=$DB_PASSWORD", secret_config(folders=[PROD]))

    message = error_message(excinfo.value)
    assert PROD in message
    assert "duplicated" in message
    assert "id-prod" not in message
    assert "db-secret" not in message


# ---------------------------------------------------------------------------
# Missing refs: fail closed, value-free
# ---------------------------------------------------------------------------


def test_missing_ref_names_key_and_folders(bw_session, install_bw):
    sentinel = "known-value-must-not-leak"
    install_bw(
        FakeBw(
            folders=[folder(PROD, "id-prod"), folder(SHARED, "id-shared")],
            items_by_folder={
                PROD: [login("KNOWN", sentinel)],
                SHARED: [],
            },
        )
    )

    with pytest.raises(SecretResolutionError) as excinfo:
        adapter.bitwarden(
            "KNOWN=$KNOWN\nTOKEN=$MISSING",
            secret_config(folders=[PROD, SHARED]),
        )

    message = error_message(excinfo.value)
    assert "TOKEN" in message
    assert "MISSING" in message
    assert f"'{PROD}'" in message
    assert f"'{SHARED}'" in message
    assert sentinel not in message
    assert "TOKEN=$MISSING" not in message
    assert excinfo.value.key == "TOKEN"


def test_missing_folder_prefixed_ref_names_that_folder(bw_session, install_bw):
    install_bw(
        FakeBw(
            folders=[folder(PROD, "id-prod")],
            items_by_folder={PROD: []},
        )
    )

    with pytest.raises(SecretResolutionError) as excinfo:
        adapter.bitwarden("TOKEN=$fujin/shared/nope", secret_config(folders=[PROD]))

    message = error_message(excinfo.value)
    assert "TOKEN" in message
    assert f"'{SHARED}'" in message


def test_unknown_configured_folder_fails_before_lookup(bw_session, install_bw):
    install_bw(FakeBw(folders=[], items_by_folder={}))

    with pytest.raises(SecretResolutionError) as excinfo:
        adapter.bitwarden("TOKEN=$TOKEN", secret_config(folders=[SHARED]))

    assert SHARED in error_message(excinfo.value)


def test_empty_folders_option_is_rejected_at_config_load():
    with pytest.raises(ImproperlyConfiguredError) as excinfo:
        secret_config(folders=[])

    assert "folders" in error_message(excinfo.value)
    assert "empty" in error_message(excinfo.value)


# ---------------------------------------------------------------------------
# No folders: legacy global path is unchanged
# ---------------------------------------------------------------------------


def test_without_folders_uses_one_get_password_per_ref(bw_session, install_bw):
    fake = install_bw(
        FakeBw(items_by_folder={PROD: [login("A", "va"), login("B", "vb")]})
    )

    out = adapter.bitwarden("A=$A\nB=$B", secret_config())

    assert out == "A=va\nB=vb"
    assert sorted(call[2] for call in fake.get_password_calls()) == ["A", "B"]
    assert fake.list_items_calls() == []
    assert [c for c in fake.calls if c[0] == "list"] == []
    assert [
        "get",
        "password",
        "A",
        "--raw",
        "--session",
        "session-token",
        "--nointeraction",
    ] in [call for call in fake.get_password_calls() if call[2] == "A"]


def test_without_folders_missing_ref_still_fails_closed(bw_session, install_bw):
    install_bw(FakeBw(items_by_folder={}))

    with pytest.raises(SecretResolutionError):
        adapter.bitwarden("A=$NOPE", secret_config())


# ---------------------------------------------------------------------------
# Escaping and authentication
# ---------------------------------------------------------------------------


def test_escaped_dollars_are_never_looked_up(bw_session, install_bw):
    fake = install_bw(FakeBw(folders=[folder(PROD, "id-prod")]))

    out = adapter.bitwarden(
        "PASSWORD=pa$$word\nDOLLAR=$$", secret_config(folders=[PROD])
    )

    assert out == 'PASSWORD="pa$word"\nDOLLAR="$"'
    assert fake.calls == []


def test_password_env_unlocks_without_listing_when_no_refs(monkeypatch, install_bw):
    monkeypatch.delenv("BW_SESSION", raising=False)
    fake = install_bw(FakeBw(folders=[folder(PROD, "id-prod")]))

    out = adapter.bitwarden(
        "LITERAL=plain", secret_config(folders=[PROD], password_env="BW_PASSWORD")
    )

    assert out == "LITERAL=plain"
    assert fake.calls == [
        ["unlock", "--nointeraction", "--passwordenv", "BW_PASSWORD", "--raw"]
    ]


def test_password_env_unlock_then_folder_resolution(monkeypatch, install_bw, vault):
    monkeypatch.delenv("BW_SESSION", raising=False)
    fake = install_bw(vault)

    out = adapter.bitwarden(
        "DB_PASSWORD=$DB_PASSWORD",
        secret_config(folders=[PROD], password_env="BW_PASSWORD"),
    )

    assert out == "DB_PASSWORD=db-secret"
    assert fake.calls[0] == [
        "unlock",
        "--nointeraction",
        "--passwordenv",
        "BW_PASSWORD",
        "--raw",
    ]
    assert fake.calls[1] == [
        "list",
        "folders",
        "--raw",
        "--session",
        "unlocked-session",
        "--nointeraction",
    ]


def test_bw_output_is_text(bw_session, install_bw, vault):
    """``text=True`` is parent-side only; pin it through the return types."""
    install_bw(vault)

    assert isinstance(
        adapter._run_bw(["list", "folders", "--raw"], "session-token", "list folders"),
        str,
    )
    assert isinstance(adapter._read_secret("DB_PASSWORD", "session-token"), str)
    assert isinstance(adapter._signin("BW_PASSWORD"), str)


# ---------------------------------------------------------------------------
# Configuration typing
# ---------------------------------------------------------------------------


def test_secret_config_decodes_string_and_list_options():
    decoded = msgspec.convert(
        {
            "adapter": "bitwarden",
            "options": {
                "password_env": "BW_PASSWORD",
                "folders": [PROD, SHARED],
            },
        },
        type=SecretConfig,
    )

    assert decoded.options["password_env"] == "BW_PASSWORD"
    assert decoded.options["folders"] == [PROD, SHARED]


def test_secret_config_accepts_a_single_string_folder():
    decoded = msgspec.convert(
        {"adapter": "bitwarden", "options": {"folders": PROD}},
        type=SecretConfig,
    )

    assert decoded.options["folders"] == PROD


def test_secret_config_rejects_non_string_option_values():
    with pytest.raises(ImproperlyConfiguredError) as excinfo:
        SecretConfig(adapter="bitwarden", options={"timeout": 30})

    message = error_message(excinfo.value)
    assert "timeout" in message
    assert "string" in message


def test_secret_config_rejects_non_string_list_entries():
    with pytest.raises(ImproperlyConfiguredError) as excinfo:
        SecretConfig(adapter="bitwarden", options={"folders": [PROD, 1]})

    message = error_message(excinfo.value)
    assert "folders" in message
    assert "strings" in message
