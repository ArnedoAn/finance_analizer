"""
Timezone Utilities Module

Provides timezone-aware datetime functions for the application.
Default timezone is set to Colombia (America/Bogota).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings

# Datetime formats seen in AI responses and phone notification payloads.
_DATETIME_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
)


def get_app_timezone() -> ZoneInfo:
    """Get the application timezone from settings."""
    return ZoneInfo(get_settings().timezone)


def parse_datetime(value: str | None) -> datetime | None:
    """Parse a datetime string from a known set of formats. None if unparseable."""
    if not value:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
