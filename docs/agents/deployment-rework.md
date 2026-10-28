# Fujin deployment rework — state of the system

Handoff document for a review of the deployment process (speed, simplicity,
feature parity). Everything below is implemented, committed and pushed to
`main`; the unit suite is green. Integration tests are Docker-based and are
being restructured (see §9).

- Repo: `Tobi-De/fujin`, branch `main`
- Baseline before this batch: `66a33d9` (`#94`: content-addressed release tree)
- Current head: `df67ace`
- Design source being backported: `../kaze/docs/*.md` (kaze, an Odin rewrite)

## 1. What this batch shipped

Eleven issues closed on top of the release-tree keystone (`#76`–`#83`, `#87`
were already merged before this batch).

| Issue | Commit | What it does |
|---|---|---|
| #85 | `192c30f` | `$$` → literal `$` in env values; one shared env-template parser for core + all adapters |
| #84 | `0f12781` | project-level `env`/`envfile` template + host-scoped `[[hosts]].env` overlays merged by key |
| #89 | `b596d79` | deploy-time replica convergence in both directions, every deploy (incl. no-op) |
| #88 | `24f7d8d` | readiness tiers: `Type=notify`, explicit 30 s `active` fallback, socket file + active socket unit, opt-in `[health_check]` http/command probes, auto-rollback |
| #91 | `0bf55e5` | documented exit-code taxonomy 0–6, used by every error class and command |
| #95 | `bf405c8` | Bitwarden adapter: folder-scoped refs, one `bw list items` per folder, fail-closed |
| #86 | `23a1fc8` | per-host `config_fingerprint` (host-scoped build cache) + cross-host deploy guard with `--force` |
| #90 | `b5d8ec9` | `deploy --dry-run`: read-only activation plan |
| #92 | `b5d8ec9` | `fujin doctor`: reconcile state/units/instances/permissions/Caddy/lock/audit; `--fix` opt-in |
| #93 | `18d5c7a` | scoped sudoers drop-in instead of `NOPASSWD:ALL`; transactional `server harden` |
| #74 | `49699da` | `{shared_dir}` resolves in units/Caddyfiles/hooks |

Supporting fixes in `49699da`: dropin parent directories are created before
writing; `template_instance_plan` also scans `.wants/.requires` symlinks;
`rollback` integration test uses the current prompt API. `df67ace` adds a
180 s per-test timeout to integration runs.

## 2. Module map

| File | Role |
|---|---|
| `config.py` (735) | `Config`/`HostConfig`/`SecretConfig`/`HealthCheckConfig`; four-layer merge (`fujin.toml` → `[hosts.<name>]` → `.fujin/hosts/<name>.toml`); `effective_env`, `effective_secrets` |
| `manifest.py` (538) | `Manifest`/`Entry`/`EntryKind`; `build_manifest`, `release_digest`, `diff_manifests`, `config_fingerprint`, `bundle_key`; `canonical_manifest_bytes` (digest input) vs `stored_manifest_bytes` (adds host) |
| `store.py` (515) | pure logic: `State` (state.json), `record_deploy/record_rollback`, `is_noop`, `restart_set`, `desired_instances`, `convergence`, `gc_plan`, `activation_plan` |
| `build.py` (586) | local build cache; `prebuild()` (single pre-build computation shared with dry-run), `run_build`, `bundle_key` inputs, cache verification |
| `transfer.py` (471) | rsync upload to `.staging-<pid>`, server-side hardlink dedup, `verify_staging`, `commit_release`, `swap_current`, `sweep_stale_staging` |
| `serverops.py` (1153) | server I/O port: layout/lock, code install (venv/binary), hashing, unit/dropin install, Caddy install, state/manifest read-write, convergence plan, health probe, doctor probes |
| `connection.py` (622) | ssh2 wrapper (select loop, pty, sudo watchers, incremental UTF-8); `get()` via system `ssh cat`; transport failures → `ConnectionError` (5) |
| `secrets.py` (239) | env template parser (`parse_env_template`, `split_env_template`, `render_template`), adapter registry, fail-closed resolution |
| `sudoers.py` (486) | app-scoped `Cmnd_Alias` rendering; transactional install/harden root scripts |
| `commands/deploy.py` (1888) | orchestration: dry-run, build, secrets, manifest, transfer/verify/commit, swap, restart, converge, readiness, health check, hooks, rollback, gc, audit |
| `commands/rollback.py` (579) | target selection, activation, convergence, state/audit |
| `commands/doctor.py` (916) | 8 read-only checks + lock-aware `--fix` |
| `commands/server.py` (723) | bootstrap, create-user (scoped rule), harden, keys, exec |
| `exit_codes.py` (69) | the 0–6 taxonomy constants |
| `plugins/fujin-secrets-*` | `system` (built-in) + bitwarden/doppler/1password/env adapters; all use the core parser |

## 3. Deploy pipeline (current)

`Deploy.__call__` (`commands/deploy.py`):

0. `--dry-run` → `_dry_run()`: config validation (env placeholders, health
   probe) before any output/SSH, then a read-only plan; returns 0.
1. Resolve secrets for the selected host (`effective_env` merged template →
   adapter). Secret values registered for redaction; refs only in output.
2. `build.prebuild()` + `run_build()`: bundle key over config fingerprint,
   build command/env, requirements/pyproject, unit/Caddy/asset sources; cache
   hit re-verifies file hashes.
3. Assemble client tree (units/dropins, Caddyfile, assets, rendered `.env`,
   dist) → client manifest + client digest.
4. No-op check: `store.is_noop(client_digest, state)` → skip transfer/restart,
   still converge replicas and run `post_start`; `--force` bypasses.
5. Read the live manifest (diff base + rollback target) and check the
   cross-host guard (live host AND fingerprint differ → error unless
   `--force`).
6. Upload: rsync staging + hardlink dedup of byte-identical files from the
   live release.
7. Server-side code install (`uv`/venv or binary), ownership normalize,
   server-side hashing of `venv/**` or `bin/**`, merged manifest, final
   digest.
8. Verify staging against the manifest → commit `releases/<digest>/`.
9. Install units/dropins (`/etc/systemd/system`) and Caddyfile.
10. `pre_install` (old release live) → `post_install` (new release dir, not
    live yet) hooks.
11. `swap_current` (atomic `current.next` + `mv -T`).
12. `_restart`: diff-based restart set (unit change → that unit; env change →
    env-fed units; code/dist change → all app units; sockets/timers only on
    their own change; oneshots excluded; `--full-restart` forces all).
13. `_converge`: `serverops.template_instance_plan` = desired `svc@1..N`
    vs enabled ∪ active (incl. `.wants/.requires` symlinks); `enable --now`
    missing/stopped/disabled, `disable --now` extras highest-first.
14. `_wait_for_units`: `systemctl is-failed` must report `active`, 0.5 s
    interval, 30 s deadline; sockets additionally `test -S` + socket unit
    active; `Type=notify` units excluded (restart already blocked on READY=1).
15. `_run_health_check`: optional probe (`curl -sf` or `timeout N bash -lc`),
    retried until `[health_check].timeout`, failure → journal + rollback.
16. `post_start` (non-fatal on failure), `record_deploy` (state.json incl.
    fingerprint), gc, audit log (incl. release digest).

Failure paths: readiness/restart failure → interactive (or `--no-input`
auto) rollback to `state.current`, exit 3; no rollback possible → 4; the
rollback itself failing → 6; transport loss → 5. Cleanup never masks the
code.

## 4. Invariants (do not break)

- `current` symlink is truth; release dirs are immutable once committed.
- A release is verified against its manifest before `current` points at it.
- `config_fingerprint` excludes secret values, replicas, version, aliases,
  hooks, local paths; includes host identity (address/user/port/native auth/
  key path/passphrase env) and env identity as `KEY: $REF|null`.
- `bundle_key` (build cache) is host-scoped; rendered `.env` and secret
  values never enter the build cache, fingerprint or logs.
- `release_digest` covers entries + dist + version + fingerprint; `host` is
  stored but excluded from the digest.
- `$REF` runs to end of line; `$$` is unescaped before ref detection;
  unresolved refs fail closed before any server I/O.
- Exit codes 0–6 are stable and documented (`docs/commands/exit-codes.rst`).
- Deploy user has an app-scoped sudoers rule; never `NOPASSWD:ALL`.

## 5. Performance-relevant operations (where time goes)

Implemented so far (all unit-tested, committed):

- `acquire_lock` acquires before announcing, polls at 50 ms (was a fixed
  0.5 s sleep), and accepts only its own token — a blocked/foreign holder can
  never be mistaken for success.
- `ensure_app_user` is one command and repairs a missing group.
- `install_units` is one round trip (was ~10 for 3 units + 2 dropins);
  `.wants`/`.requires` enablement symlinks are never treated as stray units.
- `template_instance_plan` is one query (was 3); the readiness poll is one
  probe per tick (was 1+2 per socket).
- `prepare_staging` is 1 call in binary mode, 3 in python-package (uv venv,
  uv pip, one fused permission pass) — venv setgid is applied by the
  privileged `find` walk.
- A redeploy with unchanged requirements hardlink-clones the live venv and
  installs only the wheel (`--no-deps --compile-bytecode`; the compile covers
  just the newly installed package). A marker file created right before the
  install bounds the permission pass to `find venv -newer <marker>`, so the
  live release's hardlinked inodes keep their modes and ctimes.
  `hash_server_built` reuses sha256/mode for files whose inode matches the
  live release. The live release is proven byte-identical by an integration
  test.
- `verify_staging` no longer re-hashes the freshly server-hashed `venv/**`
  (`skip_prefixes`), while the committed-release reuse path still hashes all.
- Post-activation bookkeeping (state, release listing, audit append, batched
  GC `rm`) is one round trip.
- Commands >4 KiB are spooled to a private remote temp file (`sh <file>`);
  admin scripts use `run_script` (`sudo bash <file>`). This removes the
  libssh2 exec-channel stalls that made CI time out.
- Unchanged unit/dropin sets and unchanged Caddyfiles are not rewritten:
  `install_units` is skipped when `units_installed_from` matches the live
  digest and the manifest diff has no unit change; when some unit did change,
  only the changed unit/dropin entries are written (stale removal and the
  single `daemon-reload` still cover the full declared set).
  `install_caddyfile` short-circuits when the server content already matches;
  a main config restored from a leftover `.fujin-bak` is reloaded and the
  stale backup removed instead.
- `_converge` skips the instance-presence query when neither the previous nor
  the final manifest declares an `@.service` unit (`--force` still queries).
- `release_lock` is one round trip (kill group + holder, remove marker).
- The local build and the client manifest/digest are computed before the SSH
  connection and the remote lock; a build failure exits 4 with no SSH I/O.
  Concurrent local builds serialize on `.fujin/cache/.build.lock`.
- `rsync` and `conn.get()` share one OpenSSH ControlMaster
  (`ControlPath=/tmp/fujin-ssh-%C`, `ControlPersist=60s`).
- Integration suite: 14 tests, ~32–52 s with `-n 4` (was 27 tests, 15+ min and
  hanging). CI runs it serially (~2 min job) because four privileged
  systemd containers starved the runner. Harness: content-addressed image +
  flock, shared container for app-scoped tests with per-app teardown, fresh
  containers only for bootstrap/create-user/harden, no fixed sleeps.

Measured on the Docker test app (small dependency set): v1 full install
1.32 s, v2 venv clone 1.13 s. The clone/hash-reuse win grows with the number
of dependencies and files.

Still open (see §9).

Not yet measured end to end: `verify_staging` hashing cost on a real
multi-thousand-file venv (now only client entries), manifest JSON size/upload
(the merged manifest is still uploaded once via SCP), `uv pip install` cost
when requirements change, readiness fallback on slow starters.

## 6. Known limitations / risks

- A deploy credential that can install systemd units is effectively
  privileged; the scoped sudoers rule reduces blast radius, it does not
  contain a malicious deploy user (documented honestly in
  `docs/configuration.rst`).
- `fujin down` on the legacy `.pyz` layout no longer runs the bundled
  uninstaller as root; it warns with the manual admin step and continues the
  release-tree teardown.
- `record_rollback` stores the target release's fingerprint; `doctor` uses
  the manifest as truth (state is a cache).
- `fujin doctor` audit check only validates records that carry a digest
  (deploys/rollbacks from this batch onward); older records are a note.
- `config_fingerprint` includes env ref *names*; rotating a secret value
  changes the release digest but not the fingerprint.
- Cross-host guard fires only when host identity AND fingerprint both differ
  (so renames and normal config changes pass); it is a belt-and-braces check,
  not a security boundary.
- Dry-run cannot print the final digest without building (server-built
  `venv/**` entries); it prints the client digest + swap target and says so.
- Health-check probes run as the deploy user (no sudo) and are bounded per
  attempt by `min(5, timeout)`.

## 7. Verification

```bash
uv run pytest --ignore=tests/integration -q   # 700 passed, 1 skipped
uv run pytest plugins/fujin-secrets-bitwarden/tests -q
uv run fujin --help                            # exit-code summary
uv run fujin deploy --help                     # --dry-run
uv run fujin doctor --help
uv run fujin server harden --help
just test-integration                          # Docker; --timeout=180 per test
```

The one skipped unit test is the `visudo -c` syntax check (skips when
`visudo` is not installed locally).

## 8. Docs updated

- `docs/configuration.rst`: project/host env, template variables
  (incl. `{shared_dir}`), fingerprint + cross-host guard, readiness +
  `[health_check]`, scoped sudo (`deploy-user-sudo`).
- `docs/commands/deploy.rst`: flow, `--dry-run`, restart behaviour.
- `docs/commands/doctor.rst`, `docs/commands/exit-codes.rst` (new).
- `docs/commands/server.rst`: create-user scoped rule, `harden` migration.
- `docs/secrets.rst`, plugin READMEs: `$$`, folders, Vaultwarden.

## 9. Open work

- **Integration suite:** restructured and green locally (13 tests, ~32 s with
  `-n 4`, two consecutive runs). CI verification is in flight; the suite is
  no longer the bottleneck.
- **Transport:** large commands are spooled now; the remaining risk is the
  default 300 s idle deadline on genuinely silent long commands. Consider a
  shorter default plus per-command overrides.
- **Speedup items still open** (`docs/agents/deployment-speedup.md`):
  - item 2 **partial**: `verify_staging` no longer re-hashes `venv/**`, but
    the merged manifest is still uploaded via SCP (writing it server-side is
    not implemented).
  - item 4 **partial**: `acquire_lock` polls instead of sleeping, but the
    pre-flight handshake (`ensure_app_user` + `ensure_layout` +
    `acquire_lock` + `sweep_stale_staging` + state read) is still several
    round trips.
  - item 5 **partial**: the build runs before the SSH connection, but it is
    not parallelized with secret resolution.
  - item 6 **partial**: the build-cache-hit double read is fixed (verified
    bytes are reused), the local cache of committed manifests by `<digest>`
    is not implemented.
  - item 7 **partial**: post-rsync staging preparation is fused, but fewer
    staging round trips are still possible.
  Everything else — items 1, 3, 8, 9, 10, 11 — is implemented and
  unit-tested.
- A real-app measurement is still missing: the numbers above come from the
  Docker test app (a handful of dependencies). Profile `time fujin deploy` on
  a real Django project (hundreds of venv files) before/after to confirm the
  venv-clone and hash-reuse wins.
- The older open issues (pre-dating this rework) were not touched; some may
  be redundant now (`#67`, `#72`, …) and need a pass.
- `#95` follow-ups deliberately out of scope: attachments/SSH keys,
  one-item-per-app custom fields.
