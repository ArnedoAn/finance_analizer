"""Aware time handling: phone-local timestamps are America/Bogota."""
from datetime import UTC, datetime


def now_utc() -> datetime:
    return datetime.now(UTC)


