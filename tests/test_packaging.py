"""Guardrails for the optional secret-adapter extras.

`uv tool install "fujin-cli[bw,op,env]"` only works if the root project
declares one extra per adapter plugin, with a floor that is not older than the
released CLI. Both broke silently before: 1Password was only reachable through
``onepassword`` (not ``op``) and the floors stayed at an old beta.
"""

from __future__ import annotations

import re
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
PLUGINS_DIR = ROOT / "plugins"

#: Extra name that is not simply the adapter name.
EXTRA_ALIASES = {"op": "onepassword"}


def _project() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def _extras() -> dict[str, list[str]]:
    return _project().get("optional-dependencies", {})


def _requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0].strip()


def _version_key(version: str) -> tuple[int, int, int, int, int]:
    """Order ``0.28.0b9`` style versions without a runtime dependency."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", version)
    assert match, f"unexpected version format: {version}"
    major, minor, patch, stage, serial = match.groups()
    # A final release sorts above any pre-release of the same number.
    ranks = {None: 3, "rc": 2, "b": 1, "a": 0}
    return int(major), int(minor), int(patch), ranks[stage], int(serial or 0)


def test_every_plugin_is_installable_through_an_extra():
    extras = _extras()
    plugin_dists = {
        tomllib.loads((plugin / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["name"]
        for plugin in PLUGINS_DIR.iterdir()
        if (plugin / "pyproject.toml").exists()
    }

    covered = {
        _requirement_name(requirement)
        for requirements in extras.values()
        for requirement in requirements
    }

    assert plugin_dists <= covered, (
        f"plugins without an extra: {sorted(plugin_dists - covered)} "
        f"(declared extras: {sorted(extras)})"
    )


def test_short_adapter_aliases_are_declared():
    extras = _extras()
    for alias, target in EXTRA_ALIASES.items():
        assert alias in extras, f"missing `{alias}` extra"
        assert target in extras, f"missing `{target}` extra"
        assert [_requirement_name(r) for r in extras[alias]] == [
            _requirement_name(r) for r in extras[target]
        ], f"`{alias}` must install the same package as `{target}`"


def test_extra_floors_are_not_older_than_the_project():
    """Mirrors `just sync-plugin-versions`; a stale floor loses adapter fixes."""
    current = _version_key(_project()["version"])

    stale = []
    for extra, requirements in _extras().items():
        for requirement in requirements:
            floor = re.search(r">=\s*([^,;\s\"]+)", requirement)
            assert floor, f"`{extra}` requirement has no floor: {requirement}"
            if _version_key(floor.group(1)) < current:
                stale.append(f"{extra}: {requirement}")

    assert not stale, (
        "extras resolve an adapter older than the CLI; "
        "run `just sync-plugin-versions`: " + ", ".join(stale)
    )
