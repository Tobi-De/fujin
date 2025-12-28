# Fujin Secrets - Doppler

Doppler secret adapter for Fujin deployment tool.

## Installation

```bash
pip install fujin-secrets-doppler
```

Or with uv:

```bash
uv pip install fujin-secrets-doppler
```

## Prerequisites

Download and install the [Doppler CLI](https://docs.doppler.com/docs/cli) and sign in to your account.

Move to your project root directory and run `doppler setup` to configure your project.

## Configuration

Add the following to your `fujin.toml` file:

```toml
[secrets]
adapter = "doppler"
```

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
1. Uses the Doppler CLI configuration from your project setup
2. Resolves all secrets concurrently using `doppler run --command "echo $<name>"`
3. Returns the resolved environment variables

## Related

- [Fujin Documentation](https://github.com/Tobi-De/fujin)
- [Doppler CLI Documentation](https://docs.doppler.com/docs/cli)
