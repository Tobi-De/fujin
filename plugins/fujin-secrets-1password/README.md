# Fujin Secrets - 1Password

1Password secret adapter for Fujin deployment tool.

## Installation

```bash
pip install fujin-secrets-1password
```

Or with uv:

```bash
uv pip install fujin-secrets-1password
```

## Prerequisites

Download and install the [1Password CLI](https://developer.1password.com/docs/cli/) and sign in to your account.

You need to be actively signed in for Fujin to work with 1Password.

## Configuration

Add the following to your `fujin.toml` file:

```toml
[secrets]
adapter = "1password"
```

## Usage

In your environment file (`.env` or configured via `envfile` in `fujin.toml`), use 1Password secret references:

```env
DEBUG=False
AWS_ACCESS_KEY_ID=$op://personal/aws-access-key-id/password
AWS_SECRET_ACCESS_KEY=$op://personal/aws-secret-access-key/password
```

The secret reference format is: `$op://vault/item/field`

## How it Works

The adapter:
1. Uses the existing 1Password CLI session (requires you to be signed in)
2. Resolves all secrets concurrently using `op read <reference>`
3. Returns the resolved environment variables

## Related

- [Fujin Documentation](https://github.com/Tobi-De/fujin)
- [1Password CLI Documentation](https://developer.1password.com/docs/cli/)
- [1Password Secret References](https://developer.1password.com/docs/cli/secret-references/)
