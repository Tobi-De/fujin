"""End-to-end tests for the Bitwarden protocol adapter.

A real ``http.server`` instance on 127.0.0.1 plays the Bitwarden server:
prelogin, token and sync are implemented, and the test encrypts the vault with
keys derived independently from the master password. Nothing leaves the
machine and no CLI is involved.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.metadata
import json
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError
from fujin_secrets_bitwarden.protocol import bitwarden_protocol

EMAIL = "User@Example.com "
NORMALIZED_EMAIL = "user@example.com"
PASSWORD = "correct horse battery staple"
PASSWORD_ENV = "BW_MASTER_PASSWORD"

PROD = "fujin/app/prod"
SHARED = "fujin/shared"

IV = bytes(range(16))


# ---------------------------------------------------------------------------
# Independent crypto helpers (deliberately not imported from the adapter)
# ---------------------------------------------------------------------------


def pbkdf2(secret: bytes, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret, salt, iterations, dklen=32)


def hkdf(prk: bytes, info: bytes, length: int) -> bytes:
    output, block, counter = b"", b"", 1
    while len(output) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        output += block
        counter += 1
    return output[:length]


def encrypt(
    plaintext: bytes,
    enc_key: bytes,
    mac_key: bytes,
    *,
    enc_type: int = 2,
    iv: bytes = IV,
) -> str:
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    parts = [base64.b64encode(iv).decode(), base64.b64encode(ciphertext).decode()]
    if enc_type == 2:
        mac = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
        parts.append(base64.b64encode(mac).decode())
    return f"{enc_type}." + "|".join(parts)


# ---------------------------------------------------------------------------
# Fake Bitwarden server
# ---------------------------------------------------------------------------


def folder(name: str, folder_id: str) -> dict:
    return {"name": name, "id": folder_id}


def login(
    name: str,
    password: str | None,
    folder_id: str | None = None,
    enc_type: int = 2,
) -> dict:
    return {
        "type": 1,
        "name": name,
        "password": password,
        "folder_id": folder_id,
        "enc_type": enc_type,
    }


def note(name: str, folder_id: str | None = None) -> dict:
    return {"type": 2, "name": name, "password": None, "folder_id": folder_id}


class FakeVault:
    """Plaintext vault contents plus recorded requests."""

    def __init__(self):
        self.email = NORMALIZED_EMAIL
        self.password = PASSWORD
        self.kdf = {"Kdf": 0, "KdfIterations": 1000}
        self.user_key = bytes(range(64))
        self.folders: list[dict] = []
        self.ciphers: list[dict] = []
        self.requests: list[dict] = []
        self.prelogin_email: str | None = None
        self.prelogin_content_type: str | None = None
        self.token_form: dict[str, list[str]] | None = None
        self.token_content_type: str | None = None
        self.token_error: tuple[int, bytes] | None = None
        self.omit_key = False
        self.sync_status = 200
        self.sync_authorization: str | None = None
        self.access_token = "test-access-token"
        self.server_url = ""

    # -- crypto ---------------------------------------------------------

    def master_key(self) -> bytes:
        if self.kdf["Kdf"] == 0:
            return pbkdf2(
                self.password.encode(), self.email.encode(), self.kdf["KdfIterations"]
            )
        return hash_secret_raw(
            secret=self.password.encode(),
            salt=self.email.encode(),
            time_cost=self.kdf["KdfIterations"],
            memory_cost=self.kdf["KdfMemory"],
            parallelism=self.kdf["KdfParallelism"],
            hash_len=32,
            type=Argon2Type.ID,
            version=19,
        )

    def user_keys(self) -> tuple[bytes, bytes]:
        """Stretched master keys that wrap the account key."""
        master = self.master_key()
        return hkdf(master, b"enc", 32), hkdf(master, b"mac", 32)

    def account_keys(self) -> tuple[bytes, bytes]:
        """The decrypted account key halves, used for vault contents."""
        return self.user_key[:32], self.user_key[32:]

    def wrapped_user_key(self) -> str:
        enc_key, mac_key = self.user_keys()
        return encrypt(self.user_key, enc_key, mac_key)

    def sync_payload(self) -> dict:
        enc_key, mac_key = self.account_keys()
        folders = [
            {
                "id": entry["id"],
                "name": encrypt(entry["name"].encode(), enc_key, mac_key),
            }
            for entry in self.folders
        ]
        ciphers = []
        for index, cipher in enumerate(self.ciphers):
            # ``foreign_key`` simulates an organization item encrypted with a
            # key the adapter does not have.
            if cipher.get("foreign_key"):
                item_enc_key, item_mac_key = b"\x11" * 32, b"\x22" * 32
            else:
                item_enc_key, item_mac_key = enc_key, mac_key
            entry = {
                "id": f"cipher-{index}",
                "folderId": cipher["folder_id"],
                "organizationId": cipher.get("organization_id"),
                "type": cipher["type"],
                "name": encrypt(cipher["name"].encode(), item_enc_key, item_mac_key),
            }
            if cipher["type"] == 1:
                password = cipher["password"]
                entry["login"] = {
                    "username": None,
                    "password": (
                        None
                        if password is None
                        else encrypt(
                            password.encode(),
                            item_enc_key,
                            item_mac_key,
                            enc_type=cipher["enc_type"],
                        )
                    ),
                }
            ciphers.append(entry)
        return {"folders": folders, "ciphers": ciphers}

    # -- helpers --------------------------------------------------------

    @property
    def paths(self) -> list[str]:
        return [request["path"] for request in self.requests]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    @property
    def vault(self) -> FakeVault:
        return self.server.vault

    def _respond(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record(self, body: bytes) -> None:
        self.vault.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )

    def do_GET(self) -> None:
        self._record(b"")
        if self.path != "/api/sync":
            self._respond(404, b"{}")
            return
        vault = self.vault
        vault.sync_authorization = self.headers.get("Authorization")
        if vault.sync_status != 200:
            self._respond(vault.sync_status, b'{"error":"SECRET-VALUE-FROM-SERVER"}')
            return
        if vault.sync_authorization != f"Bearer {vault.access_token}":
            self._respond(401, b"{}")
            return
        self._respond(200, json.dumps(vault.sync_payload()).encode())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._record(body)

        if self.path == "/identity/accounts/prelogin":
            try:
                data = json.loads(body)
            except ValueError:
                data = {}
            self.vault.prelogin_email = data.get("email")
            self.vault.prelogin_content_type = self.headers.get("Content-Type")
            self._respond(200, json.dumps(self.vault.kdf).encode())
            return

        if self.path == "/identity/connect/token":
            vault = self.vault
            vault.token_content_type = self.headers.get("Content-Type")
            if vault.token_error is not None:
                status, payload = vault.token_error
                self._respond(status, payload)
                return
            vault.token_form = urllib.parse.parse_qs(body.decode("utf-8"))
            payload = {"access_token": vault.access_token}
            if not vault.omit_key:
                payload["Key"] = vault.wrapped_user_key()
            self._respond(200, json.dumps(payload).encode())
            return

        self._respond(404, b"{}")


@pytest.fixture
def vault(monkeypatch):
    fake = FakeVault()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.vault = fake
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    fake.server_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    try:
        yield fake
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def secret_config(vault: FakeVault, **overrides) -> SecretConfig:
    options: dict = {
        "email": EMAIL,
        "password_env": PASSWORD_ENV,
        "server_url": vault.server_url,
    }
    options.update(overrides)
    return SecretConfig(
        adapter="bitwarden-protocol",
        options={key: value for key, value in options.items() if value is not None},
    )


def error_message(exc: BaseException) -> str:
    return str(getattr(exc, "message", exc))


def standard_vault(vault: FakeVault) -> FakeVault:
    vault.folders = [folder(PROD, "id-prod"), folder(SHARED, "id-shared")]
    vault.ciphers = [
        login("DB_PASSWORD", "db-secret", folder_id="id-prod"),
        login("TOKEN", "prod-token", folder_id="id-prod"),
        login("TOKEN", "shared-token", folder_id="id-shared"),
        login("SMTP_PASSWORD", "smtp-secret", folder_id="id-shared"),
    ]
    return vault


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_configured_folders_resolve_in_order(vault):
    standard_vault(vault)

    out = bitwarden_protocol(
        "DB_PASSWORD=$DB_PASSWORD\nSMTP_PASSWORD=$SMTP_PASSWORD\nDEBUG=False",
        secret_config(vault, folders=[PROD, SHARED]),
    )

    assert out == "DB_PASSWORD=db-secret\nSMTP_PASSWORD=smtp-secret\nDEBUG=False"
    assert vault.paths == [
        "/identity/accounts/prelogin",
        "/identity/connect/token",
        "/api/sync",
    ]


def test_first_configured_folder_wins(vault):
    standard_vault(vault)

    out = bitwarden_protocol(
        "TOKEN=$TOKEN", secret_config(vault, folders=[PROD, SHARED])
    )

    assert out == "TOKEN=prod-token"


def test_lookup_order_is_configuration_order(vault):
    standard_vault(vault)

    out = bitwarden_protocol(
        "TOKEN=$TOKEN", secret_config(vault, folders=[SHARED, PROD])
    )

    assert out == "TOKEN=shared-token"


def test_folders_accepts_a_single_string(vault):
    standard_vault(vault)

    out = bitwarden_protocol(
        "SMTP_PASSWORD=$SMTP_PASSWORD", secret_config(vault, folders=SHARED)
    )

    assert out == "SMTP_PASSWORD=smtp-secret"


def test_folder_prefixed_refs_disambiguate_duplicate_names(vault):
    standard_vault(vault)

    out = bitwarden_protocol(
        "PROD=$fujin/app/prod/TOKEN\nSHARED=$fujin/shared/TOKEN\nFIRST=$TOKEN",
        secret_config(vault, folders=[PROD, SHARED]),
    )

    assert out == "PROD=prod-token\nSHARED=shared-token\nFIRST=prod-token"


def test_fujin_prefix_targets_the_fujin_folder(vault):
    standard_vault(vault)
    vault.folders.append(folder("fujin", "id-fujin"))
    vault.ciphers.append(login("SINGLE", "single-secret", folder_id="id-fujin"))

    out = bitwarden_protocol(
        "ONE=$fujin/SINGLE",
        secret_config(vault, folders=[PROD, SHARED]),
    )

    assert out == "ONE=single-secret"


def test_global_lookup_without_folders(vault):
    standard_vault(vault)

    out = bitwarden_protocol(
        "DB_PASSWORD=$DB_PASSWORD\nSMTP_PASSWORD=$SMTP_PASSWORD",
        secret_config(vault),
    )

    assert out == "DB_PASSWORD=db-secret\nSMTP_PASSWORD=smtp-secret"


def test_global_lookup_duplicate_name_fails_closed(vault):
    standard_vault(vault)

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol("TOKEN=$TOKEN", secret_config(vault))

    message = error_message(excinfo.value)
    assert "TOKEN" in message
    assert "multiple" in message
    assert excinfo.value.adapter == "bitwarden-protocol"
    assert excinfo.value.key == "TOKEN"
    for value in ("prod-token", "shared-token"):
        assert value not in message


def test_global_lookup_honours_folder_prefixed_refs(vault):
    standard_vault(vault)
    vault.folders.append(folder("fujin", "id-fujin"))
    vault.ciphers.append(login("SINGLE", "single-secret", folder_id="id-fujin"))

    out = bitwarden_protocol(
        "SINGLE=$fujin/SINGLE\nSHARED=$fujin/shared/TOKEN", secret_config(vault)
    )

    assert out == "SINGLE=single-secret\nSHARED=shared-token"


def test_non_login_cipher_is_skipped(vault):
    vault.folders = [folder(SHARED, "id-shared")]
    vault.ciphers = [
        note("NOTE_ONLY", folder_id="id-shared"),
        login("NO_PASSWORD", None, folder_id="id-shared"),
        login("REAL", "real-value", folder_id="id-shared"),
    ]

    out = bitwarden_protocol("REAL=$REAL", secret_config(vault, folders=[SHARED]))
    assert out == "REAL=real-value"

    for ref in ("$NOTE_ONLY", "$NO_PASSWORD"):
        with pytest.raises(SecretResolutionError) as excinfo:
            bitwarden_protocol(f"X={ref}", secret_config(vault, folders=[SHARED]))
        assert "real-value" not in error_message(excinfo.value)


def test_organization_items_are_skipped(vault):
    vault.folders = [folder(SHARED, "id-shared")]
    org_item = login("ORG_ONLY", "org-secret", folder_id="id-shared")
    org_item.update(organization_id="org-1", foreign_key=True)
    vault.ciphers = [
        org_item,
        login("REAL", "real-value", folder_id="id-shared"),
    ]

    out = bitwarden_protocol("REAL=$REAL", secret_config(vault, folders=[SHARED]))
    assert out == "REAL=real-value"

    with pytest.raises(SecretResolutionError):
        bitwarden_protocol("ORG=$ORG_ONLY", secret_config(vault, folders=[SHARED]))


def test_type_0_cipher_password_decrypts(vault):
    vault.folders = [folder(SHARED, "id-shared")]
    vault.ciphers = [
        login("LEGACY", "legacy-secret", folder_id="id-shared", enc_type=0)
    ]

    out = bitwarden_protocol("LEGACY=$LEGACY", secret_config(vault, folders=[SHARED]))

    assert out == "LEGACY=legacy-secret"


def test_literals_and_escaped_dollars_are_untouched(vault):
    vault.folders = [folder(SHARED, "id-shared")]
    vault.ciphers = [login("REAL", "real-value", folder_id="id-shared")]

    out = bitwarden_protocol(
        "DEBUG=False\nPASSWORD=pa$$word\nREAL=$REAL",
        secret_config(vault, folders=[SHARED]),
    )

    assert out == 'DEBUG=False\nPASSWORD="pa$word"\nREAL=real-value'


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_missing_ref_names_key_ref_and_folders(vault):
    standard_vault(vault)

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol(
            "MISSING=$NOPE", secret_config(vault, folders=[PROD, SHARED])
        )

    message = error_message(excinfo.value)
    assert "MISSING" in message
    assert "NOPE" in message
    assert PROD in message
    assert SHARED in message
    assert excinfo.value.adapter == "bitwarden-protocol"
    assert excinfo.value.key == "MISSING"


def test_missing_folder_prefixed_ref_names_that_folder(vault):
    standard_vault(vault)

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol(
            "MISSING=$fujin/nope/VALUE", secret_config(vault, folders=[PROD])
        )

    message = error_message(excinfo.value)
    assert "fujin/nope" in message
    assert PROD not in message


def test_unknown_configured_folder_fails_before_resolution(vault):
    standard_vault(vault)

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol(
            "TOKEN=$TOKEN", secret_config(vault, folders=[PROD, "no/such/folder"])
        )

    assert "no/such/folder" in error_message(excinfo.value)


def test_duplicate_folder_names_fail_closed(vault):
    vault.folders = [
        folder(SHARED, "id-a"),
        folder(SHARED, "id-b"),
    ]
    vault.ciphers = [login("TOKEN", "shared-token", folder_id="id-a")]

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol("TOKEN=$TOKEN", secret_config(vault, folders=[SHARED]))

    message = error_message(excinfo.value)
    assert SHARED in message
    assert "duplicated" in message


def test_unknown_kdf_fails_closed(vault):
    vault.kdf = {"Kdf": 7, "KdfIterations": 1}

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol("X=$X", secret_config(vault))

    assert "unsupported KDF" in error_message(excinfo.value)
    assert "/identity/connect/token" not in vault.paths


def test_invalid_kdf_iterations_fail_closed(vault):
    vault.kdf = {"Kdf": 0, "KdfIterations": 0}

    with pytest.raises(SecretResolutionError):
        bitwarden_protocol("X=$X", secret_config(vault))


def test_two_factor_login_error_is_value_free(vault):
    vault.token_error = (
        400,
        b'{"TwoFactorProviders":[0,1],"error":"two factor required"}',
    )

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol("X=$X", secret_config(vault))

    message = error_message(excinfo.value)
    assert "two-factor" in message
    assert "TwoFactorProviders" not in message
    assert "HTTP" not in message


def test_login_http_error_reports_status_only(vault):
    vault.token_error = (
        401,
        b'{"error":"invalid_grant","error_description":"bad secret VALUE"}',
    )

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol("X=$X", secret_config(vault))

    message = error_message(excinfo.value)
    assert "HTTP 401" in message
    assert "invalid_grant" not in message
    assert "bad secret VALUE" not in message


def test_sync_http_error_is_value_free(vault):
    vault.sync_status = 503

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol("X=$X", secret_config(vault))

    message = error_message(excinfo.value)
    assert "HTTP 503" in message
    assert "SECRET-VALUE-FROM-SERVER" not in message


def test_missing_account_key_fails_closed(vault):
    vault.omit_key = True

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol("X=$X", secret_config(vault))

    assert "account key" in error_message(excinfo.value)


def test_missing_password_environment_variable(vault, monkeypatch):
    monkeypatch.delenv(PASSWORD_ENV, raising=False)

    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol("X=$X", secret_config(vault))

    assert PASSWORD_ENV in error_message(excinfo.value)
    assert vault.requests == []


@pytest.mark.parametrize("option", ["email", "password_env"])
def test_missing_required_options(vault, option):
    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol("X=$X", secret_config(vault, **{option: None}))

    assert f"options.{option}" in error_message(excinfo.value)
    assert vault.requests == []


def test_invalid_server_url_fails_closed(vault):
    with pytest.raises(SecretResolutionError) as excinfo:
        bitwarden_protocol("X=$X", secret_config(vault, server_url="not a url"))

    assert "server_url" in error_message(excinfo.value)


# ---------------------------------------------------------------------------
# Token request shape
# ---------------------------------------------------------------------------


def test_login_request_matches_the_bitwarden_protocol(vault):
    standard_vault(vault)

    bitwarden_protocol("TOKEN=$TOKEN", secret_config(vault, folders=[PROD]))

    assert vault.prelogin_email == NORMALIZED_EMAIL
    assert vault.prelogin_content_type == "application/json"
    assert vault.token_content_type == "application/x-www-form-urlencoded"

    form = vault.token_form
    assert form["grant_type"] == ["password"]
    assert form["username"] == [NORMALIZED_EMAIL]
    assert form["scope"] == ["api offline_access"]
    assert form["client_id"] == ["cli"]
    assert form["deviceType"] == ["10"]
    assert form["deviceName"] == ["fujin"]
    uuid.UUID(form["deviceIdentifier"][0])

    master_key = pbkdf2(
        PASSWORD.encode(), NORMALIZED_EMAIL.encode(), vault.kdf["KdfIterations"]
    )
    expected = base64.b64encode(
        hashlib.pbkdf2_hmac("sha256", master_key, PASSWORD.encode(), 1, dklen=32)
    ).decode()
    assert form["password"] == [expected]

    assert vault.sync_authorization == f"Bearer {vault.access_token}"


def test_server_url_trailing_slash_is_stripped(vault):
    standard_vault(vault)

    out = bitwarden_protocol(
        "TOKEN=$TOKEN",
        secret_config(vault, folders=[PROD], server_url=vault.server_url + "/"),
    )

    assert out == "TOKEN=prod-token"


# ---------------------------------------------------------------------------
# No vault access without refs
# ---------------------------------------------------------------------------


def test_no_refs_performs_no_http_requests(vault):
    out = bitwarden_protocol("DEBUG=False\nPASSWORD=pa$$word", secret_config(vault))

    assert out == 'DEBUG=False\nPASSWORD="pa$word"'
    assert vault.requests == []


def test_no_refs_needs_no_password_or_server(monkeypatch):
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    config = SecretConfig(
        adapter="bitwarden-protocol",
        options={
            "email": EMAIL,
            "password_env": PASSWORD_ENV,
            "server_url": "http://127.0.0.1:1",
        },
    )

    assert bitwarden_protocol("A=1\nB=two", config) == "A=1\nB=two"


# ---------------------------------------------------------------------------
# Argon2 accounts
# ---------------------------------------------------------------------------


def test_argon2id_accounts_are_supported(vault):
    vault.kdf = {
        "Kdf": 1,
        "KdfIterations": 2,
        "KdfMemory": 1024,
        "KdfParallelism": 1,
    }
    standard_vault(vault)

    out = bitwarden_protocol(
        "DB_PASSWORD=$DB_PASSWORD", secret_config(vault, folders=[PROD])
    )

    assert out == "DB_PASSWORD=db-secret"


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def test_entry_point_is_declared():
    entries = {
        entry.name: entry.value
        for entry in importlib.metadata.entry_points(group="fujin.secrets")
    }
    assert entries["bitwarden"] == "fujin_secrets_bitwarden:bitwarden"
    assert (
        entries["bitwarden-protocol"]
        == "fujin_secrets_bitwarden.protocol:bitwarden_protocol"
    )
