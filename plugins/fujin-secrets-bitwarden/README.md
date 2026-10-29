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

Then log in. Fujin asks for the master password once per deploy run and
unlocks the vault itself, so no session export is needed:

```bash
bw login
```

For unattended runs (or `fujin deploy --no-input`), provide the session or
password through the environment instead:

```bash
export BW_SESSION="$(bw unlock --raw)"
```

## Configuration

```toml
[secrets]
adapter = "bitwarden"

[secrets.options]
password_env = "BW_PASSWORD"  # optional; omit to be prompted
folders = ["fujin/myapp/prod", "fujin/shared"]
```

| Option | Type | Description |
| --- | --- | --- |
| `password_env` | string | Name of the environment variable holding the vault password. Used to unlock the vault when `BW_SESSION` is not set. Optional: when the variable is unset (or the option is omitted) Fujin prompts for the master password. |
| `folders` | string or list of strings | Bitwarden folder names to resolve refs from, in lookup order (first match wins). A single name is accepted as a one-element order. An empty list is rejected at config load; omit the option for whole-vault name lookup. |

Alternatively, set the `BW_SESSION` environment variable (`export BW_SESSION="$(bw unlock --raw)"`).
When `BW_SESSION` is present, Fujin uses it directly and `password_env` is not
required. When neither is available (including a configured `password_env`
whose variable is unset), Fujin prompts once for the master
password, unlocks with `bw unlock --raw` and keeps the returned session in
memory for the rest of the process (it is never written to disk or shell
history). `fujin deploy --no-input` disables that prompt, so unattended
deploys must set `BW_SESSION` or `password_env`.

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

Each deploy runs `bw sync` once before any lookup (right after unlocking, and
never for a template without `$REF`s), so folders and items created
server-side are visible immediately; if that sync fails, the deploy aborts
instead of reading the possibly stale local cache.

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
password: no `bw` binary, no unlocked session, no session token on disk. For
two-factor accounts a trusted-device token can be seeded once (see
[Two-factor accounts](#two-factor-accounts)).

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
environment. The only thing persisted is an optional trusted-device token for
two-factor accounts, written by `fujin-secrets-bitwarden-login`.

### CLI vs protocol

| | `bitwarden` (CLI) | `bitwarden-protocol` (HTTP) |
| --- | --- | --- |
| Requires the `bw` binary | yes | no |
| Requires an unlocked session | yes (`BW_SESSION`, `password_env`, or an interactive prompt) | no, only `password_env` |
| Performs the crypto | `bw` | this adapter |
| Two-factor accounts | whatever `bw` supports | supported after a one-time seeding step |
| Organizations/collections | via `bw` | not supported |
| Vault writes | none by Fujin | none; strictly read-only |

### Two-factor accounts

The adapter itself never prompts, so an account protected by two-factor
authentication needs one interactive seeding step on the machine that runs
deployments:

```bash
fujin-secrets-bitwarden-login --host prod
```

The command reads `email`, `server_url` and `password_env` from the selected
host's `[secrets.options]` block (explicit flags win), prompts for the
two-factor code (or takes `--code`) and the master password (or reads it from
`password_env`), logs in with `twoFactorRemember=1`, and stores the remember
token the server returns. It prints the state file path; secret values, codes
and tokens are never echoed.

Useful flags: `--email`, `--server-url`, `--provider` (provider id, default
`0` = authenticator), `--password-env`, `--state-dir`, and `--clear` to remove
the stored device.

The state file lives at
`${XDG_STATE_HOME:-~/.local/state}/fujin/bitwarden-protocol/bitwarden-protocol-<hash>.json`
(or under `$FUJIN_BW_STATE_DIR` when set), one file per server/account, mode
`0600`. It contains only:

```json
{
  "server_url": "https://vault.example.com",
  "email": "deploy@example.com",
  "device_identifier": "…",
  "two_factor_token": "…",
  "obtained_at": "2026-10-29T12:00:00Z"
}
```

No master password and no access/session token are stored. Later deploys send
the token with `twoFactorProvider=5` and the same device identifier, so no code
is needed. Remember tokens expire server-side (30 days by default); after 29
days the adapter treats the device as expired and refuses to send it, asking
for a re-seed. A missing, corrupt or rejected token likewise fails closed with
an error naming `fujin-secrets-bitwarden-login` and the state file path. The
adapter never deletes a token on its own: only `--clear` or a successful
re-seed replaces it.

### Scope and limits

- Personal vault, login items only. Secure notes, cards, identities, passwordless
  logins and attachments are ignored. Organization/collection items are skipped
  because their keys are not implemented.
- KDFs: PBKDF2-SHA256 (`Kdf = 0`) and Argon2id (`Kdf = 1`), exactly as
  advertised by the server's prelogin endpoint. Unknown KDFs fail closed.
- Accounts requiring two-factor authentication need a one-time
  `fujin-secrets-bitwarden-login` run; deploys then reuse the stored trusted
  device. Without a usable token the deploy fails closed with a value-free
  error naming that command.
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
