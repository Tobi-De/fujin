import re
from typing import Any


def safe_format(template: str, **kwargs: Any) -> tuple[str, set[str]]:
    """
    Format a string using {key} placeholders, but ignore braces that don't match keys.
    This allows files (like Caddyfiles) to use { } for blocks while still supporting
    variable substitution.

    Only single braces are supported: {var_name}
    Double braces {{}} are treated as literal braces.

    Args:
        template: The string to format
        **kwargs: The variables to substitute

    Returns:
        A tuple of (formatted_string, unresolved_variables)
    """
    unresolved = set()

    def replace(match):
        key = match.group(1)
        if key in kwargs:
            return str(kwargs[key])
        # Track variables that look like they should be substituted but weren't
        # (alphanumeric + underscore, typically our variable names)
        if re.match(r"^[a-z][a-z0-9_]*$", key):
            unresolved.add(key)
        return match.group(0)

    # Match {identifier} where identifier is alphanumeric/underscore
    # Only single braces - double braces are literal
    result = re.sub(r"(?<!\{)\{([a-zA-Z0-9_]+)\}(?!\})", replace, template)
    return result, unresolved
