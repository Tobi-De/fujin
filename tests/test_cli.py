"""Tests for the CLI entry points' interrupt handling."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fujin import fa
from fujin import __main__ as cli_entry
from fujin.errors import CommandInterrupted
from fujin.exit_codes import EXIT_USER_ABORTED


@pytest.mark.parametrize(
    "interrupt",
    [KeyboardInterrupt(), CommandInterrupted("Interrupted by the user")],
    ids=["keyboard-interrupt", "raw-mode-ctrl-c"],
)
def test_fujin_exits_quietly_on_ctrl_c(interrupt, tmp_path, monkeypatch, capsys):
    """Ctrl+C is a normal way to stop a remote command, not a crash: the
    documented user-abort exit code, and no traceback."""
    monkeypatch.chdir(tmp_path)

    with patch("fujin.__main__.cappa.invoke", side_effect=interrupt):
        with pytest.raises(SystemExit) as info:
            cli_entry.main()

    assert info.value.code == EXIT_USER_ABORTED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "\n"


def test_fa_exits_quietly_on_ctrl_c(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    with patch("fujin.fa.cappa.invoke", side_effect=KeyboardInterrupt()):
        with pytest.raises(SystemExit) as info:
            fa.main()

    assert info.value.code == EXIT_USER_ABORTED
    assert capsys.readouterr().out == ""
