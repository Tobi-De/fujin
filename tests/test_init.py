from unittest.mock import patch, MagicMock
from fujin.commands.init import Init


def test_init_creates_fujin_toml():
    with (
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.write_text") as mock_write,
        patch("pathlib.Path.resolve", return_value=MagicMock(stem="testapp")),
    ):
        init = Init(profile="simple")
        init()

        assert mock_write.called
        content = mock_write.call_args[0][0]
        assert 'app = "testapp"' in content
        assert 'installation_mode = "python-package"' in content


def test_init_skips_if_exists():
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.write_text") as mock_write,
    ):
        init = Init()
        init()

        assert not mock_write.called


def test_init_with_templates():
    with (
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.write_text"),
        patch("pathlib.Path.mkdir") as mock_mkdir,
        patch("shutil.copy") as mock_copy,
        patch(
            "pathlib.Path.iterdir",
            return_value=[MagicMock(name="t1"), MagicMock(name="t2")],
        ),
        patch("pathlib.Path.resolve", return_value=MagicMock(stem="testapp")),
    ):
        init = Init(templates=True)
        init()

        assert mock_mkdir.called
        assert mock_copy.call_count == 2
