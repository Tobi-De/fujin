# Integration Tests Optimization Plan

## Current State (27 tests, ~1938 lines)

### Test Files:
- `test_app_management.py` - 8 tests, 356 lines
- `test_full_deploy.py` - 8 tests, 814 lines  
- `test_installation.py` - 5 tests, 479 lines
- `test_server_bootstrap.py` - 6 tests, 289 lines

## Major Issues Found

### 1. **Duplicate Deployments** (BIGGEST PERFORMANCE KILLER)

**Problem:** Every test deploys a fresh app with a unique name, causing:
- Docker container setup/teardown overhead
- Full deployment cycle for each test
- Total ~27 deployments for 27 tests

**Examples:**
```python
# test_app_management.py
test_app_restart_restarts_service → deploys "restartapp"
test_app_stop_and_start → deploys "stopstartapp"  
test_app_status_shows_service_info → deploys "statusapp"
test_app_status_detail_shows_extended_info → deploys "detailapp"
test_app_logs_retrieves_service_logs → deploys "logsapp"
test_app_cat_shows_unit_file → deploys "catapp"
test_app_cat_env_shows_environment_file → deploys "catenvapp"
```

**Impact:** Each deployment takes ~10-30 seconds. 8 tests × 20s = 160 seconds wasted!

### 2. **Useless Status Tests**

These tests do nothing:
```python
def test_app_status_shows_service_info(vps_container, ssh_key, tmp_path, monkeypatch, capsys):
    config = deploy_test_app(vps_container, ssh_key, tmp_path, "statusapp")
    
    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        app.status()
    
    # The fact that we get here means status worked ← NOT TESTING ANYTHING!

def test_app_status_detail_shows_extended_info(...):
    app.status(service="web")
    # The fact that we get here without error means it worked ← NOT TESTING ANYTHING!

def test_status_command_shows_system_info(...):
    server.status()
    # The command should complete without error ← NOT TESTING ANYTHING!
```

**Verdict:** DELETE all 3 status tests. They deploy apps, run commands, then just check "it didn't crash". No assertions on actual output.

### 3. **Redundant Bootstrap Tests**

`test_server_bootstrap.py` has 3 tests that all verify the same bootstrap behavior:

```python
test_bootstrap_is_idempotent → Runs bootstrap twice, checks fujin group/dirs
test_bootstrap_sets_up_caddy_when_caddyfile_exists → Runs bootstrap, checks Caddy
test_bootstrap_creates_fujin_group_and_directories → Runs bootstrap, checks groups/dirs
```

**Problem:** All three run full bootstrap. Tests 1 and 3 check the same things!

### 4. **Test Combination Opportunities**

#### App Management Tests (Can merge 8 → 2)

**Group A: Service Lifecycle** (Merge into 1 test)
- `test_app_restart_restarts_service`
- `test_app_stop_and_start`  
- `test_app_restart_all_services`

**Group B: Introspection** (Merge into 1 test)
- `test_app_logs_retrieves_service_logs`
- `test_app_cat_shows_unit_file`
- `test_app_cat_env_shows_environment_file`

**DELETE** (3 tests):
- `test_app_status_shows_service_info` - No assertions
- `test_app_status_detail_shows_extended_info` - No assertions  
- (Keep logs/cat tests in merged form)

#### Full Deploy Tests (8 tests, mostly good)

**Keep:**
- `test_binary_deployment` ✅
- `test_python_package_deployment` ✅
- `test_deployment_with_webserver` ✅
- `test_rollback_to_previous_version` ✅
- `test_down_command` ✅
- `test_deploy_with_environment_secrets` ✅
- `test_sequential_deploys_update_version` ✅
- `test_deploy_preserves_app_data_between_versions` ✅

**These are good** - each tests distinct deployment scenario.

#### Installation Tests (5 tests, good separation)

**Keep all 5** - each tests distinct systemd feature:
- `test_socket_activated_service` ✅
- `test_timer_scheduled_service` ✅
- `test_stale_unit_cleanup` ✅
- `test_dropin_directory_handling` ✅
- `test_stale_dropin_cleanup` ✅

#### Bootstrap Tests (Merge 6 → 3)

**Merge:**
- `test_bootstrap_is_idempotent` + `test_bootstrap_creates_fujin_group_and_directories` 
  → `test_bootstrap_creates_infrastructure` (runs twice, checks all)

**Keep:**
- `test_bootstrap_sets_up_caddy_when_caddyfile_exists` ✅
- `test_create_user_creates_user_with_ssh_access` ✅
- `test_create_user_with_password_generates_password` ✅

**DELETE:**
- `test_status_command_shows_system_info` - No assertions

---

## Optimization Strategies

### Strategy 1: Shared App Deployment (Fixture)

Instead of deploying a new app for each test, deploy ONCE per test file.

**Before:**
```python
def test_app_restart_restarts_service(vps_container, ssh_key, tmp_path, monkeypatch):
    config = deploy_test_app(vps_container, ssh_key, tmp_path, "restartapp")
    # test restart...

def test_app_stop_and_start(vps_container, ssh_key, tmp_path, monkeypatch):
    config = deploy_test_app(vps_container, ssh_key, tmp_path, "stopstartapp")
    # test stop/start...
```

**After:**
```python
@pytest.fixture(scope="module")  # ← Deploy once for all tests in module
def deployed_app(vps_container, ssh_key, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("app")
    config = deploy_test_app(vps_container, ssh_key, tmp_path, "mgmtapp")
    return config

def test_app_lifecycle(deployed_app, vps_container):
    """Test restart, stop, start in one test."""
    # Test restart
    stdout, _ = exec_in_container(...)
    initial_pid = stdout.strip()
    
    with patch("fujin.config.Config.read", return_value=deployed_app):
        app = App()
        app.restart(name="web")
    
    wait_for_service(vps_container["name"], "mgmtapp-web.service")
    stdout, _ = exec_in_container(...)
    new_pid = stdout.strip()
    assert new_pid != initial_pid
    
    # Test stop
    app.stop(name="web")
    stdout, _ = exec_in_container(...)
    assert stdout in ["inactive", "failed"]
    
    # Test start
    app.start(name="web")
    wait_for_service(vps_container["name"], "mgmtapp-web.service")

def test_app_introspection(deployed_app, vps_container):
    """Test logs, cat unit, cat env in one test."""
    # All use same deployed app
    # Test logs
    app.logs(name="web", lines=10, follow=False)
    stdout, success = exec_in_container(...)
    assert success
    
    # Test cat unit
    app.cat(name="web")
    stdout, success = exec_in_container(...)
    assert "mgmtapp" in stdout
    
    # Test cat env
    app.cat(name="env")
    stdout, success = exec_in_container(...)
    assert "DEBUG=true" in stdout
```

**Savings:** 8 deployments → 1 deployment = **~140 seconds saved**

### Strategy 2: Remove No-Op Tests

DELETE these tests (they deploy apps but don't test anything):
- `test_app_status_shows_service_info`
- `test_app_status_detail_shows_extended_info`
- `test_status_command_shows_system_info`

**Savings:** 3 deployments × 20s = **~60 seconds saved**

### Strategy 3: Merge Bootstrap Tests

**Before (3 tests, 3 bootstraps):**
```python
test_bootstrap_is_idempotent
test_bootstrap_creates_fujin_group_and_directories  # ← Same checks!
```

**After (1 test, 1 bootstrap):**
```python
def test_bootstrap_creates_infrastructure(vps_container, ssh_key, tmp_path, monkeypatch):
    """Bootstrap creates group, dirs, permissions and is idempotent."""
    config = msgspec.convert(config_dict, type=Config)
    
    # Run bootstrap
    with patch("fujin.config.Config.read", return_value=config):
        server = Server()
        server.bootstrap()
    
    # Verify fujin group
    stdout, success = exec_in_container(vps_container["name"], "getent group fujin")
    assert success
    
    # Verify directories with permissions
    assert_dir_exists(vps_container["name"], "/opt/fujin")
    assert_dir_exists(vps_container["name"], "/opt/fujin/.python")
    
    stdout, _ = exec_in_container(vps_container["name"], "stat -c '%a' /opt/fujin")
    assert stdout == "775"
    
    stdout, _ = exec_in_container(vps_container["name"], "stat -c '%G' /opt/fujin")
    assert stdout == "fujin"
    
    # Verify user in group
    stdout, success = exec_in_container(...)
    assert "fujin" in stdout
    
    # Verify uv installed
    stdout, success = exec_in_container(vps_container["name"], "command -v uv")
    assert success
    
    # Run again - should be idempotent
    with patch("fujin.config.Config.read", return_value=config):
        server.bootstrap()
    
    # Verify still works
    assert_dir_exists(vps_container["name"], "/opt/fujin")
```

**Savings:** 3 bootstraps → 1 bootstrap = **~40 seconds saved**

### Strategy 4: Parallel Test Execution

Current: Tests run sequentially
Proposed: Use `pytest-xdist` to run tests in parallel

```bash
# Run tests in parallel (4 workers)
pytest -n 4 tests/integration/
```

**Caveat:** Need separate Docker containers per worker, or risk conflicts.

**Alternative:** Tests within a file share container, but different files run in parallel.

---

## Proposed New Structure

### test_app_management.py (8 tests → 2 tests)

```python
@pytest.fixture(scope="module")
def deployed_app(vps_container, ssh_key, tmp_path_factory):
    """Deploy app once for all tests in this module."""
    # Deploy with web + worker services
    ...
    return config

def test_service_lifecycle(deployed_app, vps_container):
    """Test restart single service, stop/start, restart all services."""
    # Test restart single
    # Test stop/start
    # Test restart all

def test_app_introspection(deployed_app, vps_container):
    """Test logs, cat unit, cat env commands."""
    # Test logs
    # Test cat unit
    # Test cat env

# DELETE: test_app_status_shows_service_info
# DELETE: test_app_status_detail_shows_extended_info
```

### test_server_bootstrap.py (6 tests → 4 tests)

```python
def test_bootstrap_creates_infrastructure(vps_container, ssh_key, tmp_path, monkeypatch):
    """Bootstrap creates group, directories, permissions and is idempotent."""
    # Run bootstrap
    # Check group, dirs, perms, uv
    # Run again (idempotence)
    # Check still works

def test_bootstrap_sets_up_caddy_when_caddyfile_exists(...):
    # Keep as-is

def test_create_user_creates_user_with_ssh_access(...):
    # Keep as-is

def test_create_user_with_password_generates_password(...):
    # Keep as-is

# MERGED: test_bootstrap_is_idempotent + test_bootstrap_creates_fujin_group_and_directories
# DELETE: test_status_command_shows_system_info
```

### test_full_deploy.py (8 tests, no changes)

**Keep all 8** - they test distinct scenarios and are well-structured.

### test_installation.py (5 tests, no changes)

**Keep all 5** - they test distinct systemd features.

---

## Final Test Count

| File | Before | After | Reduction |
|------|--------|-------|-----------|
| test_app_management.py | 8 | 2 | -6 (-75%) |
| test_server_bootstrap.py | 6 | 4 | -2 (-33%) |
| test_full_deploy.py | 8 | 8 | 0 |
| test_installation.py | 5 | 5 | 0 |
| **Total** | **27** | **19** | **-8 (-30%)** |

---

## Performance Improvements

### Time Savings Estimate

**Current:** ~27 tests × ~20s average = ~540 seconds (~9 minutes)

**Optimizations:**
1. Shared app fixture in test_app_management: -140s (7 fewer deploys)
2. Delete 3 no-op status tests: -60s (3 fewer deploys)
3. Merge 2 bootstrap tests: -40s (1 fewer bootstrap)
4. Total: **-240 seconds saved (~4 minutes)**

**New total:** ~300 seconds (~5 minutes)

**With pytest-xdist (4 workers):** ~75-90 seconds (~1.5 minutes)

---

## Additional Optimizations

### 1. Smaller Test Binaries

Current test apps write full scripts. Use simpler binaries:

```python
# Before (heavy)
script_content = """#!/usr/bin/env python3
import http.server
import socketserver
PORT = 8000
Handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    httpd.serve_forever()
"""

# After (light)
distfile.write_text("#!/bin/bash\nwhile true; do sleep 5; done\n")
```

**Savings:** Faster startup, less Docker overhead

### 2. Reduce Sleep/Wait Times

Review all `time.sleep()` and `wait_for_service()` calls:

```python
# Current
time.sleep(2)  # Wait for logs to accumulate

# Optimized
time.sleep(0.5)  # Logs appear almost immediately
```

**Savings:** ~10-15 seconds across all tests

### 3. Reuse Docker Container

Current: Each test file creates new container?
Proposed: Single container for all tests (session scope)

```python
@pytest.fixture(scope="session")
def vps_container():
    # Create once, use for all tests
    ...
```

**Caveat:** Tests must clean up after themselves (or namespace apps)

**Savings:** ~30-60 seconds (container setup/teardown)

---

## Implementation Priority

### Phase 1: Quick Wins (30 min)
1. ✅ Delete 3 no-op status tests
2. ✅ Merge 2 redundant bootstrap tests

**Expected:** -100s, 8 fewer tests

### Phase 2: App Management Refactor (1-2 hours)
1. ✅ Create `deployed_app` fixture with module scope
2. ✅ Merge 8 tests → 2 tests
3. ✅ Verify all behavior still covered

**Expected:** -140s

### Phase 3: Speed Optimizations (1 hour)
1. ✅ Reduce sleep times
2. ✅ Simplify test binaries
3. ✅ Review wait_for_service timeout

**Expected:** -15s

### Phase 4: Parallel Execution (Optional, 2 hours)
1. ✅ Set up pytest-xdist
2. ✅ Ensure tests don't conflict (separate app names)
3. ✅ Configure CI for parallel runs

**Expected:** 4x speedup (~60s total)

---

## Migration Checklist

### test_app_management.py

- [ ] Create `deployed_app` fixture (module scope)
- [ ] Create `test_service_lifecycle` (merge restart + stop/start + restart-all)
- [ ] Create `test_app_introspection` (merge logs + cat + cat env)
- [ ] Delete `test_app_status_shows_service_info`
- [ ] Delete `test_app_status_detail_shows_extended_info`
- [ ] Delete `test_app_restart_restarts_service`
- [ ] Delete `test_app_stop_and_start`
- [ ] Delete `test_app_logs_retrieves_service_logs`
- [ ] Delete `test_app_cat_shows_unit_file`
- [ ] Delete `test_app_cat_env_shows_environment_file`
- [ ] Delete `test_app_restart_all_services`

### test_server_bootstrap.py

- [ ] Create `test_bootstrap_creates_infrastructure` (merge idempotent + group/dirs)
- [ ] Delete `test_bootstrap_is_idempotent`
- [ ] Delete `test_bootstrap_creates_fujin_group_and_directories`
- [ ] Delete `test_status_command_shows_system_info`

### General Optimizations

- [ ] Review all `time.sleep()` calls, reduce where safe
- [ ] Simplify test binary scripts
- [ ] Consider session-scoped container fixture
- [ ] Set up pytest-xdist in CI

---

## Risk Assessment

**Low Risk:**
- Deleting no-op status tests ✅ (they don't test anything)
- Merging redundant bootstrap tests ✅ (same checks)

**Medium Risk:**
- Shared app fixture ⚠️ (tests must not interfere with each other)
  - Mitigation: Each test operates on different services or verifies different things
  - Use separate services (web, worker) for different test aspects

**High Risk:**
- Parallel execution ⚠️⚠️ (app name conflicts, container conflicts)
  - Mitigation: Ensure app names unique, use test isolation

---

## Success Metrics

- ✅ Test count: 27 → 19 tests (-30%)
- ✅ Test runtime: ~9min → ~5min (-44%)
- ✅ With parallel: ~5min → ~1.5min (-83%)
- ✅ Maintain 100% coverage of features
- ✅ No flaky tests introduced
- ✅ All tests still verify real behavior

---

## Conclusion

**Primary wins:**
1. Delete 3 useless status tests (-60s)
2. Shared app deployment fixture (-140s)
3. Merge redundant bootstrap tests (-40s)

**Total:** -240s (~4 minutes faster), -8 tests

**With parallelization:** Additional 4x speedup = ~1.5min total

**Recommendation:** Start with Phase 1 & 2 (quick wins + app management), then evaluate if parallel execution is worth the complexity.
