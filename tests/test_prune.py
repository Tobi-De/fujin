"""Tests for the prune command (release-tree store).

Prune deletes release digests under ``releases/`` per ``store.gc_plan``
(keep current + previous[0] + the newest ``keep`` releases), honoring the
state registry in ``store/state.json``. Pre-release-tree apps (no
``store/`` directory) are left untouched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin import store
from fujin.commands.prune import Prune
from fujin.config import Config

# base_config fixture from conftest.py

DIGESTS = {
    "a": "aaaaaaaa00000000000000000000000000000000000000000000000000000000",
    "b": "bbbbbbbb00000000000000000000000000000000000000000000000000000000",
    "c": "cccccccc00000000000000000000000000000000000000000000000000000000",
    "d": "dddddddd00000000000000000000000000000000000000000000000000000000",
}


def state_bytes(current=None, previous=()) -> str:
    """Serialized store/state.json content for a mock connection."""
    return store.serialize_state(
        store.State(current=current, previous=list(previous))
    ).decode()


def make_prune(minimal_config_dict, run_side_effects, keep, confirm=True):
    """Wire a Prune command against a scripted mock connection."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.side_effect = run_side_effects

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.prune.Confirm") as mock_confirm,
        patch.object(Prune, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = confirm
        prune = Prune(keep=keep)
        prune()

    calls = [call[0][0] for call in mock_conn.run.call_args_list]
    return calls, mock_output, mock_confirm


def rm_cmds(calls: list[str]) -> list[str]:
    return [cmd for cmd in calls if "rm -rf" in cmd]


# ============================================================================
# Validation
# ============================================================================


@pytest.mark.parametrize("keep", [0, 1])
def test_prune_keep_below_2_raises_error(minimal_config_dict, keep):
    """prune with --keep < 2 raises error (current + previous always kept)."""
    config = msgspec.convert(minimal_config_dict, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Prune, "output", MagicMock()),
    ):
        prune = Prune(keep=keep)

        with pytest.raises(SystemExit) as exc_info:
            prune()

        assert exc_info.value.code == 1


# ============================================================================
# No Releases Scenarios
# ============================================================================


@pytest.mark.parametrize(
    "run_side_effects,keep,expected_message",
    [
        # Store present but state.json missing/corrupt
        (
            [("", True)],
            2,
            "No release store state found. Nothing to prune.",
        ),
        # State present but no releases on disk
        (
            [(state_bytes(current="d"), True), ("", True)],
            2,
            "No releases found to prune.",
        ),
        # Fewer releases than keep
        (
            [
                (state_bytes(current="d"), True),
                (f"{DIGESTS['d']}\n{DIGESTS['a']}", True),
            ],
            5,
            "Only 2 releases found. Nothing to prune (keep=5).",
        ),
    ],
)
def test_prune_no_releases_scenarios(
    minimal_config_dict, run_side_effects, keep, expected_message
):
    """prune handles various no-releases scenarios correctly."""
    calls, mock_output, _confirm = make_prune(
        minimal_config_dict, run_side_effects, keep
    )

    mock_output.info.assert_called_with(expected_message)
    assert not rm_cmds(calls)
    assert not mock_output.success.called


# ============================================================================
# Successful Pruning
# ============================================================================


def test_prune_deletes_old_releases_when_user_confirms(minimal_config_dict):
    """prune deletes old releases when user confirms."""
    # releases/ listing is newest (mtime) first.
    releases_output = f"{DIGESTS['c']}\n{DIGESTS['b']}\n{DIGESTS['a']}"
    state = state_bytes(current=DIGESTS["c"], previous=[DIGESTS["b"]])
    calls, mock_output, _confirm = make_prune(
        minimal_config_dict,
        [(state, True), (releases_output, True), ("", True)],
        keep=2,
        confirm=True,
    )

    # Should keep current + previous[0], delete the rest
    joined = "\n".join(rm_cmds(calls))
    assert DIGESTS["a"] in joined
    assert DIGESTS["b"] not in joined
    assert DIGESTS["c"] not in joined
    assert mock_output.success.called


def test_prune_aborts_when_user_declines(minimal_config_dict):
    """prune aborts when user declines confirmation."""
    releases_output = f"{DIGESTS['c']}\n{DIGESTS['b']}\n{DIGESTS['a']}"
    state = state_bytes(current=DIGESTS["c"], previous=[DIGESTS["b"]])
    calls, mock_output, mock_confirm = make_prune(
        minimal_config_dict,
        [(state, True), (releases_output, True)],
        keep=2,
        confirm=False,
    )

    # No rm commands, no success message
    assert not rm_cmds(calls)
    assert mock_confirm.ask.called
    assert not mock_output.success.called


def test_prune_defaults_to_versions_to_keep(minimal_config_dict):
    """prune without --keep falls back to versions_to_keep from config."""
    minimal_config_dict["versions_to_keep"] = 2
    releases_output = f"{DIGESTS['c']}\n{DIGESTS['b']}\n{DIGESTS['a']}"
    state = state_bytes(current=DIGESTS["c"], previous=[DIGESTS["b"]])
    calls, _mock_output, mock_confirm = make_prune(
        minimal_config_dict,
        [(state, True), (releases_output, True), ("", True)],
        keep=None,
        confirm=True,
    )

    assert mock_confirm.ask.called
    assert DIGESTS["a"] in "\n".join(rm_cmds(calls))
    assert DIGESTS["c"] not in "\n".join(rm_cmds(calls))


# ============================================================================
# Keep Parameter Behavior
# ============================================================================


def test_prune_with_keep_2_deletes_all_but_current_and_previous(minimal_config_dict):
    """prune with --keep 2 deletes everything but current + previous[0]."""
    releases_output = f"{DIGESTS['d']}\n{DIGESTS['c']}\n{DIGESTS['b']}\n{DIGESTS['a']}"
    state = state_bytes(current=DIGESTS["d"], previous=[DIGESTS["c"]])
    calls, _mock_output, _confirm = make_prune(
        minimal_config_dict,
        [(state, True), (releases_output, True), ("", True), ("", True)],
        keep=2,
        confirm=True,
    )

    joined = "\n".join(rm_cmds(calls))
    assert DIGESTS["b"] in joined
    assert DIGESTS["a"] in joined
    assert DIGESTS["d"] not in joined
    assert DIGESTS["c"] not in joined


def test_prune_with_keep_5_keeps_all_if_only_3_releases(minimal_config_dict):
    """prune with --keep 5 keeps all releases if only 3 exist."""
    releases_output = f"{DIGESTS['c']}\n{DIGESTS['b']}\n{DIGESTS['a']}"
    state = state_bytes(current=DIGESTS["c"], previous=[DIGESTS["b"]])
    calls, mock_output, _confirm = make_prune(
        minimal_config_dict,
        [(state, True), (releases_output, True)],
        keep=5,
    )

    mock_output.info.assert_called_with(
        "Only 3 releases found. Nothing to prune (keep=5)."
    )
    assert not rm_cmds(calls)


# ============================================================================
# Listing Filter
# ============================================================================


def test_prune_ignores_staging_entries(minimal_config_dict):
    """prune ignores staging directories (.staging-*) in releases/."""
    releases_output = f".staging-4242\n{DIGESTS['c']}\n{DIGESTS['b']}\n{DIGESTS['a']}"
    state = state_bytes(current=DIGESTS["c"], previous=[DIGESTS["b"]])
    calls, mock_output, _confirm = make_prune(
        minimal_config_dict,
        [(state, True), (releases_output, True), ("", True)],
        keep=2,
        confirm=True,
    )

    joined = "\n".join(rm_cmds(calls))
    assert ".staging-4242" not in joined
    assert DIGESTS["a"] in joined
    assert mock_output.success.called
