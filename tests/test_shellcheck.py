import subprocess
import pytest
from fujin.config import InstallationMode


def test_install_script_shellcheck(mock_config, tmp_path):
    # Render the script
    new_units, user_units = mock_config.render_systemd_units()
    context = mock_config.build_context(
        distfile_name="testapp-0.1.0.whl",
        user_units=user_units,
        new_units=new_units,
    )

    script_content = mock_config.render_install_script(context=context)

    script_path = tmp_path / "install.sh"
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


def test_uninstall_script_shellcheck(mock_config, tmp_path):
    # Render the script
    new_units, user_units = mock_config.render_systemd_units()
    context = mock_config.build_context(
        distfile_name="testapp-0.1.0.whl",
        user_units=user_units,
        new_units=new_units,
    )

    script_content = mock_config.render_uninstall_script(context=context)

    script_path = tmp_path / "uninstall.sh"
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


def test_install_script_shellcheck_binary_mode(mock_config, tmp_path):
    mock_config.installation_mode = InstallationMode.BINARY

    new_units, user_units = mock_config.render_systemd_units()
    context = mock_config.build_context(
        distfile_name="testapp",
        user_units=user_units,
        new_units=new_units,
    )

    script_content = mock_config.render_install_script(context=context)

    script_path = tmp_path / "install_binary.sh"
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
