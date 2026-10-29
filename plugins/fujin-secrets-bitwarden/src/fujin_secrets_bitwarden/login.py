"""Seed a trusted device for the ``bitwarden-protocol`` secret adapter.

Bitwarden requires a second factor on the password grant unless the client
presents a remember token issued to a previously trusted device. That token
can only be obtained with a one-time interactive login, which this command
performs:

1. read the account settings from ``fujin.toml`` (unless passed as flags),
2. log in with the master password, a two-factor code and
   ``twoFactorRemember=1``,
3. persist the returned remember token together with the device identifier
   used for the grant.

Later deploys then log in without a code. The adapter never prompts; when no
usable token exists it fails closed and points at this command.

Usage::

    fujin-secrets-bitwarden-login --host prod --code 123456

Nothing secret is printed. The master password is read from the environment
variable named by ``password_env`` when set, and otherwise prompted for
without echo.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fujin.config import Config, SecretConfig
from fujin.errors import ImproperlyConfiguredError, SecretResolutionError

from . import protocol, store

_ADAPTER = "bitwarden-protocol"
_PROVIDER_CHOICES = range(8)


class LoginError(Exception):
    """A user-facing seeding failure."""


def _provider(value: str) -> int:
    try:
        provider = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("provider must be an integer") from exc
    if provider not in _PROVIDER_CHOICES:
        raise argparse.ArgumentTypeError("provider must be between 0 and 7")
    return provider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fujin-secrets-bitwarden-login",
        description=(
            "Store a trusted-device token for the bitwarden-protocol secret "
            "adapter so later deployments need no two-factor code."
        ),
    )
    parser.add_argument(
        "-H",
        "--host",
        help="Host name from fujin.toml whose secret options should be used.",
    )
    parser.add_argument("--email", help="Bitwarden account email.")
    parser.add_argument(
        "--server-url",
        help="Bitwarden/Vaultwarden base URL (default: vault.bitwarden.com).",
    )
    parser.add_argument(
        "--code",
        help="Two-factor code. Prompted for when omitted (never echoed).",
    )
    parser.add_argument(
        "--provider",
        type=_provider,
        default=0,
        help="Two-factor provider id: 0=authenticator, 1=email, 2=duo, "
        "3=yubikey, 4=u2f, 7=webauthn (default: 0).",
    )
    parser.add_argument(
        "--password-env",
        help="Environment variable holding the master password; "
        "prompted for when unset.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Remove the stored trusted device and exit.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="Override the state directory holding remembered devices.",
    )
    return parser


def _option_string(secret_config: SecretConfig | None, name: str) -> str | None:
    if secret_config is None:
        return None
    value = secret_config.options.get(name)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _load_secret_config(host: str | None) -> SecretConfig | None:
    if not Path("fujin.toml").exists():
        return None
    config = Config.read(host)
    return config.effective_secrets(config.select_host(host))


def _password(password_env: str | None) -> str:
    if password_env:
        password = os.environ.get(password_env)
        if not password:
            raise LoginError(
                f"Environment variable '{password_env}' is not set; export the "
                "master password there or omit --password-env to be prompted"
            )
        return password
    password = getpass.getpass("Bitwarden master password: ")
    if not password:
        raise LoginError("the master password must not be empty")
    return password


def _run(args: argparse.Namespace) -> int:
    try:
        secret_config = _load_secret_config(args.host)
    except ImproperlyConfiguredError as exc:
        raise LoginError(str(exc)) from exc

    if secret_config is not None and secret_config.adapter != _ADAPTER:
        raise LoginError(
            f"the configured secret adapter is '{secret_config.adapter}'; this "
            f"command only seeds the '{_ADAPTER}' adapter"
        )

    email = args.email or _option_string(secret_config, "email")
    if email is None:
        raise LoginError(
            "an account email is required: pass --email or configure "
            "[secrets.options] email"
        )
    email = email.strip().lower()

    server_url = args.server_url or _option_string(secret_config, "server_url")
    server_url = (
        protocol._DEFAULT_SERVER_URL
        if server_url is None
        else protocol.normalize_server_url(server_url)
    )

    if args.clear:
        if store.clear(server_url, email, args.state_dir):
            print(f"Removed the stored trusted device for {email}.")
            return 0
        print(f"No stored trusted device for {email}.", file=sys.stderr)
        return 1

    password_env = args.password_env or _option_string(secret_config, "password_env")
    password = _password(password_env)

    code = args.code if args.code is not None else input("Bitwarden two-factor code: ")
    code = code.strip()
    if not code:
        raise LoginError("a two-factor code is required")

    client = protocol._Client(server_url)
    kdf = client.prelogin(email)
    master_key = protocol.derive_master_key(password, email, kdf)

    remembered = store.load(server_url, email, args.state_dir)
    device_identifier = (
        remembered.device_identifier if remembered is not None else str(uuid.uuid4())
    )
    try:
        result = client.login(
            email,
            protocol.master_password_hash(master_key, password),
            device_identifier=device_identifier,
            two_factor_code=code,
            two_factor_provider=args.provider,
            remember=True,
        )
    except protocol.TwoFactorRequired as exc:
        raise LoginError(
            "Bitwarden did not accept the two-factor login; check the code and "
            "the provider and try again"
        ) from exc

    if result.remember_token is None:
        raise LoginError(
            "Bitwarden did not return a remember token, so no trusted device "
            "could be stored; make sure 'remember me' is allowed for this account"
        )

    path = store.save(
        store.RememberedDevice(
            server_url=server_url,
            email=email,
            device_identifier=device_identifier,
            two_factor_token=result.remember_token,
            obtained_at=datetime.now(UTC),
        ),
        args.state_dir,
    )
    print(f"Stored the trusted device for {email} in {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except (LoginError, SecretResolutionError) as exc:
        message = getattr(exc, "message", None) or str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
