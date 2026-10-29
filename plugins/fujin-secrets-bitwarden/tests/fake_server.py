"""Shared offline Bitwarden server used by the two-factor tests.

A real ``http.server`` on 127.0.0.1 answers prelogin, the identity token grant
and sync. The vault crypto is derived here independently of the adapter, so a
decryption bug cannot cancel itself out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

EMAIL = "User@Example.com "
NORMALIZED_EMAIL = "user@example.com"
PASSWORD = "correct horse battery staple"
PASSWORD_ENV = "BW_MASTER_PASSWORD"

PROD = "fujin/app/prod"
SHARED = "fujin/shared"

REMEMBER_TOKEN = "REMEMBER-TOKEN-VALUE"
ROTATED_TOKEN = "ROTATED-TOKEN-VALUE"

IV = bytes(range(16))

TWO_FACTOR_BODY = b'{"TwoFactorProviders":[0,1],"error":"two factor required"}'


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


def folder(name: str, folder_id: str) -> dict:
    return {"name": name, "id": folder_id}


def login_item(name: str, password: str, folder_id: str | None = None) -> dict:
    return {"type": 1, "name": name, "password": password, "folder_id": folder_id}


def standard_vault(vault: FakeVault) -> FakeVault:
    vault.folders = [folder(PROD, "id-prod"), folder(SHARED, "id-shared")]
    vault.ciphers = [
        login_item("DB_PASSWORD", "db-secret", folder_id="id-prod"),
        login_item("TOKEN", "prod-token", folder_id="id-prod"),
        login_item("TOKEN", "shared-token", folder_id="id-shared"),
    ]
    return vault


class FakeVault:
    """Account/vault state plus everything the handler needs."""

    def __init__(self):
        self.email = NORMALIZED_EMAIL
        self.password = PASSWORD
        self.kdf = {"Kdf": 0, "KdfIterations": 1000}
        self.user_key = bytes(range(64))
        self.folders: list[dict] = []
        self.ciphers: list[dict] = []
        self.requests: list[dict] = []

        # Two-factor behaviour.
        self.requires_two_factor = True
        self.two_factor_code = "123456"
        self.remember_token = REMEMBER_TOKEN
        self.rotate_remember_token = False
        self.new_remember_token = ROTATED_TOKEN
        self.omit_remember_token = False

        self.token_forms: list[dict[str, list[str]]] = []
        self.prelogin_email: str | None = None
        self.access_token = "test-access-token"
        self.server_url = ""

    @property
    def paths(self) -> list[str]:
        return [request["path"] for request in self.requests]

    def has_token_request(self) -> bool:
        return "/identity/connect/token" in self.paths

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
        master = self.master_key()
        return hkdf(master, b"enc", 32), hkdf(master, b"mac", 32)

    def account_keys(self) -> tuple[bytes, bytes]:
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
            ciphers.append(
                {
                    "id": f"cipher-{index}",
                    "folderId": cipher["folder_id"],
                    "organizationId": None,
                    "type": cipher["type"],
                    "name": encrypt(cipher["name"].encode(), enc_key, mac_key),
                    "login": {
                        "username": None,
                        "password": encrypt(
                            cipher["password"].encode(), enc_key, mac_key
                        ),
                    },
                }
            )
        return {"folders": folders, "ciphers": ciphers}

    # -- token grant ----------------------------------------------------

    def token_response(
        self, form: dict[str, list[str]]
    ) -> tuple[int, bytes, bool, bool]:
        """Return ``(status, body, ok, issued_remember_token)`` for a grant."""
        token = form.get("twoFactorToken", [""])[0]
        provider = form.get("twoFactorProvider", [""])[0]
        remember = form.get("twoFactorRemember", ["0"])[0]

        if self.requires_two_factor and not token:
            return 400, TWO_FACTOR_BODY, False, False

        if self.requires_two_factor and provider == "5":
            if token != self.remember_token:
                return 400, TWO_FACTOR_BODY, False, False
            if remember != "0":
                return 400, TWO_FACTOR_BODY, False, False
            issued = self.rotate_remember_token and not self.omit_remember_token
            payload: dict = {"access_token": self.access_token}
            payload["Key"] = self.wrapped_user_key()
            if issued:
                payload["TwoFactorToken"] = self.new_remember_token
            return 200, json.dumps(payload).encode(), True, issued

        if self.requires_two_factor:
            if token != self.two_factor_code or remember != "1":
                return 400, TWO_FACTOR_BODY, False, False
            payload = {"access_token": self.access_token}
            payload["Key"] = self.wrapped_user_key()
            issued = not self.omit_remember_token
            if issued:
                payload["TwoFactorToken"] = self.remember_token
            return 200, json.dumps(payload).encode(), True, issued

        payload = {"access_token": self.access_token}
        payload["Key"] = self.wrapped_user_key()
        return 200, json.dumps(payload).encode(), True, False


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
        authorization = self.headers.get("Authorization")
        if authorization != f"Bearer {self.vault.access_token}":
            self._respond(401, b"{}")
            return
        self._respond(200, json.dumps(self.vault.sync_payload()).encode())

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
            self._respond(200, json.dumps(self.vault.kdf).encode())
            return

        if self.path == "/identity/connect/token":
            form = urllib.parse.parse_qs(body.decode("utf-8"))
            self.vault.token_forms.append(form)
            status, payload, _ok, _issued = self.vault.token_response(form)
            self._respond(status, payload)
            return

        self._respond(404, b"{}")


class FakeServer:
    """Context manager that serves one ``FakeVault`` on an ephemeral port."""

    def __init__(self, vault: FakeVault):
        self.vault = vault
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.vault = vault
        self._httpd.daemon_threads = True

    def __enter__(self) -> FakeVault:
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        self._thread = thread
        self.vault.server_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        return self.vault

    def __exit__(self, *exc_info) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
