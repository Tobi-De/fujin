import re
from typing import Any


def safe_format(template: str, **kwargs: Any) -> str:
    """
    Format a string using {key} placeholders, but ignore braces that don't match keys.
    This allows files (like Caddyfiles) to use { } for blocks while still supporting
    variable substitution.

    Args:
        template: The string to format
        **kwargs: The variables to substitute

    Returns:
        The formatted string
    """

    def replace(match):
        key = match.group(1)
        if key in kwargs:
            return str(kwargs[key])
        return match.group(0)

    # Match {identifier} or {{identifier}} where identifier is alphanumeric/underscore
    # We strip all surrounding braces and replace with the value
    return re.sub(r"\{+([a-zA-Z0-9_]+)\}+", replace, template)
