"""Aware time handling: phone-local timestamps are America/Bogota."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BOGOTA = ZoneInfo("America/Bogota")


def get_app_timezone() -> ZoneInfo:
    return BOGOTA


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return datetime.fromtimestamp(int(value) / 1000, UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=BOGOTA) if parsed.tzinfo is None else parsed
    except (ValueError, OverflowError, OSError):
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=BOGOTA)
            except ValueError:
                continue
        return None
