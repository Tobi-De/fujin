# Fujin Secrets - Bitwarden

Bitwarden (and Vaultwarden) secret adapters for the Fujin deployment tool.

Two adapters are provided:

- `bitwarden` shells out to the Bitwarden CLI (default, described below).
- `bitwarden-protocol` talks to the server HTTP API and decrypts the vault
  itself, so neither the CLI nor an unlocked session is needed.

Both resolve the same `$NAME` / `$folder/NAME` refs from your env template.

## Installation

```bash
pip install fujin-secrets-bitwarden
```

Or install it together with the CLI through the `bw` extra:

```bash
pip install "fujin-cli[bw]"
```

## Prerequisites (CLI adapter)

These prerequisites apply to the `bitwarden` CLI adapter only; the
`bitwarden-protocol` adapter needs no local Bitwarden software.

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

## The `bitwarden-protocol` adapter

This adapter talks to Bitwarden/Vaultwarden's HTTP API directly
(`identity/accounts/prelogin`, `identity/connect/token` and `api/sync`) and
performs the vault crypto locally. It is read-only and uses only the master
password: no `bw` binary, no unlocked session, no token stored on disk.

```toml
[secrets]
adapter = "bitwarden-protocol"

[secrets.options]
email = "deploy@example.com"
password_env = "BW_MASTER_PASSWORD"
server_url = "https://vault.example.com"          # optional
folders = ["fujin/myapp/prod", "fujin/shared"]    # optional
```

| Option | Type | Description |
| --- | --- | --- |
| `email` | string (required) | Account email. Stripped and lower-cased before use as the KDF salt. |
| `password_env` | string (required) | Name of the environment variable holding the master password. |
| `server_url` | string | Base URL, defaults to `https://vault.bitwarden.com`. A trailing `/` is ignored. |
| `folders` | string or list of strings | Same lookup-order semantics as the CLI adapter; omitted means whole-vault name lookup. |

`BW_SESSION` is deliberately **not** accepted: a session token cannot decrypt
the vault, so the master password must always be available in the deploy
environment. Nothing is persisted between deployments.

### CLI vs protocol

| | `bitwarden` (CLI) | `bitwarden-protocol` (HTTP) |
| --- | --- | --- |
| Requires the `bw` binary | yes | no |
| Requires an unlocked session | yes (`BW_SESSION` or `password_env`) | no, only `password_env` |
| Performs the crypto | `bw` | this adapter |
| Two-factor accounts | whatever `bw` supports | not supported |
| Organizations/collections | via `bw` | not supported |
| Vault writes | none by Fujin | none; strictly read-only |

### Scope and limits

- Personal vault, login items only. Secure notes, cards, identities, passwordless
  logins and attachments are ignored. Organization/collection items are skipped
  because their keys are not implemented.
- KDFs: PBKDF2-SHA256 (`Kdf = 0`) and Argon2id (`Kdf = 1`), exactly as
  advertised by the server's prelogin endpoint. Unknown KDFs fail closed.
- Accounts requiring two-factor authentication cannot log in; the 2FA challenge
  is reported as a value-free error and the deploy stops.
- HTTPS with the default certificate verification; there is no insecure
  override.
- Errors never include vault values, response bodies or credential material.

### Resolution

Refs use the same syntax as the CLI adapter (`$NAME`, `$folder/NAME`, and `$$`
for a literal `$`). Folder order, first-match-wins, folder-prefixed refs and
duplicate folder-name failures behave identically. Without `folders`, each ref
is matched by exact item name across the whole vault; if the same name exists
more than once, resolution fails closed instead of picking one arbitrarily.

A deploy whose env template contains no `$REF` performs no network requests at
all, and does not even require the master password to be set.

## Related

- [Fujin Documentation](https://github.com/Tobi-De/fujin)
- [Bitwarden CLI Documentation](https://bitwarden.com/help/cli/)
