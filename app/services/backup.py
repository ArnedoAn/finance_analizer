"""Backup status read from the JSON file written atomically by infra/backup/backup.sh (contract sections 11 and 14)."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.timezone import now_utc

STALE_AFTER = timedelta(hours=24)
FILE_STATUSES = frozenset({"ok", "failed", "running"})
MAX_DETAIL = 500


def _parse(value) -> datetime | None:
    """Accept ISO-8601 strings (naive = UTC) or epoch seconds; anything else is None."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip())
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def read_status(settings, now: datetime | None = None) -> dict:
    """Return ``{status, last_run_at, last_success_at, detail}``; never raises.

    A missing, unreadable or malformed file is ``unknown``. A file reporting ``ok`` whose last
    success is older than 24 hours (or absent) is ``stale``; ``failed`` and ``running`` are kept as is.
    """
    now = now or now_utc()
    unknown = {"status": "unknown", "last_run_at": None, "last_success_at": None, "detail": "Archivo de estado no disponible"}
    try:
        data = json.loads(Path(settings.backup_status_file).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return unknown
    if not isinstance(data, dict) or not isinstance(data.get("status"), str) or data["status"] not in FILE_STATUSES:
        return unknown
    status = data["status"]
    last_run, last_success = _parse(data.get("last_run_at")), _parse(data.get("last_success_at"))
    detail = data.get("detail")
    detail = detail[:MAX_DETAIL] if isinstance(detail, str) and detail else None
    if status == "ok" and (last_success is None or now - last_success > STALE_AFTER):
        status = "stale"
        detail = detail or "Sin respaldo exitoso en las últimas 24 horas"
    return {"status": status, "last_run_at": _iso(last_run), "last_success_at": _iso(last_success), "detail": detail}
