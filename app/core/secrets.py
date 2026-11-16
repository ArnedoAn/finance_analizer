"""Read mounted secrets without exposing their paths or contents in errors."""
import os
from pathlib import Path


def read_secret(name: str) -> str | None:
    """Prefer NAME_FILE over NAME; strip only a mounted file's trailing newline.

    An unreadable configured file fails closed, never falls back to an old env key.
    """
    filename = os.environ.get(name + "_FILE")
    if filename:
        try:
            return Path(filename).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError, ValueError):
            raise ValueError(f"Cannot read secret {name}_FILE") from None
    return os.environ.get(name)
