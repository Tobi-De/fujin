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

# Run integration tests (requires VM)
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
- `Config`: Main configuration with app metadata, processes, host config, and webserver settings
- `ProcessConfig`: Defines how each process runs (command, replicas, socket/timer options)
- `TimerConfig`: Systemd timer configuration (on_calendar, on_boot_sec, on_unit_active_sec, persistent, etc.)
- `HostConfig`: SSH connection details, environment files, and deployment target info
- `Webserver`: Caddy reverse proxy configuration (upstream, statics, config directory)
- `InstallationMode`: Enum for `python-package` vs `binary` deployment

**Important behaviors:**
- Version defaults to reading from `pyproject.toml`
- Python version can be read from `.python-version` file if not specified
- Template rendering uses Jinja2 with search paths: `.fujin/` (local overrides) then `src/fujin/templates/` (defaults)
- Systemd unit names follow pattern: `{app_name}.service` for single replica, `{app_name}@.service` for multiple replicas
- The config validates that a `web` process exists if webserver is enabled

### Connection & SSH (`connection.py`)

`SSH2Connection` wraps ssh2-python for executing remote commands. Uses context manager pattern via `connection()` function.

**Key features:**
- Non-blocking I/O with select() for real-time output streaming
- PTY support for interactive sessions (password prompts, shells)
- Automatic sudo password handling via watchers
- UTF-8 incremental decoding to handle split characters across packets
- Directory context manager (`cd()`) for maintaining working directory state
- File upload via SCP (`put()` method)

**PATH handling**: Automatically prepends `~/.cargo/bin` and `~/.local/bin` to PATH to find tools like `uv`.

### Commands Structure (`commands/`)

All commands inherit from `BaseCommand` which provides `config`, `stdout`, and `connection` properties. Uses Cappa for CLI parsing.

**Main commands:**
- `deploy`: Build → transfer → install → configure services (the core deployment workflow)
- `init`: Initialize fujin.toml configuration for a new project
- `up`: Bootstrap server (install system deps, caddy, etc.)
- `rollback`: Roll back to previous version by symlinking and restarting services
- `app`: Manage application (exec commands in app context, logs, restart)
- `server`: Server-level operations (exec, install caddy)
- `config`: Show/print caddy config, test config, reload caddy
- `down`: Stop and disable services
- `prune`: Remove old releases (keeps N versions based on config)
- `printenv`: Show resolved environment variables (useful for debugging secrets)

**Command pattern**: Each command is a Cappa command class that implements `__call__()`.

### Secrets Management (`secrets.py`)

Supports fetching secrets from external sources during deployment. Environment variables prefixed with `$` trigger secret resolution.

**Adapters:**
- `system`: Read from local environment variables
- `bitwarden`: Bitwarden CLI (`bw get password`)
- `1password`: 1Password CLI (`op read`)
- `doppler`: Doppler CLI (`doppler secrets get`)

Secrets are resolved concurrently using ThreadPoolExecutor for performance.

### Template System

**Jinja2 templates** in `src/fujin/templates/`:
- `install.sh.j2`: Deployment script (uploaded and executed on remote)
- `uninstall.sh.j2`: Cleanup script for removing deployments
- `default.service.j2`: Systemd service unit template
- `web.service.j2`: Special template for web processes
- `default.socket.j2`: Systemd socket activation template
- `default.timer.j2`: Systemd timer template
- `Caddyfile.j2`: Caddy reverse proxy configuration

**Template overrides**: Users can place custom templates in `.fujin/` directory to override defaults.

### Deployment Flow

1. **Build** (`deploy.py`): Run build_command locally
2. **Resolve secrets**: Parse env file and fetch secrets from configured adapter
3. **Bundle**: Create tarball with distfile, requirements.txt, .env, systemd units, install script
4. **Transfer**: Upload bundle to server via SCP
5. **Install** (`install.sh.j2`):
   - Extract to versioned directory (e.g., `~/apps/myapp/v1.0.0/`)
   - Install Python/dependencies or binary
   - Copy systemd units
   - Symlink current → version directory
   - Reload systemd, restart services
6. **Configure Caddy**: Upload Caddyfile and reload if webserver enabled

### Testing

- Uses pytest with inline-snapshot for snapshot testing
- `pytest-subprocess` for mocking subprocess calls
- Tests in `tests/` directory, integration tests in `tests/integration/`
- Markers: `@pytest.mark.use_recorder` for mock recording

### Tools & Dependencies

- **UV**: Fast Python package installer (used for dependency management)
- **Cappa**: Modern CLI framework (replaces argparse/click)
- **msgspec**: Fast serialization library for config parsing (TOML support)
- **ssh2-python**: Python bindings for libssh2 (lower-level than paramiko)
- **Rich**: Terminal formatting and output
- **Jinja2**: Template rendering

## Common Patterns

### Adding a new command

1. Create file in `src/fujin/commands/new_command.py`
2. Inherit from `BaseCommand` or use standalone `@cappa.command`
3. Add to imports and `Fujin.subcommands` union in `__main__.py`
4. Use `self.config` for configuration access
5. Use `self.stdout.output()` for user-facing output (supports Rich markup)
6. Use `with self.connection as conn:` for SSH operations

### Working with configuration

```python
# Access config
self.config.app_name
self.config.processes["web"]
self.config.host.domain_name

# Render templates
units, user_units = self.config.render_systemd_units()
caddyfile = self.config.render_caddyfile()

# Build context for install script
context = self.config.build_context(
    distfile_name="app.whl",
    user_units=user_units,
    new_units=units
)
```

### Remote command execution

```python
with self.connection as conn:
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

## Project-Specific Notes

- **Versioning**: Uses bump-my-version, updates both pyproject.toml and src/fujin/__init__.py
- **Changelog**: Generated via git-cliff using conventional commits
- **Workspace**: UV workspace includes `examples/django/bookstore`
- **Vagrant**: Vagrantfile provided for local testing with VM
- **PyApp**: Can build standalone binary with `just build-bin`
- **Ruff config**: Requires `from __future__ import annotations` import in all files

## Systemd Security Directives

Fujin uses systemd security directives to harden services. **IMPORTANT**: When apps are deployed to `/home/{user}/.local/share/fujin`, you MUST use `ProtectHome=read-only` instead of `ProtectHome=true`.

### Correct Security Configuration

For apps in `/home/*`:
```ini
[Service]
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only  # NOT "true" - apps need to read from /home
ReadWritePaths={app_dir}
ReadWritePaths={app_dir}/.venv  # Python needs to write bytecode
```

### Why These Settings?

- `ProtectHome=true` makes `/home` completely inaccessible (exit code 203/EXEC)
- `ProtectHome=read-only` allows reading app code/venv but prevents writes
- `ProtectSystem=strict` makes most of filesystem read-only
- `ReadWritePaths={app_dir}/.venv` allows Python to write `.pyc` bytecode files
- Without `.venv` write access, Python can't compile bytecode (exit code 203/EXEC)

### Alternative: System-Wide Installation

If using `/opt/fujin` instead of `/home`:
- Can use `ProtectHome=true` (home dirs not needed)
- Still need `ReadWritePaths={app_dir}/.venv` for Python bytecode
- Cleaner separation but requires sudo for directory creation

### Debugging Permission Issues

Common exit codes:
- **203/EXEC**: Executable not found or not accessible (check ProtectHome, ProtectSystem, file permissions)
- **226**: Namespace/cgroup setup failed (usually ProtectSystem incompatibility)

Test manually:
```bash
# Check if binary is accessible
ls -la /path/to/app/.venv/bin/app

# Test with systemd restrictions
sudo systemd-run --pty \
  --property=ProtectSystem=strict \
  --property=ProtectHome=read-only \
  --property=ReadWritePaths=/path/to/app \
  --property=ReadWritePaths=/path/to/app/.venv \
  --property=User=username \
  /path/to/app/.venv/bin/app --version
```

## Alias System

Fujin supports command aliases defined in `fujin.toml`:

```toml
[aliases]
console = "app exec -i shell"
```

Parsed in `__main__.py:_parse_aliases()` and expands before command invocation.

## Testing Principles

### Test Structure

Tests are organized in `tests/` with clear separation:
- `tests/unit/` - Fast unit tests with no external dependencies
- `tests/installer/` - Zipapp installer tests
- `tests/integration/` - Docker-based integration tests requiring SSH

### Core Principles

**Avoid Noise**
- No docstrings that just repeat the function name
- Remove comments that don't add information beyond what the code shows
- Keep only meaningful inline comments that explain non-obvious behavior

**Consolidate Related Tests**
- Combine related single-assert tests into comprehensive tests
- Group multiple assertions testing the same functionality
- Example: Instead of `test_for_case_a()` and `test_for_case_b()`, write `test_handles_cases_a_and_b()`

**Test Business Logic, Not Libraries**
- Don't test framework/library functionality (e.g., msgspec type validation)
- Focus on our code's behavior and validation logic
- Test edge cases and error conditions in our code

**Fixtures**
- Add fixtures as needed, not upfront
- Use descriptive names that indicate what they provide
- Keep fixtures minimal and focused
- Prefer function-scoped fixtures for isolation

**Code Organization**
- All imports at the top of the file (no inline imports)
- Import constants from source instead of redefining them in tests
- Use logical section headers to group related tests
- Mark tests with appropriate pytest markers (`@pytest.mark.unit`, etc.)

**Error Assertions**
- Access error messages via `.message` attribute for `ImproperlyConfiguredError`
- Check for meaningful error message content, not just exception type
- Example: `assert "expected text" in exc_info.value.message.lower()`

### Running Tests

```bash
# Run all tests (excludes integration by default)
just test

# Run integration tests (requires Docker)
just test-integration

# Run specific test file
just test tests/unit/test_config.py

# Run with specific marker
pytest -m unit
```
