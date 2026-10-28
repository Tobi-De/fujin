# Fujin Deployment Speedup Findings

Context: Analysis of the post-rework deployment pipeline (`docs/agents/deployment-rework.md`, head `df67ace`) to reduce deployment latency without dropping any features or breaking any §4 invariants.

A typical full deploy currently executes **38–48 sequential SSH exec channels / file transfers** (plus local build and secret resolution). On a 40 ms RTT VPS with shell startup per `conn.run()`, SSH round-trip overhead alone adds **2–4 seconds**, while redundant server-side Python venv installation and double-hashing dominate CPU and disk I/O (**10–30+ seconds** on Python apps).

All findings below preserve every invariant in `docs/agents/deployment-rework.md`:
- `current` symlink is truth; release dirs are immutable once committed.
- Releases are verified against their manifest before `current` points at them.
- `config_fingerprint`, `bundle_key`, and `release_digest` semantics remain unchanged.
- Unresolved `$REF` / `{placeholder}` values fail closed before any server mutation.
- Exit-code taxonomy (`0–6`) and app-scoped sudoers rules (`sudoers.py`) remain intact.

---

## Summary of Opportunities

| # | Optimization | Target | Saved / Deploy | Primary Mechanism |
|---|---|---|---|---|
| 1 | **Hardlink-clone `venv/` when `requirements.txt` & Python version match live release** | `serverops.py`, `deploy.py` | **5–25 s** | Skip `uv venv` + `uv pip install -r` + full `venv/` sha256 walk when deps haven't changed; only `--no-deps` install the wheel |
| 2 | **Eliminate double-hashing of `venv/**` & `bin/**` during `verify_staging`** | `serverops.py`, `transfer.py`, `deploy.py` | **1–5 s** | `hash_server_built` hashes `venv/**` at line 956 and `verify_staging` hashes the exact same files again 25 lines later |
| 3 | **Skip `install_units` & `install_caddyfile` when diff shows no unit/caddy changes** | `deploy.py`, `serverops.py` | **0.5–1.5 s** (5–15+ RTTs) | Use `prev_manifest` vs `final_manifest` diff (`store.units_installed_from`) instead of rewriting every unit/dropin + `daemon-reload` + `systemctl reload caddy` on every deploy |
| 4 | **Fix `acquire_lock` unconditional `sleep 0.5` and fuse lock + pre-flight checks** | `serverops.py`, `deploy.py` | **0.7–1.2 s** (8 RTTs → 1) | Acquire `flock` before backgrounding holder (eliminates `sleep 0.5`), fuse `_notice_old_layout` + `ensure_app_user` + `ensure_layout` + `acquire_lock` + `sweep_stale_staging` + `read_state` + `_resolve_live_digest` |
| 5 | **Run local `run_build` BEFORE opening SSH connection; parallelize with secrets** | `deploy.py` | **0.5–3 s** + avoids idle SSH hold | `run_build` currently runs *inside* `with connection()` while holding the server deploy lock; can run concurrently with secret resolution before SSH connects |
| 6 | **Drop redundant `manifest.json` transfers, `requirements.txt` SCP, and cache `prev_manifest` locally** | `deploy.py`, `serverops.py`, `build.py` | **0.3–1.0 s** (3 transfers) | `requirements.txt` is uploaded twice (rsync + `conn.put`); `manifest.json` is written to staging twice (rsynced pre-merge, then overwritten via SCP post-merge); immutable live manifests can be cached locally by `<digest>` |
| 7 | **Fuse `install_code_into_staging` + `set_envfile_mode` + `normalize_staging_ownership` + `hash_server_built`** | `serverops.py`, `deploy.py` | **0.4–0.8 s** (6 RTTs → 1) | Combine post-rsync staging preparation into a single remote script execution and eliminate duplicate `chmod -R` / `find` walks over `venv/` |
| 8 | **Fuse `install_units` per-file `_sudo_write` calls** | `serverops.py` | **0.3–1.0 s** (2N+4 RTTs → 1) | Currently runs 1 SSH command per unit file, 1 per dropin dir, 1 per dropin file, plus 2 `find` scans and `daemon-reload` |
| 9 | **Fuse `_converge` + `_wait_for_units` socket/unit probes** | `serverops.py`, `deploy.py` | **0.3–0.8 s** (3+2S RTTs → 2) | `template_instance_plan` makes 3 separate `conn.run` calls; `_wait_for_units` runs `2 * len(sockets)` serial `_probe` calls per poll tick |
| 10 | **Fuse post-activation tail (`write_state` + `list_releases` + `collect_garbage` + `log_operation` + `release_lock`)** | `deploy.py`, `serverops.py`, `audit.py` | **0.3–0.6 s** (6+ RTTs → 1) | Combine state write, release listing, GC `rm -rf`, audit append, and lock release into one final remote call |
| 11 | **OpenSSH `ControlMaster` multiplexing for `rsync` and `conn.get()`** | `transfer.py`, `connection.py` | **0.3–0.8 s** | `rsync_to_staging` and `conn.get` spawn separate OS `ssh` processes that perform full TCP + SSH handshakes from scratch |

---

## Detailed Findings

### 1. Server-Side Venv Reuse / Cloning (`serverops.py:556-589`, `deploy.py:935-959`)

**Current behavior:**
On every `python-package` deploy, `install_code_into_staging` runs:
1. `uv venv --relocatable -p <py> --managed-python <staging>/venv`
2. `conn.put(requirements, "<staging>/requirements.txt")`
3. `uv pip install --python <staging>/venv/bin/python <dist> --no-deps -r <staging>/requirements.txt`
4. `chmod -R u+rwX,go+rX <staging>/venv && chmod -R u+x <staging>/venv/bin`
5. `hash_server_built` walks and SHA256-hashes every file in `venv/**` (often 3,000–15,000 files).

**Why it is slow:**
In 90%+ of deploys, only application code (`bin/<wheel>`) changed while `requirements.txt` and `python_version` are identical to `prev_manifest`. Re-creating the venv, compiling bytecode (`UV_COMPILE_BYTECODE=1`) for all third-party packages, and hashing thousands of files on a 1–2 vCPU VPS takes 5–25s.

**Speedup:**
- When `prev_manifest` exists and `prev_manifest.entries.get("requirements.txt")` has the same `sha256` as the new `requirements.txt` (and `python_version` in `config_fingerprint` has not changed):
  - Hardlink-clone the live release's `venv` into staging (`cp -al <live_release>/venv <staging>/venv`), then run `uv pip install --python <staging>/venv/bin/python --no-deps --force-reinstall --no-cache <dist>` (or `--reinstall-package <app>`).
  - Because `uv` unlinks replaced files before writing new ones (rather than mutating in-place), hardlinked files in `<live_release>/venv` remain strictly immutable.
  - During `hash_server_built`, any file in `<staging>/venv` whose inode matches `<live_release>/venv` (or `st_nlink > 1`) can reuse its `sha256` and `mode` directly from `prev_manifest.entries` without reading or hashing its bytes from disk.

---

### 2. Double-Hashing the Entire Release Tree (`deploy.py:956-986`, `serverops.py:601`, `transfer.py:347`)

**Current behavior:**
1. At `deploy.py:956`, `serverops.hash_server_built(conn, remote_staging, ...)` runs a Python script over SSH that walks `venv/**` (or `bin/**`) and computes `sha256` and `mode` of every file.
2. At `deploy.py:961`, `_write_remote_manifest` uploads the merged `manifest.json` (with those exact hashes) via SCP.
3. At `deploy.py:984` (when `reuse_verdict` is `None`, i.e., every normal deploy), `transfer.verify_staging` runs `_VERIFY_SCRIPT` over SSH, which reads `manifest.json` and **re-computes `sha256` of every single entry in `manifest["entries"]`**, including all `venv/**` files that were hashed on the server milliseconds earlier.

**Why it is futile:**
- `venv/**` was just generated on the server and its hashes in `final_manifest` came *from* `remote_staging` moments before. Re-hashing `venv/**` against the hashes just read from `remote_staging` is redundant.
- `verify_staging` (for client-known files) and `hash_server_built` (for `venv/**` / `bin/**`) can be **fused into a single server-side Python script**:
  1. Upload client staging tree (with client `manifest.json` already inside it from `rsync`).
  2. After `install_code_into_staging` and `normalize_staging_ownership`, run **one** Python script that:
     - Verifies client-known entries against the rsynced client `manifest.json` (aborting if any client file is missing, corrupt, or has wrong mode),
     - Simultaneously hashes `venv/**` / `bin/**`,
     - Merges the `venv/**` entries into `manifest.json`, computes `final_digest` (`sha256(canonical_manifest_bytes)`), and writes the final `manifest.json` directly on the server.
- **Impact:** Eliminates 100% of the second full-tree SHA256 pass, eliminates `_write_remote_manifest` SCP upload (hundreds of KB), and collapses multiple SSH round-trips into one.

---

### 3. Redundant `install_units` and `install_caddyfile` on Unchanged Deploys (`deploy.py:994-1002`, `serverops.py:728-933`)

**Current behavior:**
Every deploy that isn't a pure no-op executes:
- `serverops.install_units`:
  - `find /etc/systemd/system ...` (1 RTT)
  - `_sudo_write` for **every** unit file (1 RTT each)
  - `sudo mkdir -p` + `_sudo_write` for **every** dropin file (2 RTTs each)
  - `_remove_empty_dropin_dirs` (`find` = 1 RTT)
  - `sudo systemctl daemon-reload` (1 RTT, ~150–400ms on systemd)
- `serverops.install_caddyfile`:
  - `test -f <caddy>` (1 RTT)
  - `test -f <bak>` (1 RTT)
  - `cat <caddy>` (1 RTT)
  - `_sudo_write(<bak>)` (1 RTT)
  - `_sudo_write(<caddy>)` (1 RTT)
  - `sudo systemctl reload caddy` (1 RTT, ~100–300ms)

**Why it is futile:**
- `State` already tracks `units_installed_from: str | None` (`store.py:93`), and `prev_manifest` is already loaded in memory.
- If `prev_manifest` is present, `state.units_installed_from == live_digest`, and no `EntryKind.UNIT` entries were added, removed, or modified between `prev_manifest` and `final_manifest` (and `--force` is not set), the unit files on disk in `/etc/systemd/system` are already identical. Rewriting them and running `systemctl daemon-reload` is unnecessary.
- In `install_caddyfile` (`serverops.py:913`), `old_content` is read via `cat`, and even when `old_content == content`, it still writes the backup file, rewrites the main file, and executes `sudo systemctl reload caddy`. Checking whether `Caddyfile` changed in the manifest diff (or short-circuiting when `old_content == content` and `!bak_exists`) skips the backup write, main write, and `systemctl reload caddy`.

---

### 4. `acquire_lock` Unconditional `sleep 0.5` & Pre-Deploy Handshake Chatter (`serverops.py:337-430`, `deploy.py:359-369`)

**Current behavior:**
Before `_deploy` starts, `Deploy.__call__` runs 7–9 sequential SSH commands:
1. `test -d <install_dir>` (`_notice_old_layout`)
2. `test -d <app_dir>/store` (`_notice_old_layout`)
3. `sudo groupadd -f ... && sudo useradd ...` (`ensure_app_user`)
4. `id -u <app_user>` (`ensure_app_user`)
5. `sudo mkdir -p ... && sudo chown ... && sudo chmod ...` (`ensure_layout`)
6. `command -v flock` (`acquire_lock`)
7. `setsid nohup bash -c 'echo $$-$TOKEN > holder; exec 9>lock; flock -x 9 && sleep 7200' &` (`acquire_lock`)
8. `sleep 0.5; if flock -n ...` (`acquire_lock` — **hardcoded 500ms sleep on every deploy**)
9. `find <releases> -maxdepth 1 -type d -name '.staging-*'` (`sweep_stale_staging`)

**Speedup:**
- **Eliminate `sleep 0.5` in `acquire_lock`**: `acquire_lock` currently backgrounds `setsid nohup bash -c ...` and then sleeps 0.5s waiting for the background subshell to open the lock and write the holder file. Instead, test/acquire the lock synchronously in the foreground or have the subshell write the holder file after `flock -n 9` succeeds before backgrounding the `sleep {ttl}` child (`exec 9>{lock_q}; flock -n 9 || exit 1; setsid nohup bash -c 'echo $$-token > holder; exec sleep 7200' >/dev/null 2>&1 </dev/null &`). That acquires the lock in **1 RTT with 0ms sleep**.
- **Skip `sudo groupadd` / `sudo useradd` when user exists**: `id -u <app_user> >/dev/null 2>&1 || (sudo groupadd -f ... && sudo useradd ... && id -u <app_user>)` avoids two `sudo` executions on every deploy after the first.
- **Fuse the pre-flight sequence**: `_notice_old_layout`, `ensure_app_user`, `ensure_layout`, `acquire_lock`, `sweep_stale_staging`, `read_state`, and `_resolve_live_digest` can be combined into a single composite shell script on connection open.

---

### 5. Local Build & Secret Resolution Ordering (`deploy.py:330-370, 720-743`)

**Current behavior:**
1. Resolve secrets synchronously (`deploy.py:332`).
2. Open SSH connection, run `ensure_app_user`, `ensure_layout`, `acquire_lock`, `sweep_stale_staging` (`deploy.py:358-368`).
3. **While holding the remote SSH connection and remote deploy lock idle**, run `build.run_build()` locally (`deploy.py:722`).

**Speedup:**
- `build.run_build()` does not depend on resolved secret values (`bundle_key` explicitly excludes secret values per §4 Invariants) or any remote server state.
- Run `build.run_build()` and `resolve_secrets()` **before** opening the SSH connection (and optionally in parallel via `ThreadPoolExecutor(max_workers=2)`):
  - Local build (`uv build` / `cargo build`) and external secret CLI (`bw` / `op` / `doppler`) overlap in wall-clock time.
  - If either fails, the deploy aborts immediately without opening an SSH connection or taking the remote lock.
  - `client_manifest` and `client_digest` are ready before SSH connects.

---

### 6. Redundant File Transfers & Local Cache Hits (`deploy.py`, `serverops.py`, `build.py`, `config.py`)

1. **Duplicate `requirements.txt` upload**:
   - `build._assemble_staging` writes `requirements.txt` into `staging_dir` (`build.py:298`).
   - `transfer.rsync_to_staging` uploads `staging_dir` (including `requirements.txt`) to `remote_staging` (`deploy.py:922`).
   - Immediately after, `install_code_into_staging` (`serverops.py:573`) calls `conn.put(requirements, f"{staging_dir}/requirements.txt")` via SCP. The file is already at `{staging_dir}/requirements.txt` from rsync; dropping `conn.put` saves an SCP round-trip.
2. **Duplicate `manifest.json` upload**:
   - `deploy.py:872` writes `client_manifest` to `staging_dir / "manifest.json"` before `rsync_to_staging`, so `rsync` uploads it.
   - Then `deploy.py:961` calls `_write_remote_manifest`, which writes `final_manifest` to a temp file and uploads it via `conn.put` (SCP) to overwrite `manifest.json`.
3. **Cache immutable committed manifests locally by `<digest>`**:
   - `read_current_manifest` (`deploy.py:878`) spawns an external `ssh cat` process (`conn.get`) on every deploy to download `current/manifest.json` (often 200KB–1MB for Python venvs).
   - Because a committed release `<digest>` is immutable and content-addressed by the SHA256 of its canonical manifest bytes (§4), caching committed manifests locally under `.fujin/cache/manifests/<digest>.json` allows `read_current_manifest` to check the local cache by `live_digest` first, completely skipping the download on subsequent deploys from the same machine.
4. **Duplicate local reads on build cache hit (`build.py:510-539`)**:
   - `_verify_staged_files` reads and SHA256-hashes every file in `cache_dir` (`build.py:351`). Immediately after (`build.py:515`), `run_build` reads every file in `cache_dir` a second time, re-hashes `dist_path` (`_find_cached_dist`), and re-writes `built.json` (`_write_built_record`) even when no unit/asset file drifted.
5. **Uncached `get_git_short_hash()` (`config.py:216, 725`)**:
   - `get_git_short_hash()` is called multiple times during a single deploy (`config.local_version` in `run_build`, `build_manifest`, `_show_deployment_summary`, `log_operation`), spawning `git rev-parse --short HEAD` each time. Decorating `get_git_short_hash()` with `@cache` eliminates repeated subprocess calls.

---

### 7. Post-Rsync Staging Chmod / Chown / Env-Mode Fusion (`deploy.py:933-955`, `serverops.py:582-586, 700-721, 1014-1025`)

**Current behavior:**
1. `transfer.rsync_to_staging` runs `command -v rsync` over SSH (1 RTT) + `rsync` subprocess.
2. `serverops.set_envfile_mode`: `chmod 0640 <staging>/.env` (1 RTT).
3. `install_code_into_staging`:
   - In `python-package` mode: `uv venv` (1 RTT) → `uv pip install` (1 RTT) → `chmod -R u+rwX,go+rX <staging>/venv && chmod -R u+x <staging>/venv/bin` (1 RTT).
   - In `binary` mode: `mkdir -p <staging>/bin` (1 RTT) → `chmod 755 <staging>/bin/<app>` (1 RTT).
4. `normalize_staging_ownership`: `sudo chgrp -R <app_user> <staging> && sudo find <staging> -type d -exec chmod g+rxs {} + && sudo find <staging> -links 1 -type f -exec chmod g+rX {} +` (1 RTT).

**Speedup:**
- Set `0o640` on `staging_dir / ".env"` locally before `rsync -rlptD` (already done via `client_manifest.entries` loop at `deploy.py:868-871`!), so `.env` and `bin/<app>` already arrive on the server with modes `0640` and `0755`.
- `chmod -R u+rwX,go+rX <staging>/venv` followed immediately by `sudo find <staging> -links 1 -type f -exec chmod g+rX {} +` walks the entire `venv/` directory tree twice in a row.
- Combine `uv venv && uv pip install`, `.env` mode enforcement, and `normalize_staging_ownership` into a single compound remote command, eliminating 4–5 SSH round-trips and one full filesystem walk over `venv/`.

---

### 8. Batching Unit & Dropin Installation (`serverops.py:728-841`)

**Current behavior:**
When units do change, `install_units` executes:
1. `find . -maxdepth 2 ...` in `/etc/systemd/system` (`_remove_stale_units`)
2. One `_sudo_write` (`echo <b64> | base64 -d | sudo tee ... && sudo mv ...`) per unit file
3. One `sudo mkdir -p` + one `_sudo_write` per dropin file
4. `find . -maxdepth 1 -type d -name '<app>*.d' -empty` (`_remove_empty_dropin_dirs`)
5. `sudo systemctl daemon-reload`

**Speedup:**
- All `sudo mkdir -p`, `echo <b64> | base64 -d | sudo tee ... >/dev/null && sudo mv ...`, and `sudo systemctl daemon-reload` commands use the exact command forms permitted by `sudoers.py` and can be joined with `&&` into a **single** `conn.run()` call.
- Only write unit/dropin files whose content actually changed compared to `prev_manifest` (when `state.units_installed_from == live_digest`).

---

### 9. Fusing Convergence Queries and Readiness Probes (`serverops.py:1032-1112`, `deploy.py:1359-1436`)

**Current behavior in `_converge`:**
- `template_instance_plan` makes 3 sequential SSH calls on every deploy (even when the app has no `@.service` template units):
  1. `systemctl list-unit-files --no-legend '<app>-*@*.service'`
  2. `find /etc/systemd/system -maxdepth 2 -type l -name '<app>-*@*.service' -printf '%f\n'`
  3. `systemctl list-units --type=service --state=active --no-legend '<app>-*@*.service'`

**Current behavior in `_wait_for_units`:**
- On every poll iteration (`deploy.py:1394-1428`), it runs:
  1. `systemctl is-failed <units...>` (1 RTT)
  2. For each socket in `sockets`: `test -S <path>` (1 RTT) followed by `systemctl is-active <socket_unit>` (1 RTT).

**Speedup:**
- Fuse the 3 read-only listing commands in `template_instance_plan` into a single `conn.run` call separated by a marker line (3 RTTs → 1 RTT). If neither `prev_manifest` nor `final_manifest` has any `@.service` units, skip `template_instance_plan` unless `--force` is given.
- In `_wait_for_units`, combine `systemctl is-failed <service_units + socket_units>` and `test -S <socket_path>` checks into a **single** `_probe()` call per poll iteration (`1 + 2 * len(sockets)` RTTs → 1 RTT per tick).

---

### 10. Fusing Post-Activation Bookkeeping (`deploy.py:1033-1051`, `audit.py:61-62`, `serverops.py:206-249, 448-471, 975-986`)

**Current behavior:**
After `post_start` hooks finish, `_transfer_and_activate` and `__call__` run **7+ sequential SSH commands**:
1. `write_state`: `printf ... | base64 -d > state.json.tmp && mv -f state.json.tmp state.json`
2. `list_releases`: `cd <app_dir> && ls -1t releases/`
3. `collect_garbage`: `rm -rf <app_dir>/releases/<digest>` (1 SSH command **per** deleted release)
4. `log_operation` (1/2): `sudo mkdir -p /opt/fujin/.audit`
5. `log_operation` (2/2): `echo ... | sudo tee -a /opt/fujin/.audit/<app>.log >/dev/null`
6. `release_lock` (1/2): `kill -- -<pid>; kill <pid>; true`
7. `release_lock` (2/2): `rm -f <app_dir>/store/lock.holder`

**Speedup:**
- In `collect_garbage`, pass all digests to a single `rm -rf` invocation (`rm -rf <dir1> <dir2> ...`).
- In `log_operation`, combine `sudo mkdir -p /opt/fujin/.audit && echo ... | sudo tee -a ... >/dev/null` into 1 call.
- In `release_lock`, combine `kill ...; rm -f ...` into 1 call.
- Chain `write_state` + `log_operation` + `list_releases` into one remote round-trip.

---

### 11. OpenSSH `ControlMaster` Multiplexing for `rsync` and `conn.get` (`transfer.py:156-172`, `connection.py:397-428`)

**Current behavior:**
- Fujin holds an active `libssh2` (`ssh2-python`) session in `conn`, but `conn.get()` (used by `read_remote_file` / `read_current_manifest`) and `transfer.rsync_to_staging()` both spawn external OpenSSH `ssh` processes.
- Each external `ssh` invocation performs a full TCP handshake, key exchange, and authentication with `sshd` (~250–600ms each).

**Speedup:**
- Pass `-o ControlMaster=auto -o ControlPath=/tmp/fujin-ssh-%r@%h:%p -o ControlPersist=60s` in `_ssh_transport_opts` (`transfer.py`) and `conn.get()` (`connection.py`), so external `ssh` / `rsync` calls reuse a single multiplexed OpenSSH connection.

---

## End-to-End Pipeline Comparison

```
Current Full Deploy (Python app, unchanged requirements, no unit/Caddy changes):
  Local secrets (serial)  ──►  9 pre-flight SSH RTTs (+0.5s lock sleep)  ──►  Local build
  ──►  SCP download manifest.json  ──►  link_unchanged + rsync + SCP requirements.txt
  ──►  uv venv + uv pip install (all deps) + 2x chmod/find walks
  ──►  hash_server_built (full venv SHA256 walk #1) + SCP upload manifest.json
  ──►  verify_staging (full venv SHA256 walk #2) + commit_release
  ──►  install_units (N+4 RTTs + daemon-reload) + install_caddyfile (6 RTTs + reload caddy)
  ──►  swap_current + restart + converge (3 RTTs) + readiness + 7 post-deploy RTTs
  Total: ~42 SSH round-trips + 2 full venv SHA256 walks + full uv pip install

Optimized Full Deploy (same scenario, 100% feature parity):
  Local build + secrets (parallel, pre-SSH)
  ──►  1 fused pre-flight + lock + state/digest read (0ms sleep, local manifest cache hit)
  ──►  link_unchanged + rsync (no redundant requirements.txt / manifest.json SCP)
  ──►  hardlink-cloned venv + uv pip install --no-deps <wheel> + single fused verify/hash/commit
  ──►  skip unchanged units/Caddyfile  ──►  swap_current + restart + fused converge/readiness
  ──►  1 fused state + GC + audit + unlock call
  Total: ~8–10 SSH round-trips + 0 redundant venv SHA256 walks + wheel-only uv install
```
