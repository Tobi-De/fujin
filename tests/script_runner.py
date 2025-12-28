import os
import stat
import shutil
import subprocess
from pathlib import Path
import pytest


class ScriptRunner:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "root"
        self.bin = tmp_path / "bin"
        self.logs = tmp_path / "logs"
        self.root.mkdir()
        self.bin.mkdir()
        self.logs.mkdir()

        # Setup environment
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}:{self.env['PATH']}"
        self.env["HOME"] = str(self.root / "home" / "testuser")

        # Create mock commands
        self._create_mock("sudo", 'exec "$@"')
        self._create_mock("systemctl", self._log_command("systemctl"))
        self._create_mock("caddy", self._log_command("caddy"))
        self._create_mock("uv", self._log_command("uv"))
        self._create_mock("git", self._log_command("git"))
        self._create_mock("chown", self._log_command("chown"))

        # Create necessary directories that scripts expect
        (self.root / "etc" / "systemd" / "system").mkdir(parents=True)
        (self.root / "etc" / "caddy" / "conf.d").mkdir(parents=True)
        (self.root / "home" / "testuser").mkdir(parents=True)

    def _create_mock(self, name: str, content: str):
        path = self.bin / name
        path.write_text(f"#!/bin/bash\n{content}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _log_command(self, name: str) -> str:
        log_file = self.logs / f"{name}.log"
        return f"""
echo "{name} $@" >> {log_file}
if [[ "{name}" == "uv" && "$1" == "venv" ]]; then
    mkdir -p .venv
fi
"""

    def run(self, script_content: str, cwd: Path = None):
        # Rewrite paths in script to point to our fake root
        # This is a heuristic and might need adjustment based on script content
        rewritten = script_content.replace("/etc/", f"{self.root}/etc/")
        rewritten = rewritten.replace("/home/", f"{self.root}/home/")

        # Write script to file
        script_path = self.root / "script.sh"
        script_path.write_text(rewritten)
        script_path.chmod(0o755)

        # Execute
        result = subprocess.run(
            [str(script_path)],
            env=self.env,
            cwd=str(cwd) if cwd else str(self.root),
            capture_output=True,
            text=True,
        )

        return RunResult(result, self.root, self.logs)


class RunResult:
    def __init__(self, process: subprocess.CompletedProcess, root: Path, logs: Path):
        self.process = process
        self.root = root
        self.logs = logs
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.returncode = process.returncode

    def assert_success(self):
        if self.returncode != 0:
            raise AssertionError(
                f"Script failed with code {self.returncode}.\nStdout: {self.stdout}\nStderr: {self.stderr}"
            )

    def get_log(self, name: str) -> str:
        log_file = self.logs / f"{name}.log"
        if log_file.exists():
            return log_file.read_text().strip()
        return ""


@pytest.fixture
def script_runner(tmp_path):
    return ScriptRunner(tmp_path)
