# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Fujin is a deployment tool for getting projects running on a VPS. It manages app processes using systemd and runs apps behind Caddy reverse proxy. The tool supports both Python packages and self-contained binaries, providing automatic SSL certificates, secrets management, and rollback capabilities.

**Core Philosophy**: Automate deployment while leaving users in full control of their Linux box. It's not a CLI PaaS - users should be able to SSH into their server and troubleshoot.

## Development Commands

### Environment Setup
```bash
# Install dependencies
uv sync

# Run fujin in development mode
uv run fujin --help
```

### Testing
```bash
# Run unit tests (excludes integration tests)
just test
# Or: uv run pytest --ignore=tests/integration -sv

# Run integration tests (requires Docker)
just test-integration
# Or: uv run pytest tests/integration

# Run specific test
just test tests/test_config.py::test_name

# Update inline snapshots (uses inline-snapshot library)
just test-fix

# Review inline snapshot changes
just test-review
```

### Code Quality
```bash
# Format code (ruff + pyproject-fmt)
just fmt

# Type checking
just lint
# Or: uvx mypy .
```

### Documentation
```bash
# Serve docs with live reload
just docs-serve
# Or: uv run --group docs sphinx-autobuild docs docs/_build/html --port 8002 --watch src/fujin
```

### Example Projects
```bash
# Run uv commands in Django example
just djuv [ARGS]

# Generate Django requirements
just dj-requirements

# Run fujin in Django example context
just fujin [ARGS]

# Test with Vagrant VM
just recreate-vm
just ssh
```

### Release Management
```bash
# Bump version and generate changelog
just bumpver [major|minor|patch]

# Generate changelog only
just logchanges

# Build binary distribution (uses PyApp)
just build-bin
```

## Architecture

### Configuration System (`config.py`)

The `Config` class (msgspec struct) is the central configuration object, loaded from `fujin.toml` in the project root.

**Key components:**
- `Config`: Main configuration with app metadata, host config, and deployment settings
- `HostConfig`: SSH connection details, environment files, and deployment target info
- `SecretConfig`: Secret adapter configuration (adapter name, password env var)
- `InstallationMode`: Enum for `python-package` vs `binary` deployment

**Important Config fields:**
- `app_name`: Application name (used for systemd unit naming)
- `app_user`: System user to run the app as (defaults to app_name)
- `version`: App version (defaults to reading from `pyproject.toml`)
- `replicas`: Dict mapping service name to replica count (e.g., `{"worker": 3}`)
- `hosts`: List of `HostConfig` for deployment targets
- `local_config_dir`: Path to `.fujin/` directory (default)

**Important Config properties:**
- `app_dir`: Returns `/opt/fujin/{app_name}` (deployment location)
- `deployed_units`: List of `DeployedUnit` discovered from `.fujin/systemd/`
- `systemd_units`: All unit names that should be enabled/started
- `caddyfile_exists`: Whether `.fujin/Caddyfile` exists
- `caddy_config_path`: Remote Caddy config path (`/etc/caddy/conf.d/{app_name}.caddy`)

**Important behaviors:**
- Version defaults to reading from `pyproject.toml`
- Python version can be read from `.python-version` file if not specified
- Apps are always deployed to `/opt/fujin/{app_name}`
- Systemd unit names follow pattern: `{app_name}-{service}.service` for single replica, `{app_name}-{service}@.service` for multiple replicas

### Service Discovery (`discovery.py`)

The `DeployedUnit` class represents a discovered systemd service from the `.fujin/systemd/` directory.

**DeployedUnit fields:**
- `service_name`: Base name (e.g., "web", "worker")
- `is_template`: True if using `@.service` format for replicas
- `service_file`, `socket_file`, `timer_file`: Paths to unit files
- `template_service_name`: Full systemd name (e.g., `myapp-web@.service`)
- `replica_count`: Number of instances to run
- `instance_service_names`: List of instance names for operations

**Discovery process:**
- Scans `.fujin/systemd/` for `.service` files
- Detects template services (ending with `@`)
- Finds matching `.socket` and `.timer` files
- Generates instance names based on `config.replicas`

### Connection & SSH (`connection.py`)

`SSH2Connection` wraps ssh2-python for executing remote commands. Uses context manager pattern via `connection()` method.

**Key features:**
- Non-blocking I/O with select() for real-time output streaming
- PTY support for interactive sessions (password prompts, shells)
- Automatic sudo password handling via watchers
- UTF-8 incremental decoding to handle split characters across packets
- Directory context manager (`cd()`) for maintaining working directory state
- File upload via SCP (`put()` method)

**PATH handling**: Automatically prepends `~/.cargo/bin` and `~/.local/bin` to PATH to find tools like `uv`.

### Commands Structure (`commands/`)

All commands inherit from `BaseCommand` which provides `config`, `output`, and `connection()` properties. Uses Cappa for CLI parsing.

**Main commands:**
- `init`: Initialize fujin.toml and `.fujin/` directory structure for a new project
- `deploy`: Build → create zipapp → upload → execute installer (the core deployment workflow)
- `up`: Alias for `server bootstrap`
- `rollback`: Roll back to previous version using stored `.pyz` bundles
- `app`: Manage application (status, start/stop/restart, logs, shell, exec, scale)
- `server`: Server-level operations (bootstrap, create-user, setup-ssh, status, exec)
- `down`: Tear down project (stop services, remove files, delete app user)
- `prune`: Remove old version bundles (keeps N versions based on config)
- `new`: Create new systemd service, timer, or dropin files
- `migrate`: Migrate old fujin.toml format to file-based structure
- `audit`: View deployment audit log

**Command pattern**: Each command is a Cappa command class that implements `__call__()`.

### Secrets Management (`secrets.py`)

Supports fetching secrets from external sources during deployment. Environment variables prefixed with `$` trigger secret resolution.

**Adapters:**
- `system`: Read from local environment variables (default)
- `bitwarden`: Bitwarden CLI (`bw get password`)
- `1password`: 1Password CLI (`op read`)
- `doppler`: Doppler CLI (`doppler secrets get`)

Secrets are resolved concurrently using ThreadPoolExecutor for performance.

### Template System (`templates.py`)

Templates are Python string constants used by the `new` and `init` commands to generate files:
- `NEW_SERVICE_TEMPLATE`: Systemd service unit template
- `NEW_TIMER_SERVICE_TEMPLATE`: Systemd service for timer-triggered tasks
- `NEW_TIMER_TEMPLATE`: Systemd timer configuration
- `NEW_DROPIN_TEMPLATE`: Systemd dropin for overrides/extensions
- `CADDYFILE_TEMPLATE`: Simple Caddyfile template

Templates use `{variable}` placeholders for Python string formatting and `{{variable}}` for values resolved at deploy time.

### Deployment Flow

1. **Build** (`deploy.py`): Run build_command locally
2. **Resolve secrets**: Parse env file and fetch secrets from configured adapter
3. **Create bundle** (in temp directory):
   - Copy distfile and requirements.txt
   - Create `.env` with resolved variables
   - Copy systemd units from `.fujin/systemd/`
   - Copy dropins (common.d/ and service-specific)
   - Copy Caddyfile if exists
   - Create `config.json` with deployment metadata
4. **Create zipapp**: Bundle everything with `_installer/__main__.py` into `.pyz` file
5. **Upload**: SCP zipapp to `/opt/fujin/{app_name}/.versions/{app_name}-{version}.pyz`
6. **Verify**: SHA256 checksum verification
7. **Execute**: Run `python3 installer.pyz install` on server
8. **Prune**: Remove old versions (keeps `versions_to_keep` most recent)

**Installer actions** (`_installer/__main__.py`):
- Create app user if needed
- Set up `/opt/fujin/{app_name}` directory
- Install Python package (via uv) or copy binary
- Create `.appenv` shell script for environment setup
- Install systemd units and dropins
- Enable and start services
- Configure Caddy if enabled

### Testing

- Uses pytest with inline-snapshot for snapshot testing
- `pytest-subprocess` for mocking subprocess calls
- Tests in `tests/` directory, integration tests in `tests/integration/`
- Integration tests use Docker with real systemd

### Tools & Dependencies

- **UV**: Fast Python package installer (used for dependency management)
- **Cappa**: Modern CLI framework (replaces argparse/click)
- **msgspec**: Fast serialization library for config parsing (TOML support)
- **ssh2-python**: Python bindings for libssh2 (lower-level than paramiko)
- **Rich**: Terminal formatting and output
- **tomli-w**: TOML writing for config updates

## Common Patterns

### Adding a new command

1. Create file in `src/fujin/commands/new_command.py`
2. Inherit from `BaseCommand` or use standalone `@cappa.command`
3. Add to imports and `Fujin.subcommands` union in `__main__.py`
4. Use `self.config` for configuration access
5. Use `self.output.output()` for user-facing output (supports Rich markup)
6. Use `with self.connection() as conn:` for SSH operations

### Working with configuration

```python
# Access config
self.config.app_name
self.config.app_dir  # property, returns /opt/fujin/{app_name}
self.config.replicas  # dict of service -> replica count

# Access discovered units
for du in self.config.deployed_units:
    print(du.service_name, du.is_template, du.replica_count)

# Get all systemd unit names
units = self.config.systemd_units
```

### Remote command execution

```python
with self.connection() as conn:
    # Simple command
    stdout, success = conn.run("ls -la")

    # With directory context
    with conn.cd("/path/to/dir"):
        conn.run("pwd")  # runs in /path/to/dir

    # Upload file
    conn.put("local/file.txt", "remote/file.txt")

    # Interactive (PTY)
    conn.run("bash", pty=True)
```

### Output formatting

```python
self.output.success("Operation completed!")  # green
self.output.error("Something failed")        # red
self.output.warning("Be careful")            # yellow
self.output.info("FYI...")                   # blue
self.output.output("Plain text")             # no color
```

## Project-Specific Notes

- **Versioning**: Uses bump-my-version, updates both pyproject.toml and src/fujin/__init__.py
- **Changelog**: Generated via git-cliff using conventional commits
- **Workspace**: UV workspace includes `examples/django/bookstore`
- **Vagrant**: Vagrantfile provided for local testing with VM
- **PyApp**: Can build standalone binary with `just build-bin`
- **Ruff config**: Requires `from __future__ import annotations` import in all files

## Systemd Security Directives

Fujin uses systemd security directives to harden services. Apps are deployed to `/opt/fujin/{app_name}`.

### Recommended Security Configuration

```ini
[Service]
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/fujin/{app_name}
UMask=0002  # Create files/sockets as group-writable
```

### Permission Model

**App Services:**
- Run as `{app_user}:{app_user}` (defaults to `{app_name}`)
- RuntimeDirectory (`/run/{app_name}/`) created with mode `0755`
- `UMask=0002` ensures sockets/files are group-writable (e.g., `0775` for sockets)

**Caddy Integration:**
- Caddy runs as `caddy:caddy`
- During deployment, `caddy` user is added to `{app_user}` group
- This allows Caddy to connect to Unix sockets in `/run/{app_name}/`
- Command: `sudo usermod -aG {app_user} caddy` (non-fatal if caddy user doesn't exist)

**File Ownership:**
- `/opt/fujin/{app_name}`: owned by `{deploy_user}:{app_user}`, mode `775`
- `.install/` directory: owned by `{deploy_user}:{app_user}`, mode `775`
- `.install/.env`: mode `640` (readable by group, not world)

### Why These Settings?

- `ProtectHome=true`: Home directories inaccessible (safe since apps are in /opt)
- `ProtectSystem=strict`: Most of filesystem read-only
- `ReadWritePaths`: Grant write access to app directory for logs, database, etc.
- `UMask=0002`: Sockets/files group-writable so Caddy (in app group) can access them

### Debugging Permission Issues

Common exit codes:
- **203/EXEC**: Executable not found or not accessible (check file permissions)
- **226**: Namespace/cgroup setup failed (usually ProtectSystem incompatibility)

Test manually:
```bash
# Check if binary is accessible
ls -la /opt/fujin/myapp/.venv/bin/myapp

# Test with systemd restrictions
sudo systemd-run --pty \
  --property=ProtectSystem=strict \
  --property=ProtectHome=true \
  --property=ReadWritePaths=/opt/fujin/myapp \
  --property=User=myapp \
  /opt/fujin/myapp/.venv/bin/myapp --version
```

## Alias System

Fujin supports command aliases defined in `fujin.toml`:

```toml
[aliases]
shell = "server exec --appenv bash"
logs = "app logs"
```

Parsed in `__main__.py:_parse_aliases()` and expands before command invocation.

## Testing Principles

### Test Structure

Tests are organized in `tests/` with two categories:

**Unit Tests** (`tests/test_*.py`):
- Fast tests with mocked dependencies
- Cover error handling, user interaction, and pure logic
- No external dependencies (SSH, Docker)
- Examples: `test_config.py`, `test_app.py`, `test_rollback.py`

**Integration Tests** (`tests/integration/`):
- Docker-based tests with real systemd/SSH
- Verify end-to-end behavior on a simulated VPS
- Require Docker to run
- Test files:
  - `test_full_deploy.py` - Deployment lifecycle (deploy, rollback, down)
  - `test_installation.py` - Systemd units (sockets, timers, dropins)
  - `test_server_bootstrap.py` - Server setup and user creation
  - `test_app_management.py` - App commands (restart, logs, status)
  - `helpers.py` - Shared assertion utilities

### Core Principles

**Prefer Integration Tests for Command Behavior**
- Integration tests verify actual system behavior
- Unit tests focus on error handling and pure logic
- Avoid brittle mock chains that just verify command strings

**Keep Unit Tests Focused**
- Test error handling and edge cases
- Test pure logic functions (name resolution, formatting)
- Test user interaction (keyboard interrupt, confirmation decline)

**Shared Fixtures** (`tests/conftest.py`):
- `minimal_config_dict` - Base configuration dict
- `minimal_config` - Config object from dict
- `mock_connection` - Mocked SSH connection
- `mock_output` - Mocked output handler

**Integration Test Helpers** (`tests/integration/helpers.py`):
- `exec_in_container()` - Run command in Docker container
- `wait_for_service()` - Wait for systemd service with retries
- `assert_service_running()` - Verify service is active
- `assert_file_exists()` / `assert_file_contains()` - File assertions

### Running Tests

```bash
# Run unit tests (fast, no Docker needed)
just test

# Run integration tests (requires Docker)
just test-integration

# Run specific test file
just test tests/test_config.py

# Update inline snapshots
just test-fix
```
