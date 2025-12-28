# Fujin Secrets - Bitwarden

Bitwarden secret adapter for Fujin deployment tool.

## Installation

```bash
pip install fujin-secrets-bitwarden
```

Or with uv:

```bash
uv pip install fujin-secrets-bitwarden
```

## Prerequisites

Download and install the [Bitwarden CLI](https://bitwarden.com/help/cli/#download-and-install) and log in to your account.

You should be able to run `bw get password <name_of_secret>` and get the value for the secret. This is the command that will be executed when pulling your secrets.

## Configuration

Add the following to your `fujin.toml` file:

```toml
[secrets]
adapter = "bitwarden"
password_env = "BW_PASSWORD"
```

To unlock the Bitwarden vault, the password is required. Set the `BW_PASSWORD` environment variable in your shell. When Fujin signs in, it will always sync the vault first.

Alternatively, you can set the `BW_SESSION` environment variable. If `BW_SESSION` is present, Fujin will use it directly without signing in or syncing the vault. In this case, the `password_env` configuration is not required.

## Usage

In your environment file (`.env` or configured via `envfile` in `fujin.toml`), prefix secret values with `$`:

```env
DEBUG=False
AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
```

The `$` sign indicates to Fujin that it is a secret that should be resolved using the configured adapter.

## How it Works

The adapter:
1. Authenticates with Bitwarden using `BW_SESSION` or unlocks the vault with the configured password
2. Syncs the vault (when unlocking)
3. Resolves all secrets concurrently using `bw get password <name> --raw`
4. Returns the resolved environment variables

## Related

- [Fujin Documentation](https://github.com/Tobi-De/fujin)
- [Bitwarden CLI Documentation](https://bitwarden.com/help/cli/)
