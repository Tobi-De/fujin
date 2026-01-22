from unittest.mock import Mock, patch

from fujin.commands.up import Up


def test_up_passes_host_to_subcommands(minimal_config):
    """Test that Up command passes host parameter to Server and Deploy."""
    with (
        patch("fujin.commands.up.Server") as mock_server_class,
        patch("fujin.commands.up.Deploy") as mock_deploy_class,
        patch("fujin.config.Config.read", return_value=minimal_config),
    ):
        # Create mock instances
        mock_server_instance = Mock()
        mock_deploy_instance = Mock()
        mock_server_class.return_value = mock_server_instance
        mock_deploy_class.return_value = mock_deploy_instance

        # Run up command with specific host
        up = Up(host="test-host")
        up()

        # Verify Server was instantiated with host parameter
        mock_server_class.assert_called_once_with(host="test-host")
        mock_server_instance.bootstrap.assert_called_once()

        # Verify Deploy was instantiated with host parameter
        mock_deploy_class.assert_called_once_with(host="test-host")
        mock_deploy_instance.assert_called_once()


def test_up_passes_none_when_no_host_specified(minimal_config):
    """Test that Up command passes None when no host is specified."""
    with (
        patch("fujin.commands.up.Server") as mock_server_class,
        patch("fujin.commands.up.Deploy") as mock_deploy_class,
        patch("fujin.config.Config.read", return_value=minimal_config),
    ):
        # Create mock instances
        mock_server_instance = Mock()
        mock_deploy_instance = Mock()
        mock_server_class.return_value = mock_server_instance
        mock_deploy_class.return_value = mock_deploy_instance

        # Run up command without host parameter
        up = Up(host=None)
        up()

        # Verify Server was instantiated with None (default)
        mock_server_class.assert_called_once_with(host=None)
        mock_server_instance.bootstrap.assert_called_once()

        # Verify Deploy was instantiated with None (default)
        mock_deploy_class.assert_called_once_with(host=None)
        mock_deploy_instance.assert_called_once()
