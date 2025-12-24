# Fujin Development Roadmap

**Last Updated:** 2025-12-24

This document tracks approved features and improvements. All open questions from RESPONSES.md have been resolved and integrated below.

---

## 🎯 High Priority (Next Sprint)

### 1. Default Aliases System
**Status:** New Feature
**Effort:** Low
**Impact:** High

Implement a default aliases configuration that ships with Fujin:

```toml
# Built-in aliases (users can override in their fujin.toml)
[aliases]
status = "app info"
up = "server bootstrap && deploy"
```

**Tasks:**
- [ ] Create default aliases configuration
- [ ] Document alias system
- [ ] Add `fujin aliases list` command to show available aliases

### 2. Better Error Messages
**Status:** Enhancement
**Effort:** Low
**Impact:** High

Replace terse error messages with helpful, actionable ones:

```python
# Instead of:
ImproperlyConfiguredError: No fujin.toml file found

# Show:
❌ No fujin.toml found in current directory.

Run 'fujin init' to create one, or cd to your project directory.
```

**Files to update:**
- `src/fujin/config.py` - Config loading errors
- `src/fujin/commands/*.py` - Command-specific errors
- Create error message formatting utilities

### 3. Clickable URLs in Output
**Status:** Enhancement
**Effort:** Low
**Impact:** Medium

Make URLs clickable in terminal output using Rich markup:

```python
# In deploy.py
self.stdout.output(
    f"[blue]Application available at: [link=https://{domain}]https://{domain}[/link][/blue]"
)
```

**Files to update:**
- `src/fujin/commands/deploy.py`
- Any other commands that output URLs

### 4. Progress Indicators
**Status:** Enhancement
**Effort:** Medium
**Impact:** High

Add progress spinners and indicators for long-running operations:

```python
from rich.progress import Progress, SpinnerColumn, TextColumn

with Progress(
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
) as progress:
    task = progress.add_task("Building...", total=None)
    subprocess.run(build_command)
    progress.update(task, description="Uploading...")
    # ... etc
```

**Tasks:**
- [ ] Add progress indicators to deploy command
- [ ] Add progress to build, upload, install phases
- [ ] Show upload progress with size information
- [ ] Ensure progress works with `--no-input` flag

### 5. Better Command Descriptions & Examples
**Status:** Enhancement
**Effort:** Low
**Impact:** Medium

Improve help text with descriptions and examples:

```python
@cappa.command(
    help="Deploy your application to the server",
    epilog="""
Examples:
  fujin deploy                    Deploy with default settings
  fujin deploy --no-input         Skip all prompts (CI mode)
    """
)
```

**Tasks:**
- [ ] Audit all command descriptions
- [ ] Add examples to commonly used commands
- [ ] Ensure consistent tone and style
- [ ] Add usage examples to documentation

### 6. Consistent Color Coding
**Status:** Enhancement
**Effort:** Low
**Impact:** Low

Standardize color usage across all commands:

- **Info:** Blue
- **Success:** Green
- **Warning:** Yellow
- **Error:** Red
- **Critical:** Red + Bold

**Tasks:**
- [ ] Audit all stdout.output() calls
- [ ] Create color constants or helpers
- [ ] Document color scheme for contributors

---

## 💎 Medium Priority (Next Quarter)

### 1. Better Log Streaming
**Status:** New Feature
**Effort:** Medium
**Impact:** High

Enhance the `fujin app logs` command:

```bash
# Enhanced syntax
fujin app logs web --follow --tail 100
fujin app logs --level error
fujin app logs --since "2 hours ago"
fujin app logs --grep "ValueError"
```

**Tasks:**
- [ ] Add `--tail` flag (default 50)
- [ ] Add `--level` filtering (error, warning, info)
- [ ] Add `--since` time filtering
- [ ] Add `--grep` pattern matching
- [ ] Document journalctl mapping

### 2. Resource Monitoring (`fujin server stats`)
**Status:** New Feature
**Effort:** Medium
**Impact:** High

Add server resource monitoring:

```bash
fujin server stats

# Output:
# CPU: 15%
# Memory: 2.1 GB / 4 GB (52%)
# Disk: 12 GB / 50 GB (24%)
# Network: ↓ 1.2 MB/s  ↑ 0.3 MB/s
```

**Tasks:**
- [ ] Implement `fujin server stats` command
- [ ] Parse `/proc` or use `top`/`htop` output
- [ ] Add optional `--watch` mode for continuous monitoring
- [ ] Consider per-process stats

### 3. SSH Setup Helper
**Status:** New Feature
**Effort:** Medium
**Impact:** Medium

Interactive SSH key setup:

```bash
fujin server setup-ssh
> Enter server IP: 1.2.3.4
> Enter username: ubuntu
> Enter password (or press Enter for key): ***
✓ SSH key copied to server
✓ Updated fujin.toml with connection details
```

**Scope:** Just SSH key setup - does NOT handle server bootstrapping (Python, uv, etc.). Single focused purpose.

**Tasks:**
- [ ] Implement `fujin server setup-ssh` command
- [ ] Use `ssh-copy-id` or equivalent
- [ ] Generate SSH key if needed
- [ ] Update fujin.toml automatically
- [ ] Test with different SSH configurations

### 4. Multi-Server / Environment Support
**Status:** New Feature
**Effort:** High
**Impact:** High

**Note:** This consolidates both "environment-specific configs" and "multi-server support" suggestions as they're essentially the same feature.

```toml
[[hosts]]
name = "production"
domain_name = "app.example.com"
user = "ubuntu"

[[hosts]]
name = "staging"
domain_name = "staging.example.com"
user = "ubuntu"
```

```bash
fujin deploy --host production
fujin deploy --host staging
fujin app logs --host production
```

**Default host behavior:** First host in the list is the default (no `--host` flag required for single/first host).

**Tasks:**
- [ ] Design config schema for multiple hosts
- [ ] Add `--host` flag to all relevant commands
- [ ] Implement host selection logic (first host is default)
- [ ] Update documentation
- [ ] Handle default host selection properly

### 5. Better Error Types
**Status:** Code Quality
**Effort:** Medium
**Impact:** Medium

Create specific exception hierarchy:

```python
class DeploymentError(Exception):
    """Base class for deployment errors"""

class BuildError(DeploymentError):
    """Build command failed"""

class UploadError(DeploymentError):
    """Bundle upload failed"""

class InstallError(DeploymentError):
    """Remote installation failed"""
```

**Tasks:**
- [ ] Create `src/fujin/errors.py` module
- [ ] Define error hierarchy
- [ ] Update commands to use specific errors
- [ ] Improve error messages with context
- [ ] Add error recovery suggestions

### 6. Type Hints Completion
**Status:** Code Quality
**Effort:** Low
**Impact:** Low

Complete type hints in connection.py and other modules:

**Tasks:**
- [ ] Audit connection.py for missing type hints
- [ ] Add return type annotations
- [ ] Ensure mypy passes with no errors
- [ ] Document complex types

### 7. Consolidate Small Commands
**Status:** Code Cleanup
**Effort:** Medium
**Impact:** Low

Consolidate very small commands into logical groups:

```python
# Instead of separate files for tiny commands, group related ones:
@cappa.command
class Config(BaseCommand):
    @cappa.command
    def show(self): ...

    @cappa.command
    def print_env(self): ...

    @cappa.command
    def validate(self): ...
```

**Tasks:**
- [ ] Identify commands under 20 lines
- [ ] Group by domain (config, server, app)
- [ ] Update imports and command structure
- [ ] Test command invocations still work

---

## 🔮 Low Priority / Future Consideration

### 1. Deployment Verification (Health Checks + Smoke Tests)
**Status:** New Feature
**Effort:** Medium
**Impact:** Medium
**Note:** Low priority. Consolidates "health checks" and "smoke tests" suggestions (they're the same thing).

**Behavior on failure:** Just warn - NO automatic rollback. Let user decide what to do.

Automatically verify deployment after completion:

```python
# After deployment
if self.verify:  # Default True, use --no-verify to skip
    self.stdout.output("[blue]Verifying deployment...[/blue]")

    # Check services are running
    for process in self.config.processes:
        units = self.config.get_active_unit_names(process)
        for unit in units:
            status, ok = conn.run(f"systemctl is-active {unit}", hide=True, warn=True)
            if ok:
                self.stdout.output(f"  ✓ {unit}")
            else:
                self.stdout.output(f"  ✗ {unit} failed")

    # Optional: Health check endpoint
    if self.config.health_check_url:
        response = requests.get(self.config.health_check_url, timeout=5)
        self.stdout.output(f"  ✓ Health endpoint responding")
```

**Config option:**
```toml
# Optional health check URL
health_check_url = "https://app.example.com/health"

# Or disable verification
[deploy]
verify = false
```

### 2. Deployment Confirmation with Summary
**Status:** New Feature
**Effort:** Medium
**Impact:** Medium
**Note:** Interesting but low priority.

Show summary before deploying:

```
┌─ Deployment Summary ─────────────────┐
│ App:         bookstore               │
│ Version:     0.14.1 → 0.15.0         │
│ Host:        book.example.com        │
│ Processes:   web (1), worker (2)     │
│ Bundle:      23.4 KB                 │
└──────────────────────────────────────┘

Deploy to production? [y/N]:
```

Add `--yes` flag to skip for CI/CD.

### 3. Pre/Post Deploy Hooks System
**Status:** New Feature
**Effort:** High
**Impact:** High
**Note:** This would enable notifications, backups, and other custom actions. Feature is for later - error handling behavior TBD.

```toml
[hooks]
pre_deploy = "python scripts/backup_db.py"
post_deploy = "curl -X POST https://example.com/notify -d '{\"status\": \"deployed\"}'"
```

**Replaces:**
- Deployment notifications (use post_deploy hook)
- Backup before deploy (use pre_deploy hook)

**Error handling:** TBD when implementing this feature (abort vs warn vs prompt).

**Tasks:**
- [ ] Design hook configuration schema
- [ ] Implement hook execution
- [ ] Decide error handling behavior (continue vs abort)
- [ ] Document common hook patterns
- [ ] Provide hook examples in docs

### 4. Template Customization Guide
**Status:** Documentation
**Effort:** Low
**Impact:** Low

```bash
fujin templates eject web

# Creates .fujin/web.service.j2 with helpful comments:
# This template is for the web process
# Available variables:
#   - app_name: bookstore
#   - command: .venv/bin/gunicorn...
# See docs: https://fujin.oluwatobi.dev/templates
```

**Tasks:**
- [ ] Implement `fujin templates eject` command
- [ ] Add inline documentation to templates
- [ ] Create template customization guide
- [ ] Add examples for common customizations

### 5. Deployment History Tracking
**Status:** New Feature
**Effort:** Medium
**Impact:** Low
**Note:** Low priority. Wait for user demand before implementing.

**Approach:** Store JSON file on server at `{app_dir}/.deployments.json` with deployment records (version, timestamp, user, git commit, etc.). Add `fujin app history` command to read and display it.

### 6. Audit Logging
**Status:** New Feature
**Effort:** Low
**Impact:** Low
**Note:** Interesting for compliance/debugging.

Log all remote commands:

```
~/.fujin/audit.log:
2024-12-24 12:30:15 deploy v0.15.0 to book.example.com
2024-12-24 12:31:02 server exec "systemctl restart app"
```

**Tasks:**
- [ ] Create audit log writer
- [ ] Log all SSH commands
- [ ] Add timestamps and user info
- [ ] Consider log rotation
- [ ] Add `fujin audit show` command

### 7. Secrets Plugin System
**Status:** Architecture
**Effort:** Medium
**Impact:** Medium
**Note:** Use true plugin system with entry points for extensibility.

```bash
# Core fujin ships with system adapter
pip install fujin-cli

# Install separate plugin packages
pip install fujin-secrets-bitwarden
pip install fujin-secrets-doppler
```

**Approach:**
- Use Python entry points for plugin discovery (`importlib.metadata.entry_points()`)
- Core fujin ships with `system` adapter only
- Each secret adapter becomes its own package (e.g., `fujin-secrets-bitwarden`)
- Auto-discover plugins at runtime via entry points group `fujin.secrets`
- Keep core secrets.py simple with plugin loading logic

**Entry point example:**
```toml
# fujin-secrets-bitwarden/pyproject.toml
[project.entry-points."fujin.secrets"]
bitwarden = "fujin_secrets_bitwarden:BitwardenAdapter"
```

**Tasks:**
- [ ] Design entry points interface and contract
- [ ] Implement plugin discovery in secrets.py
- [ ] Move existing adapters to separate packages
- [ ] Document plugin development guide
- [ ] Add helpful error messages for missing plugins

---

## ❌ Rejected / Not Doing

### Config Validation Command
**Reason:** "how are we validating systemd and caddy config locally? plus what's the point of validation the build command?"

### Local Development Mode
**Reason:** "No idea how this would works, no for now"

### Better Init Profiles
**Reason:** User said "nope"

### Extract Config Validation to Separate Module
**Reason:** "I don't want to split where I need to look for config and validation logic"

### Merge app.py and server.py exec Commands
**Reason:** "feels like change for the sake of change"

### Validate .env File for Secrets
**Reason:** "feels out of scope"

### Encrypted Config Support
**Reason:** "ssh does encrypt the data right? not sure there is a point"

### Better Secret Management UX (Interactive)
**Reason:** "Good idea for later maybe, but not for now" (moved to backlog)

### --dry-run Flag
**Reason:** "duplicate with deployment confirmation summary"

### Type-safe ProcessConfig Context
**Reason:** Unnecessary complexity. Current dict-based approach is fine for small, localized context dictionaries used with Jinja2 templates.

---

## 📝 Consolidated Features

The following suggestions were duplicates and have been consolidated:

### Environment-specific configs + Multi-server support
→ **Single "Multi-host Support" feature** (See Medium Priority #4)
**Reason:** These are the same thing, just described differently.

### Health checks + Smoke tests
→ **Single "Deployment Verification" feature** (See Low Priority #1)
**Reason:** Both verify deployment succeeded.

### Progress bar + Progress indicators
→ **Single "Progress Indicators" feature** (See High Priority #4)
**Reason:** Same feature, use Rich Progress throughout.

---

## 🧪 Testing (Separate Track)

**Note:** Testing improvements will be handled separately per the test_report.md recommendations.

**Key items:**
- Add integration tests for zipapp installer
- Implement smoke tests / health checks
- Follow test architecture improvements from test_report.md

---

## 📊 Progress Tracking

### Current Sprint Focus
- [ ] Implement default aliases system
- [ ] Better error messages
- [ ] Clickable URLs
- [ ] Progress indicators
- [ ] Better command descriptions & examples

### Next Sprint Candidates
- Better log streaming
- Resource monitoring
- SSH setup helper
- Better error types
- Command description improvements

---

## 🎯 Success Metrics

Track these to measure improvement:

- **User feedback** on error messages (qualitative)
- **Time to first successful deploy** for new users
- **Number of support questions** about common operations
- **CLI responsiveness** (commands complete with progress feedback)

---

*This roadmap is a living document. Update as priorities change or new requirements emerge.*
