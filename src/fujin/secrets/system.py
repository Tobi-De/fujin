from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator
from typing import TYPE_CHECKING

from fujin.config import SecretConfig

if TYPE_CHECKING:
    from . import secret_reader


@contextmanager
def system(_: SecretConfig) -> Generator[secret_reader, None, None]:
    try:
        yield os.getenv
    finally:
        pass
