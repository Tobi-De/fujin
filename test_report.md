# Test Architecture Review & Recommendations

**Project:** Fujin
**Test Framework:** pytest
**Current Test LOC:** ~2,277 lines across 12 test files
**Date:** 2025-12-24

## Executive Summary

The current test suite has a solid foundation but suffers from architectural issues that will become more problematic as the codebase grows. The recent zipapp migration has exposed gaps in the testing approach, particularly around installer testing. This report focuses on structural and architectural improvements rather than coverage gaps.

---

## Current Architecture

### Directory Structure
```
tests/
├── conftest.py              # Global fixtures
├── script_runner.py         # Shell script test utility
├── test_*.py                # Unit tests (10 files)
└── integration/
    ├── conftest.py          # Integration test fixtures
    ├── Dockerfile           # Test environment
    └── test_full_deploy.py  # End-to-end tests
```

### Key Components

1. **Global Fixtures (conftest.py)**
   - `mock_config`: Standard config for tests
   - `mock_connection`: Mocked SSH connection
   - Autouse patches for connection and config
   - Command extraction helpers

2. **Script Runner**
   - Custom utility for testing bash scripts
   - Creates isolated filesystem with mocked commands
   - Logs command execution for assertions

3. **Integration Tests**
   - Docker-based with systemd support
   - Real SSH connections
   - Full deployment workflow testing

---

## Critical Issues

### 1. Over-Aggressive Autouse Fixtures

**Problem:**
```python
@pytest.fixture(autouse=True)
def patch_host_connection(mock_connection):
    # Applied to ALL tests automatically

@pytest.fixture(autouse=True)
def patch_config_read(mock_config):
    # Applied to ALL tests automatically
```

**Impact:**
- Makes it impossible to test actual connection or config logic
- Creates hidden dependencies between tests
- Reduces test isolation
- Makes test intent unclear (which tests need mocking?)

**Recommendation:**
- Remove `autouse=True` from these fixtures
- Apply them explicitly only to tests that need them
- Create marker-based approach: `@pytest.mark.mock_connection`

```python
# Preferred approach
@pytest.fixture
def isolated_test_env(mock_config, mock_connection):
    """Explicitly combine fixtures for command tests."""
    return {"config": mock_config, "conn": mock_connection}

# Usage
def test_deploy_command(isolated_test_env):
    # Clear about what's being mocked
```

---

### 2. Script Runner Obsolescence

**Problem:**
The `ScriptRunner` class was designed for testing bash scripts (install.sh, uninstall.sh), but the zipapp migration means:
- No more shell scripts to test
- Installer is now Python code in a zipapp
- Path rewriting heuristics don't work for Python

**Current State:**
```python
def test_script_execution_binary(...):
    install_script = (bundle_dir / "install.sh").read_text()  # Doesn't exist anymore!
    result = script_runner.run(install_script, cwd=bundle_dir)
```

**Recommendation:**
Either:
1. **Remove ScriptRunner entirely** and test the Python installer directly
2. **Adapt it for zipapp testing** with a new `ZipappRunner` utility

```python
# Option 1: Direct Python testing
def test_installer_install():
    from fujin._installer.__main__ import install
    with temp_bundle_dir() as bundle:
        os.chdir(bundle)
        install()
        # Assert filesystem state

# Option 2: New ZipappRunner
class ZipappRunner:
    def run_zipapp(self, pyz_path, command):
        """Run a zipapp in isolated environment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                ["python3", str(pyz_path), command],
                cwd=tmpdir,
                env=self.env,
                check=True
            )
        return Result(...)
```

---

### 3. Test Isolation Problems

**Problem:**
- Shared `mock_config` can accumulate state changes
- Autouse fixtures create implicit dependencies
- No clear reset between tests

**Example of State Leak:**
```python
def test_a(mock_config):
    mock_config.processes["new"] = ProcessConfig(...)  # Mutates shared object

def test_b(mock_config):
    # May see "new" process from test_a depending on test order
```

**Recommendation:**
- Make fixtures return fresh instances every time
- Use `scope="function"` explicitly (default, but be clear)
- Consider immutable config objects or deep copies

```python
@pytest.fixture
def mock_config():
    """Returns a fresh config for each test."""
    return Config(...)  # New instance every time, no mutations carry over
```

---

### 4. Integration Test Fragility

**Problem:**
```python
# Wait for SSH to be ready
time.sleep(5)

# Ensure ssh is running
subprocess.run(["docker", "exec", container_name, "service", "ssh", "start"])

time.sleep(2)  # More waiting
```

**Issues:**
- Fixed sleep durations are unreliable (CI environments, load)
- No retry logic
- No health checks

**Recommendation:**
Implement proper waiting with retries:

```python
def wait_for_ssh(container, timeout=30):
    """Wait for SSH to be ready with retries."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = subprocess.run(
                ["docker", "exec", container, "systemctl", "is-active", "ssh"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    raise RuntimeError(f"SSH not ready after {timeout}s")
```

---

### 5. Missing Test Markers and Organization

**Problem:**
- No way to run only fast tests
- No markers for tests requiring Docker
- No distinction between different test types

**Current:**
```toml
[tool.pytest.ini_options]
markers = [ "use_recorder: mark test to use the mock recorder" ]
```

**Recommendation:**
Add comprehensive markers:

```toml
[tool.pytest.ini_options]
markers = [
    "unit: Fast unit tests with no external dependencies",
    "integration: Integration tests requiring Docker",
    "slow: Tests that take >5 seconds",
    "ssh: Tests requiring SSH connection",
    "installer: Tests for the zipapp installer",
]

# Run only fast tests
# pytest -m "unit"

# Skip integration tests
# pytest -m "not integration"
```

---

### 6. Fixture Organization and Discoverability

**Problem:**
- Fixtures scattered across files
- No clear hierarchy or documentation
- Factory fixtures poorly named

**Example:**
```python
@pytest.fixture
def get_commands():  # This is a factory, not a value!
    def _get(mock_calls):
        # Complex parsing logic
        ...
    return _get
```

**Recommendation:**
1. **Create fixture hierarchy documentation**
2. **Rename factory fixtures** to indicate they're factories
3. **Group related fixtures**

```python
# fixtures/config.py
@pytest.fixture
def base_config():
    """Minimal valid configuration."""
    return Config(...)

@pytest.fixture
def binary_config(base_config):
    """Configuration for binary installation."""
    base_config.installation_mode = InstallationMode.BINARY
    return base_config

# fixtures/commands.py
@pytest.fixture
def command_parser_factory():  # Clearly a factory
    """Factory for parsing commands from mock calls."""
    def parse(mock_calls):
        ...
    return parse
```

---

### 7. Mock Complexity and Fragility

**Problem:**
Tests rely on fragile string matching and deep mock inspection:

```python
def test_example(mock_calls, get_commands):
    assert get_commands(mock_calls) == snapshot([
        'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && sudo systemctl start testapp.service'
    ])
```

**Issues:**
- String matching breaks with minor formatting changes
- Hard to understand what's being tested
- Snapshots can hide regressions if blindly updated

**Recommendation:**
Use structured assertions with helper functions:

```python
# test_helpers.py
class CommandAssertion:
    def __init__(self, mock_calls):
        self.calls = mock_calls

    def assert_ran(self, command_pattern):
        """Assert a command matching pattern was executed."""
        commands = [call[0][0] for call in self.calls if call[0]]
        assert any(command_pattern in cmd for cmd in commands), \
            f"Expected command '{command_pattern}' not found in: {commands}"

    def assert_sequence(self, *patterns):
        """Assert commands ran in specific order."""
        ...

# Usage
def test_deploy(mock_connection):
    deploy = Deploy()
    deploy()

    assertions = CommandAssertion(mock_connection.run.call_args_list)
    assertions.assert_ran("python3")
    assertions.assert_ran("install")
    assertions.assert_sequence("mkdir", "python3", "rm -rf")
```

---

### 8. No Testing Strategy for Zipapp Installer

**Problem:**
The new zipapp installer has complex logic:
- Detects if running from zipapp
- Extracts to temp directory
- Manages cleanup
- Handles both install and uninstall

**Current Gap:**
No clear approach for testing this in isolation.

**Recommendation:**
Create dedicated installer test suite:

```python
# tests/test_installer.py
import tempfile
import zipfile
from pathlib import Path
import subprocess

@pytest.fixture
def mock_bundle():
    """Create a fake zipapp bundle for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = Path(tmpdir)

        # Create config.json
        (bundle_dir / "config.json").write_text(json.dumps({...}))

        # Create fake artifacts
        (bundle_dir / "units").mkdir()
        (bundle_dir / ".env").touch()

        # Create zipapp
        zipapp_path = Path(tmpdir) / "test.pyz"
        # ... create zipapp with installer code

        yield zipapp_path

def test_installer_extraction(mock_bundle):
    """Test that zipapp properly extracts to temp directory."""
    result = subprocess.run(
        ["python3", str(mock_bundle), "install"],
        capture_output=True,
        env={...}
    )
    assert result.returncode == 0
    assert "Extracting installer bundle" in result.stderr

def test_installer_cleanup():
    """Test that temp directory is cleaned up after install."""
    # Verify no /tmp/fujin-* directories remain
    ...
```

---

## Recommended Architecture

### Proposed Structure

```
tests/
├── fixtures/
│   ├── __init__.py
│   ├── config.py          # Configuration fixtures
│   ├── connection.py      # Connection/SSH fixtures
│   └── helpers.py         # Test helper utilities
├── unit/
│   ├── conftest.py        # Unit test specific fixtures
│   ├── test_config.py
│   ├── test_connection.py
│   └── commands/
│       ├── test_deploy.py
│       ├── test_rollback.py
│       └── ...
├── installer/
│   ├── conftest.py        # Installer-specific fixtures
│   ├── test_zipapp_creation.py
│   ├── test_install.py
│   ├── test_uninstall.py
│   └── test_extraction.py
├── integration/
│   ├── conftest.py
│   ├── Dockerfile
│   ├── test_full_deploy.py
│   └── test_rollback.py
└── conftest.py            # Only truly global fixtures
```

### Fixture Hierarchy

```
Global (tests/conftest.py)
├── pytest.ini configuration
└── Minimal global fixtures

Unit Tests (tests/unit/conftest.py)
├── mock_config
├── mock_connection
└── command_assertions

Installer Tests (tests/installer/conftest.py)
├── zipapp_builder
├── mock_bundle
└── isolated_installer_env

Integration Tests (tests/integration/conftest.py)
├── docker_image
├── ssh_setup
└── real_deployment_env
```

---

## Implementation Roadmap

### Phase 1: Fixture Cleanup (High Priority)
1. Remove `autouse=True` from global fixtures
2. Create explicit fixture combinations
3. Add test markers (unit, integration, slow)
4. Document fixture purposes

### Phase 2: Installer Testing (High Priority - Blocks Zipapp)
1. Create `tests/installer/` directory
2. Build zipapp test fixtures
3. Test extraction, install, uninstall flows
4. Remove or adapt script_runner

### Phase 3: Test Organization (Medium Priority)
1. Reorganize tests into unit/installer/integration
2. Create fixture modules in `tests/fixtures/`
3. Add helper utilities for assertions
4. Update documentation

### Phase 4: Integration Test Improvements (Medium Priority)
1. Add retry logic with timeouts
2. Implement health checks
3. Add better error messages
4. Consider testcontainers-python

### Phase 5: Mock Simplification (Low Priority)
1. Replace string matching with structured assertions
2. Create command assertion helpers
3. Reduce snapshot test usage
4. Add better mock introspection tools

---

## Quick Wins (Can Implement Immediately)

### 1. Add Test Markers
```toml
[tool.pytest.ini_options]
markers = [
    "unit: Unit tests",
    "integration: Integration tests requiring Docker",
    "slow: Slow tests (>5s)",
]
```

### 2. Remove Autouse from Connection Mock
```python
# Before
@pytest.fixture(autouse=True)
def patch_host_connection(mock_connection):
    ...

# After
@pytest.fixture
def patch_host_connection(mock_connection):
    ...

# Apply explicitly
@pytest.mark.usefixtures("patch_host_connection")
def test_something():
    ...
```

### 3. Add Retry Helper for Integration Tests
```python
def retry_until_success(func, timeout=30, interval=1):
    """Retry function until success or timeout."""
    start = time.time()
    last_error = None
    while time.time() - start < timeout:
        try:
            return func()
        except Exception as e:
            last_error = e
            time.sleep(interval)
    raise TimeoutError(f"Failed after {timeout}s: {last_error}")
```

### 4. Create Command Assertion Helper
```python
class CommandChecker:
    def __init__(self, mock_run_calls):
        self.calls = [call[0][0] for call in mock_run_calls if call[0]]

    def contains(self, pattern):
        return any(pattern in cmd for cmd in self.calls)

    def ran_in_order(self, *patterns):
        positions = []
        for pattern in patterns:
            for i, cmd in enumerate(self.calls):
                if pattern in cmd:
                    positions.append((pattern, i))
                    break
        return positions == sorted(positions, key=lambda x: x[1])
```

---

## Long-term Goals

1. **Achieve test independence**: Any test should run successfully in isolation
2. **Clear test categories**: Easy to run just unit tests, just integration tests, etc.
3. **Fast feedback loop**: Unit tests complete in <5 seconds
4. **Maintainable mocks**: Easy to understand what's being tested
5. **Comprehensive installer testing**: Full coverage of zipapp lifecycle

---

## Metrics to Track

- **Test execution time** by category (unit/integration)
- **Fixture usage** patterns (which fixtures are used together)
- **Test isolation** failures (tests that fail when run alone)
- **Flaky test** rate (especially integration tests)

---

## Conclusion

The test suite has a solid foundation but needs architectural improvements to scale with the codebase. The zipapp migration is an opportunity to rethink the testing strategy, particularly around installer testing. Focus on:

1. **Explicit over implicit** (remove autouse)
2. **Isolation** (fresh fixtures, clear dependencies)
3. **Organization** (clear structure, proper markers)
4. **Robustness** (retries, health checks, better assertions)

Implementing these changes incrementally will significantly improve test maintainability and developer experience.
