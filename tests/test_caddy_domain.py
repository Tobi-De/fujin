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


def test_get_domain_name_simple_domain(config, tmp_path):
    """Extracts simple domain."""
    caddyfile = tmp_path / ".fujin" / "Caddyfile"
    caddyfile.parent.mkdir(parents=True)
    caddyfile.write_text("""
example.com {
    reverse_proxy localhost:8000
}
""")
    assert config.get_domain_name() == "example.com"


def test_get_domain_name_subdomain(config, tmp_path):
    """Extracts subdomain."""
    caddyfile = tmp_path / ".fujin" / "Caddyfile"
    caddyfile.parent.mkdir(parents=True)
    caddyfile.write_text("""
app.example.com {
    reverse_proxy localhost:8000
}
""")
    assert config.get_domain_name() == "app.example.com"


def test_get_domain_name_skips_comments(config, tmp_path):
    """Skips commented lines."""
    caddyfile = tmp_path / ".fujin" / "Caddyfile"
    caddyfile.parent.mkdir(parents=True)
    caddyfile.write_text("""
# old-domain.com {
example.com {
    reverse_proxy localhost:8000
}
""")
    assert config.get_domain_name() == "example.com"


def test_get_domain_name_skips_global_options(config, tmp_path):
    """Skips global options block."""
    caddyfile = tmp_path / ".fujin" / "Caddyfile"
    caddyfile.parent.mkdir(parents=True)
    caddyfile.write_text("""
{
    email admin@example.com
}

example.com {
    reverse_proxy localhost:8000
}
""")
    assert config.get_domain_name() == "example.com"


def test_get_domain_name_handles_multiple_domains_on_line(config, tmp_path):
    """
    Extracts first domain from comma-separated list.
    """
    caddyfile = tmp_path / ".fujin" / "Caddyfile"
    caddyfile.parent.mkdir(parents=True)
    caddyfile.write_text("""
example.com, www.example.com {
    reverse_proxy localhost:8000
}
""")
    # Should now return just the first domain
    assert config.get_domain_name() == "example.com"


def test_get_domain_name_ignores_blocks_without_dot(config, tmp_path):
    """Ignores blocks that don't look like domains (no dot)."""
    caddyfile = tmp_path / ".fujin" / "Caddyfile"
    caddyfile.parent.mkdir(parents=True)
    caddyfile.write_text("""
localhost {
    respond "Hello"
}

example.com {
    reverse_proxy localhost:8000
}
""")
    # "localhost" has no dot, so it should be skipped by the heuristic
    assert config.get_domain_name() == "example.com"


def test_get_domain_name_ignores_matchers(config, tmp_path):
    """Ignores named matchers that might look like domains."""
    caddyfile = tmp_path / ".fujin" / "Caddyfile"
    caddyfile.parent.mkdir(parents=True)
    caddyfile.write_text("""
example.com {
    @static {
        file
        path *.ico *.css *.js
    }
    reverse_proxy localhost:8000
}
""")
    assert config.get_domain_name() == "example.com"
