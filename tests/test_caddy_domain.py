"""Tests for Caddyfile domain extraction logic in Config."""

from __future__ import annotations

import msgspec
import pytest

from fujin.config import Config


@pytest.fixture
def config(minimal_config_dict, tmp_path):
    """Fixture for Config object with temporary directory."""
    minimal_config_dict["local_config_dir"] = tmp_path / ".fujin"
    return msgspec.convert(minimal_config_dict, type=Config)


def test_get_domain_name_returns_none_if_caddyfile_missing(config):
    """Returns None if Caddyfile does not exist."""
    assert config.get_domain_name() is None


@pytest.mark.parametrize(
    "caddyfile_content,expected_domain",
    [
        # Simple domain
        (
            """
example.com {
    reverse_proxy localhost:8000
}
""",
            "example.com",
        ),
        # Subdomain
        (
            """
app.example.com {
    reverse_proxy localhost:8000
}
""",
            "app.example.com",
        ),
        # Multiple domains on line (returns first)
        (
            """
example.com, www.example.com {
    reverse_proxy localhost:8000
}
""",
            "example.com",
        ),
    ],
)
def test_get_domain_name_extraction(
    config, tmp_path, caddyfile_content, expected_domain
):
    """Extracts domain names from various Caddyfile formats."""
    caddyfile = tmp_path / ".fujin" / "Caddyfile"
    caddyfile.parent.mkdir(parents=True)
    caddyfile.write_text(caddyfile_content)
    assert config.get_domain_name() == expected_domain


@pytest.mark.parametrize(
    "caddyfile_content,expected_domain",
    [
        # Skips comments
        (
            """
# old-domain.com {
example.com {
    reverse_proxy localhost:8000
}
""",
            "example.com",
        ),
        # Skips global options block
        (
            """
{
    email admin@example.com
}

example.com {
    reverse_proxy localhost:8000
}
""",
            "example.com",
        ),
        # Ignores blocks without dot (localhost)
        (
            """
localhost {
    respond "Hello"
}

example.com {
    reverse_proxy localhost:8000
}
""",
            "example.com",
        ),
        # Ignores named matchers
        (
            """
example.com {
    @static {
        file
        path *.ico *.css *.js
    }
    reverse_proxy localhost:8000
}
""",
            "example.com",
        ),
    ],
)
def test_get_domain_name_filters_and_skips(
    config, tmp_path, caddyfile_content, expected_domain
):
    """Correctly filters out non-domain blocks and comments."""
    caddyfile = tmp_path / ".fujin" / "Caddyfile"
    caddyfile.parent.mkdir(parents=True)
    caddyfile.write_text(caddyfile_content)
    assert config.get_domain_name() == expected_domain
