# Fujin Secrets - Environment Variable

Environment variable secret adapter for Fujin deployment tool. Reads secrets from a JSON-formatted environment variable, making it ideal for CI systems like GitHub Actions.

## Installation

```bash
pip install fujin-secrets-env
```

Or with uv:

```bash
uv pip install fujin-secrets-env
```

## Configuration

Add the following to your `fujin.toml` file:

```toml
[secrets]
adapter = "env"

[secrets.options]
source = "FUJIN_SECRETS"
```

The `source` option specifies the name of the environment variable containing the JSON-formatted secrets.

## Usage

### GitHub Actions

In your workflow file, pass all secrets as JSON using `toJSON(secrets)`:

```yaml
- name: Deploy
  run: uvx --from fujin-cli --with fujin-secrets-env fujin deploy
  env:
    FUJIN_SECRETS: ${{ toJSON(secrets) }}
```

### Environment File

In your environment configuration (via `env` in `fujin.toml`), prefix secret values with `$`:

```env
DEBUG=False
SECRET_KEY=$SECRET_KEY
DATABASE_URL=$DATABASE_URL
AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
```

The `$` prefix indicates to Fujin that the value should be resolved from the secrets source.

## How it Works

The adapter:
1. Reads the JSON string from the configured environment variable
2. Parses the JSON into a dictionary
3. For each secret reference (prefixed with `$`), looks up the value in the parsed JSON
4. Returns the resolved environment variables

## Example

Given this GitHub Actions secret setup:
- `SECRET_KEY`: `my-secret-key`
- `DATABASE_URL`: `postgres://...`

And this fujin.toml env configuration:
```toml
[[hosts]]
env = """
DEBUG=False
SECRET_KEY=$SECRET_KEY
DATABASE_URL=$DATABASE_URL
"""
```

The adapter will resolve `$SECRET_KEY` and `$DATABASE_URL` from the JSON passed via `FUJIN_SECRETS`.

## Related

- [Fujin Documentation](https://github.com/Tobi-De/fujin)
- [GitHub Actions Secrets](https://docs.github.com/en/actions/security-guides/encrypted-secrets)
