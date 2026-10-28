# Fujin Secrets - Bitwarden

Bitwarden (and Vaultwarden) secret adapter for the Fujin deployment tool.

## Installation

```bash
pip install fujin-secrets-bitwarden
```

Or with uv:

```bash
uv pip install fujin-secrets-bitwarden
```

## Prerequisites

Download and install the [Bitwarden CLI](https://bitwarden.com/help/cli/#download-and-install)
and log in to your account. When using a self-hosted [Vaultwarden](https://github.com/dani-garcia/vaultwarden)
instance, point the CLI at your server first:

```bash
bw config server https://vault.example.com
```

Then log in and unlock the vault:

```bash
bw login
export BW_SESSION="$(bw unlock --raw)"
```

## Configuration

```toml
[secrets]
adapter = "bitwarden"

[secrets.options]
password_env = "BW_PASSWORD"
folders = ["fujin/myapp/prod", "fujin/shared"]
```

| Option | Type | Description |
| --- | --- | --- |
| `password_env` | string | Name of the environment variable holding the vault password. Used to unlock the vault when `BW_SESSION` is not set. |
| `folders` | string or list of strings | Bitwarden folder names to resolve refs from, in lookup order (first match wins). A single name is accepted as a one-element order. An empty list is rejected at config load; omit the option for whole-vault name lookup. |

Alternatively, set the `BW_SESSION` environment variable (`export BW_SESSION="$(bw unlock --raw)"`).
When `BW_SESSION` is present, Fujin uses it directly and `password_env` is not
required.

### Folder layout

App secrets are usually kept in dedicated folders so personal logins can never
collide with app env refs:

```
fujin/
├── myapp/
│   ├── prod/          # prod-only secrets
│   └── staging/
└── shared/            # cross-app secrets (SMTP, S3, DB URLs)
```

Both forms of `folders` are accepted: a list (lookup order) or a bare string
(`folders = "fujin/shared"`, a one-element order). `folders = []` is a
configuration error at load time; remove the option instead when you want
whole-vault lookup.

Each deployment host can point at a different folder order while all hosts
share the same `.env` template. A host-scoped `[hosts.secrets]` block
**replaces** the global `[secrets]` table for that host (it is not merged), so
repeat `adapter` and `password_env` in the per-host block:

```toml
[secrets]
adapter = "bitwarden"

[secrets.options]
password_env = "BW_PASSWORD"
folders = ["fujin/shared"]

[[hosts]]
name = "prod"
address = "prod.example.com"
user = "deploy"

[hosts.secrets]
adapter = "bitwarden"

[hosts.secrets.options]
password_env = "BW_PASSWORD"
folders = ["fujin/myapp/prod", "fujin/shared"]

[[hosts]]
name = "staging"
address = "staging.example.com"
user = "deploy"

[hosts.secrets]
adapter = "bitwarden"

[hosts.secrets.options]
password_env = "BW_PASSWORD"
folders = ["fujin/myapp/staging", "fujin/shared"]
```

The same override can live in a machine-local per-host override file,
`.fujin/hosts/<name>.toml` (host-level keys sit at the top level):

```toml
# .fujin/hosts/prod.toml
[secrets]
adapter = "bitwarden"

[secrets.options]
password_env = "BW_PASSWORD"
folders = ["fujin/myapp/prod", "fujin/shared"]
```

## Usage

In your environment file (`.env` or configured via `envfile` in `fujin.toml`),
prefix secret values with `$`:

```env
DEBUG=False
DB_PASSWORD=$DB_PASSWORD
SMTP_PASSWORD=$fujin/shared/SMTP_PASSWORD
```

`$DB_PASSWORD` resolves to the item named `DB_PASSWORD`; `$fujin/shared/SMTP_PASSWORD`
resolves that item explicitly from the `fujin/shared` folder. `$$` escapes a
literal `$` and is never looked up (`PASSWORD=pa$$word` deploys `pa$word`).

## Resolution

### With `folders`

Fujin resolves folder names to ids once per deploy, then issues **one**
`bw list items --folderid <id> --raw` call per configured folder (no
`bw get password` calls). The resulting exact-name item maps are resolved in
configured order:

1. `$DB_PASSWORD` is looked up by exact item name in `fujin/myapp/prod`, then
   `fujin/shared`.
2. The first folder that contains an item with that exact name wins. Duplicate
   item names in later folders are ignored.
3. Folder names themselves must be unique in the vault: if `bw list folders`
   returns two folders with the same configured name, resolution fails closed
   and names that folder instead of picking one arbitrarily.
4. A folder-prefixed ref such as `$fujin/shared/postgres` resolves only from
   that folder, which disambiguates items that share a name across folders.
5. If no configured folder contains the ref, the deploy fails closed before
   anything is uploaded. The error names the env key, the ref and the folders
   that were tried; resolved values are never echoed.

Only **login** items that have a password are used. Secure notes, cards,
identities and login items without a password are skipped.

### Without `folders`

Backwards compatible with earlier releases: every ref is resolved by item name
across the whole vault with one concurrent `bw get password <name> --raw`
call per ref. Use `folders` when names can collide between apps and personal
logins.

## Related

- [Fujin Documentation](https://github.com/Tobi-De/fujin)
- [Bitwarden CLI Documentation](https://bitwarden.com/help/cli/)
