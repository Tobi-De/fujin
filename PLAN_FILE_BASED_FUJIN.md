# Plan: File-Based Fujin

## Overview
Transform fujin from template-based to file-based configuration. Users write actual systemd units and Caddyfile directly in `.fujin/` directory. Fujin discovers services from files, performs minimal variable substitution, and orchestrates deployment.

## Quick Summary

**Key Changes:**
- `.fujin/systemd/*.service` files replace `[processes]` in fujin.toml
- `.fujin/Caddyfile` replaces `[sites]` in fujin.toml
- Local files use simple names (`web@.service`), deployed with app prefix (`myapp-web@.service`)
- ExecStartPre replaces `release_command`
- Common drop-ins in `common.d/` for DRY configuration
- Minimal variable substitution: `{app_name}`, `{version}`, `{app_dir}`, `{user}`
- Zipapp deployment continues unchanged
- Each version carries its own `.fujin/` directory for rollback

**Key Decisions:**
- ✅ Strict error handling (fail on malformed files)
- ✅ No auto-open editor for `fujin new` commands
- ✅ Remove `fujin app cat` and `fujin templates eject`
- ✅ `fujin scale` updates both toml and server (single host)
- ✅ `fujin new service` supports `--socket` flag
- ✅ Rename `keep_releases` → `max_releases`

## 1. Configuration Changes

### fujin.toml - What Gets Removed
- `processes` section (replaced by actual systemd units in `.fujin/systemd/`)
- `sites` section (replaced by `.fujin/Caddyfile`)
- `webserver` section (no longer needed)
- `release_command` (replaced by ExecStartPre in units)

### fujin.toml - What Stays
- `app`
- `version`
- `build_command`
- `distfile`
- `requirements`
- `python_version`
- `installation_mode`
- `hosts` array
- `aliases`
- `keep_releases` → rename to `max_releases` (clearer meaning)

### fujin.toml - What Gets Added
- `[replicas]` - map of service name to replica count
  ```toml
  [replicas]
  web = 3
  api = 2
  ```

### New Directory Structure
```
.fujin/
  Caddyfile                    # Single file at root of .fujin/
  systemd/
    web@.service         # @ = template unit (supports replicas)
    worker.service       # regular unit
    cleanup.service      # oneshot service
    cleanup.timer        # systemd timer
    common.d/                  # Convention: always use this name
      base.conf                # Applied to all services
      security.conf
    web@.service.d/      # Service-specific drop-ins
      resources.conf
```

## 2. Variable Substitution

### Minimal Set of Variables
Only substitute these in `.fujin/` files:
- `{app_name}` - from fujin.toml `app`
- `{version}` - from fujin.toml or pyproject.toml
- `{app_dir}` - deployment path (e.g., `/home/user/apps/myapp/v1.0.0`)
- `{user}` - from host config

### Remove Entirely
- All Jinja2 templating complexity
- Template search paths (.fujin/ then src/fujin/templates/)
- Template overrides system
- Complex template logic

## 3. Service Discovery

### How Fujin Discovers Services
1. Scan `.fujin/systemd/*.service` files
2. Parse filename to extract:
   - Service name (e.g., `web@.service` → "web")
   - Whether templated (has `@` → supports replicas)
3. Check `[replicas]` in fujin.toml for replica count
4. Auto-discover `.fujin/systemd/common.d/` drop-ins (by convention, no config needed)
5. Auto-discover service-specific drop-ins in `.fujin/systemd/SERVICENAME.service.d/`

**Important:** Files in `.fujin/systemd/` use simple names (`web@.service`), but fujin adds the app name prefix when deploying to `/etc/systemd/system/` (e.g., `myapp-web@.service`). This keeps local files clean while avoiding naming conflicts on the server.

**Error handling:** File discovery uses strict validation. If any `.service` file is malformed or has invalid syntax, fujin will fail with a clear error message. No silent skipping of broken files.

### How Fujin Discovers Webserver Config
- Check if `.fujin/Caddyfile` exists
- If yes, process variables and deploy
- If no, skip webserver setup

### How Fujin Discovers Timers and Sockets
- Check for `.timer` files alongside `.service` files
- Check for `.socket` files alongside `.service` files
- Deploy and enable appropriately

## 4. Drop-in Management

### Common Drop-ins (Convention Over Configuration)
- Always use `.fujin/systemd/common.d/` directory name
- `fujin init` creates this directory with examples
- Auto-discovered and applied to ALL services during deployment
- No configuration needed in fujin.toml

### Service-specific Drop-ins
- Support `.fujin/systemd/SERVICENAME.service.d/*.conf`
- Applied only to that specific service
- Useful for resource limits, additional security, etc.

## 5. Command Changes

### Commands That Need Modification

#### `fujin init [--profile PROFILE]`
**Changes:**
- Generates minimal `fujin.toml` (no processes/sites)
- Creates `.fujin/systemd/` directory
- Creates `.fujin/systemd/common.d/` with base drop-ins
- Creates `.fujin/Caddyfile` with example
- Generates example service files based on profile

**Profiles:**
- `simple` - basic web service
- `django` - web + worker, migrations in ExecStartPre
- `binary` - single binary deployment
- `falco` - falco-specific setup

#### `fujin deploy`
**Changes:**
- Auto-discovers services from `.fujin/systemd/*.service` files
- Auto-discovers drop-ins from `common.d/` and service-specific `.d/` directories
- Processes all files with minimal variable substitution
- Deploys systemd units to `/etc/systemd/system/`
- Deploys drop-ins to `/etc/systemd/system/UNIT.service.d/`
- Deploys Caddyfile if exists
- Enables/starts services based on discovered units and replica counts

#### `fujin migrate [--dry-run] [--backup]`
**Changes:**
- Reads old-format fujin.toml
- Generates `.fujin/systemd/*.service` files from `processes` section (without app prefix)
- Generates `.fujin/Caddyfile` from `sites` section
- Generates `.fujin/systemd/common.d/base.conf` with common settings
- Converts `release_command` to ExecStartPre in appropriate unit file
- Removes old sections from fujin.toml (`processes`, `sites`, `webserver`, `release_command`)
- Adds `[replicas]` section if processes had replicas
- Renames `keep_releases` to `max_releases`
- Converts route "/" to "/*" in generated Caddyfile
- Migrates aliases (app exec → exec --app, server exec → exec, remove -i)

**Keeps existing options:**
- `--dry-run` - show what would change without writing
- `--backup` - create backup of fujin.toml

**No longer:**
- Shows diff and asks for confirmation (--dry-run handles preview)

#### `fujin show [NAME]`
**Keeps current behavior:**
- Shows rendered/generated configs with variables substituted
- `fujin show` - shows available options
- `fujin show units` - shows all systemd units
- `fujin show caddy` - shows Caddyfile
- `fujin show env` - shows environment variables
- `fujin show SERVICE` - shows specific unit

**Purpose:**
Preview what will be deployed (processed files with variables substituted)

#### `fujin rollback [VERSION]`
**Changes:**
- Discovers services from previous version's `.fujin/systemd/` directory
- Updates current symlink
- Restarts discovered services

#### `fujin app info [SERVICE]`
**Changes when called without SERVICE argument:**
Shows overview of all services:
```
Services:
  web (3 replicas) - web@.service → myapp-web@.service (deployed)
    ├─ myapp-web@1: running
    ├─ myapp-web@2: running
    └─ myapp-web@3: running

  worker - worker.service → myapp-worker.service (deployed)
    └─ myapp-worker: running

  cleanup - cleanup.service → myapp-cleanup.service (deployed, oneshot)
    └─ myapp-cleanup: inactive

Timers:
  cleanup - cleanup.timer → myapp-cleanup.timer (deployed)
    └─ myapp-cleanup.timer: active (next: tomorrow 00:00)

Webserver:
  Caddyfile: deployed
  Caddy: running
```

**Changes when called with SERVICE argument:**
Shows detailed info for that service:
```
Service: web
Source: web@.service
Deployed as: myapp-web@.service
Replicas: 3
Status:
  myapp-web@1: running (since 2 days ago)
  myapp-web@2: running (since 2 days ago)
  myapp-web@3: running (since 2 days ago)
Drop-ins:
  - common.d/base.conf
  - common.d/security.conf
```

### New Commands

#### `fujin new service NAME [--replicas N] [--socket]`
**Purpose:** Generate new service file
**Behavior:**
- Creates `.fujin/systemd/NAME.service`
- If `--replicas N`: creates `NAME@.service` (templated unit) and adds to `[replicas]` section
- If `--socket`: also creates `NAME.socket` for socket activation
- Generates from template with common patterns
- Does NOT auto-open editor (user edits manually)

**Examples:**
```bash
fujin new service email-worker
fujin new service api --replicas 2
fujin new service web --replicas 3 --socket
```

#### `fujin new timer NAME [--daily|--weekly|--hourly|--schedule SPEC]`
**Purpose:** Generate systemd timer with corresponding service
**Behavior:**
- Creates both `.fujin/systemd/NAME.service` (oneshot) and `NAME.timer`
- Pre-configures timer based on flags
- Does NOT auto-open editor (user edits manually)

**Examples:**
```bash
fujin new timer cleanup --daily
fujin new timer backup --schedule "Mon *-*-* 03:00:00"
```

#### `fujin new dropin NAME [--common|--service SERVICE]`
**Purpose:** Generate drop-in configuration file
**Behavior:**
- If `--common`: creates `.fujin/systemd/common.d/NAME.conf`
- If `--service SERVICE`: creates `.fujin/systemd/SERVICE.service.d/NAME.conf`
- Provides template with common patterns
- Does NOT auto-open editor (user edits manually)

**Examples:**
```bash
fujin new dropin security --common
fujin new dropin resources --service web
```

#### `fujin scale SERVICE=N`
**Purpose:** Scale replicated service to N instances
**Behavior:**
1. Updates `[replicas]` section in fujin.toml
2. Connects to server
3. If N > current: starts new instances (e.g., `systemctl start myapp-web@4`)
4. If N < current: stops excess instances (e.g., `systemctl stop myapp-web@5`)
5. If N == current: shows "already at desired scale"
6. Only operates on selected host (no multi-host scaling)

**Examples:**
```bash
fujin scale web=5    # Scale web service to 5 replicas
fujin scale api=1    # Scale down api to 1 replica
```

## 5.1. Deployment Artifacts & Zipapp

### How Deployment Works with Zipapp
Fujin continues to use Python zipapp (.pyz) for deployments. The `.fujin/` directory structure is bundled alongside the zipapp.

**Build phase:**
1. Run `build_command` (e.g., `uv build`)
2. Creates zipapp: `dist/myapp-{version}.pyz`
3. Bundle includes:
   - The .pyz file
   - `.fujin/systemd/` directory with all unit files and drop-ins
   - `.fujin/Caddyfile` if exists
   - Environment file
   - `.version` file

**Deploy phase:**
1. Upload bundle to server
2. Extract to versioned directory: `~/apps/myapp/v1.0.0/`
3. Discover services from `.fujin/systemd/*.service`
4. Process files (variable substitution)
5. Deploy to `/etc/systemd/system/` with app prefix (e.g., `web@.service` → `myapp-web@.service`)
6. Deploy Caddyfile if exists
7. Update `current` symlink → `v1.0.0`
8. Enable and start services

**Rollback:**
1. List available versions from `~/apps/myapp/.versions/`
2. Each version has its own `.fujin/` directory structure
3. Discover services from previous version's `.fujin/systemd/`
4. Update `current` symlink to previous version
5. Restart services (systemd units already deployed)

**Key point:** Each deployment version carries its own `.fujin/` directory, so rollback automatically uses the correct unit files for that version.

### Commands That Stay The Same

These commands require no changes:
- `fujin up` - server bootstrap
- `fujin down` - stop and disable services
- `fujin prune` - remove old releases
- `fujin exec [--app]` - execute commands
- `fujin app logs [SERVICE]` - view logs
- `fujin app start [SERVICE]` - start services
- `fujin app restart [SERVICE]` - restart services
- `fujin app stop [SERVICE]` - stop services
- `fujin app shell [COMMAND]` - interactive shell
- `fujin server info` - server system info
- `fujin server bootstrap` - setup server
- `fujin server create-user` - create user
- `fujin server setup-ssh` - SSH key setup
- `fujin audit` - view audit logs

### Commands That Get Removed

#### `fujin app cat`
**Reason:** Redundant with `fujin show` which already shows unit contents

#### `fujin templates eject`
**Reason:**
- In file-based model, there are no package templates to eject
- `fujin new` commands handle file generation
- Users directly edit `.fujin/` files instead of ejecting templates

## 6. Migration Strategy

### No Backwards Compatibility
- Clean break from old format
- `fujin migrate` command converts old → new
- Clear migration guide in documentation
- No attempt to support both formats simultaneously

### Migration Process
User runs: `fujin migrate [--dry-run] [--backup]`

Fujin:
1. Reads old fujin.toml (processes, sites, webserver, etc.)
2. Generates `.fujin/systemd/` directory structure
3. Generates one `.service` file per process
4. Generates `common.d/base.conf` with User, WorkingDirectory, etc.
5. Generates `.fujin/Caddyfile` from sites config
6. Converts process socket=true to `.socket` files
7. Converts release_command to ExecStartPre in appropriate service
8. Updates fujin.toml (removes old sections, adds [replicas])
9. If `--backup`: creates fujin.toml.backup
10. If `--dry-run`: shows changes without writing

### What Migration Generates

**From processes:**
```toml
# OLD
[processes.web]
command = "gunicorn app:app"
listen = "localhost:8000"
replicas = 3

[processes.worker]
command = "celery -A app worker"
```

**To:**
```
.fujin/systemd/web@.service
.fujin/systemd/worker.service
.fujin/systemd/common.d/base.conf
```

**From sites:**
```toml
# OLD
[[sites]]
domains = ["example.com"]
routes = { "/": "web", "/static/*" = { static = "/var/www/static" } }
```

**To:**
```
.fujin/Caddyfile
```

**From release_command:**
```toml
# OLD
release_command = "python manage.py migrate"
```

**To ExecStartPre in web service:**
```ini
[Service]
ExecStartPre=/bin/bash -c '[ "%i" = "1" ] && {app_dir}/.venv/bin/python manage.py migrate || true'
```

## 7. Systemd Concepts To Document

### ExecStartPre
- Runs commands before main service starts
- Used for migrations, setup, health checks
- Multiple ExecStartPre directives run in order
- Service doesn't start if any fail

**Example:**
```ini
[Service]
ExecStartPre={app_dir}/.venv/bin/python manage.py migrate
ExecStartPre=/usr/bin/mkdir -p /run/{app_name}
ExecStart={app_dir}/.venv/bin/gunicorn app:app
```

### Drop-in Directories
- Extend units without editing main file
- Common drop-ins apply to all services
- Service-specific drop-ins for customization
- DRY principle for shared configuration

**Example:**
```
common.d/base.conf applied to all services:
[Service]
User={user}
WorkingDirectory={app_dir}
```

### Template Units (@)
- Unit files with `@` support multiple instances
- `web@.service` in `.fujin/systemd/` gets deployed as `myapp-web@.service`
- When enabled: `myapp-web@1`, `myapp-web@2`, etc.
- `%i` in unit file = instance identifier
- Enables easy scaling

**Example:**
```ini
# web@.service (in .fujin/systemd/)
[Service]
ExecStart=gunicorn app:app --bind unix:/run/app/app-%i.sock
```

### Systemd Timers
- Alternative to cron
- More robust scheduling
- Better logging and error handling
- Persistent across reboots

**Example:**
```ini
# cleanup.timer (in .fujin/systemd/)
[Timer]
OnCalendar=daily
Persistent=true
```

## 8. Caddyfile Handling

### Single File Approach
- `.fujin/Caddyfile` at root of .fujin directory
- Not in subdirectory
- Processed with same variable substitution as systemd units
- Deployed to server's Caddy config directory

### Replica Awareness
User writes Caddyfile to handle replicas:
```
{app_name}.com {
    # Wildcard matches all instances
    reverse_proxy unix//run/{app_name}/{app_name}-*.sock {
        lb_policy least_conn
        health_uri /health
    }

    # Or list specific instances
    reverse_proxy unix//run/{app_name}/{app_name}-1.sock \
                  unix//run/{app_name}/{app_name}-2.sock \
                  unix//run/{app_name}/{app_name}-3.sock
}
```

## 9. Profile Templates

### Simple Profile
```
.fujin/
  systemd/
    web@.service      # Gunicorn with socket
    common.d/
      base.conf             # User, WorkingDirectory, etc.
  Caddyfile                 # Basic reverse proxy
```

### Django Profile
```
.fujin/
  systemd/
    web@.service      # With migrate in ExecStartPre
    worker.service    # Celery worker
    common.d/
      base.conf
      security.conf         # NoNewPrivileges, PrivateTmp, etc.
  Caddyfile                 # With /static/* handling
```

### Binary Profile
```
.fujin/
  systemd/
    web@.service      # Single binary
    common.d/
      base.conf
  Caddyfile
```

### Falco Profile
```
.fujin/
  systemd/
    web@.service
    worker.service
    common.d/
      base.conf
  Caddyfile
```

## 10. Open Questions & Decisions

### Resolved
1. ✅ **Migrate confirmation:** Use --dry-run and --backup options, no interactive confirmation
2. ✅ **fujin app cat:** Remove it (redundant with fujin show)
3. ✅ **fujin show:** Keep and repurpose for showing generated configs (already does this)
4. ✅ **fujin scale:** Update fujin.toml AND run systemctl commands to actually scale
5. ✅ **Validation:** Skip for now (let systemd/caddy handle errors)
6. ✅ **Caddyfile:** Single file at .fujin/Caddyfile
7. ✅ **Command name:** Use `fujin new`
8. ✅ **fujin app info:** Show overview when no service specified
9. ✅ **common.d:** Use convention (always this directory name), no config needed

### Decided
1. ✅ **fujin templates eject command:** Remove entirely
2. ✅ **Default editor behavior:** No auto-open (user manually edits generated files)
3. ✅ **Error handling:** Fail on malformed files (strict validation during discovery)
4. ✅ **Socket file generation:** Yes, add `--socket` flag to `fujin new service`
5. ✅ **Multi-host scaling:** No, `fujin scale` operates on selected host only

## 11. Implementation Phases

### Phase 1: Core Infrastructure
- Variable substitution system (minimal set: app_name, version, app_dir, user)
- Service discovery from filesystem with strict validation (fail on malformed files)
- Drop-in discovery and deployment (common.d/ convention)
- Update deploy command to:
  - Discover services from `.fujin/systemd/`
  - Add app prefix when deploying (web@.service → myapp-web@.service)
  - Bundle .fujin/ directory with zipapp
  - Deploy systemd units and Caddyfile

### Phase 2: Migration
- Implement `fujin migrate` command with --dry-run and --backup
- Convert processes → service files (without app prefix)
- Convert sites → Caddyfile
- Convert release_command → ExecStartPre
- Rename keep_releases → max_releases
- Generate common.d/base.conf
- Test migration on example projects (django, simple, binary, falco)

### Phase 3: New Commands
- `fujin new service [--replicas N] [--socket]` (no auto-open editor)
- `fujin new timer [--daily|--weekly|--hourly|--schedule]` (no auto-open editor)
- `fujin new dropin [--common|--service]` (no auto-open editor)
- `fujin scale SERVICE=N` (single host only, updates toml + systemctl)

### Phase 4: Init & Templates
- Update `fujin init` for all profiles (simple, django, binary, falco)
- Create example `.fujin/systemd/*.service` files for each profile
- Create example `.fujin/systemd/common.d/*.conf` files
- Create example `.fujin/Caddyfile` for each profile
- No app prefix in generated files

### Phase 5: Info & Display
- Enhance `fujin app info` with overview (show all services when no arg)
- Show both source and deployed names (web@.service → myapp-web@.service)
- Update `fujin show` to work with new file discovery
- Remove `fujin app cat`
- Remove `fujin templates eject`

### Phase 6: Documentation
- Update all documentation
- Create migration guide
- Document systemd concepts
- Add cookbook examples

## 12. Success Criteria

- Users can edit actual systemd units without fighting abstraction
- Full systemd feature availability (ExecStartPre, drop-ins, timers, etc.)
- Clear mental model: files in .fujin/ = what gets deployed
- Easy migration path from old format
- Reduced fujin complexity (less code, less config schema)
- Better alignment with "full control of your Linux box" philosophy
