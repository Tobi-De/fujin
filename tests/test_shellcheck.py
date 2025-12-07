import subprocess
import pytest
from fujin.config import InstallationMode


def test_setup_script_shellcheck(mock_config, tmp_path):
    # Render the script
    new_units, user_units = mock_config.render_systemd_units()
    valid_units = set(mock_config.active_systemd_units) | set(new_units.keys())
    valid_units_str = " ".join(sorted(valid_units))

    script_content = mock_config.render_setup_script(
        distfile_name="testapp-0.1.0.whl",
        valid_units_str=valid_units_str,
        user_units=user_units,
    )

    script_path = tmp_path / "setup.sh"
    script_path.write_text(script_content)

    # Run shellcheck
    try:
        result = subprocess.run(
            ["shellcheck", str(script_path)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        pytest.skip("shellcheck not found")

    assert (
        result.returncode == 0
    ), f"ShellCheck failed:\n{result.stdout}\n{result.stderr}"


def test_setup_script_shellcheck_binary_mode(mock_config, tmp_path):
    mock_config.installation_mode = InstallationMode.BINARY

    new_units, user_units = mock_config.render_systemd_units()
    valid_units = set(mock_config.active_systemd_units) | set(new_units.keys())
    valid_units_str = " ".join(sorted(valid_units))

    script_content = mock_config.render_setup_script(
        distfile_name="testapp",
        valid_units_str=valid_units_str,
        user_units=user_units,
    )

    script_path = tmp_path / "setup_binary.sh"
    script_path.write_text(script_content)

    try:
        result = subprocess.run(
            ["shellcheck", str(script_path)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        pytest.skip("shellcheck not found")

    assert (
        result.returncode == 0
    ), f"ShellCheck failed:\n{result.stdout}\n{result.stderr}"
