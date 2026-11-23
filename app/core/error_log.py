"""Persistent buffer of server errors, readable from the app (``GET /system/errors``).

Everything that fails on the server (cycle, mailbox, LLM, Firefly, backup, unhandled route exceptions) is
recorded as one row of ``server_errors``: moment, component, operation, message, exception type, HTTP status of
the external service and its response body (cut to 4 KB). The table keeps the last ``MAX_ROWS`` rows and at most
``RETENTION`` of history.

Two ways in, both through ``record``: explicit calls in the clients (``FireflyClient``, ``LLMClient``,
``IMAPClient``), the scheduler and the unhandled-exception handler, and ``ErrorLogHandler``, a logging handler for
every ERROR record. ``record`` is synchronous, never raises and sanitizes immediately, so a secret is never held
in memory in the pending queue; rows are written in the background by ``flush``.

Sanitizing (``sanitize``): configured secrets (API keys, tokens, mail passwords) and anything that looks like a
credential are masked, ``redact`` removes personal data, literals the caller says were sent (``scrub``: e.g. the
text given to the LLM, which a provider may echo) are removed, and quoted fragments longer than
``MAX_QUOTED`` characters (what an exception message uses to quote a raw message) are omitted.

Nothing is stored until ``install`` supplies a session opener (``app.main`` does it at startup), so tests and
scripts never write errors anywhere by accident.
"""
import asyncio
import logging
import re
from collections import deque
from datetime import UTC, datetime, timedelta

from app.core.timezone import now_utc

MAX_ROWS = 300
RETENTION = timedelta(days=30)
MAX_BODY = 4096
MAX_MESSAGE = 500
MAX_QUOTED = 80
COMPONENTS = ("cycle", "mailbox", "llm", "firefly", "backup", "api", "server")

_RAW_BODY_LIMIT = 65536
_MIN_SECRET = 4
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 [redacted]"),
    (re.compile(
        r"(?i)\b(authorization|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|pwd|clave|contrase[ñn]a)"
        r"([\"']?\s*[:=]\s*)([\"']?)[^\s\"',;&}]+"
    ), r"\1\2\3[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"), "[redacted]"),
    (re.compile(r"\b(?:sk|cofre)[-_][A-Za-z0-9_-]{12,}"), "[redacted]"),
    (re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}"), "[redacted]"),
)
_QUOTED = re.compile(rf"""(["'])((?:(?!\1).){{{MAX_QUOTED + 1},}})\1""", re.S)
_IGNORED_LOGGERS = ("app.core.error_log", "sqlalchemy", "aiosqlite", "asyncpg")
_EVENT_SKIP = frozenset({"event", "exc_info", "level", "logger", "timestamp", "app", "env", "stack"})

_pending: deque[dict] = deque(maxlen=MAX_ROWS)
_opener = None
_loop: asyncio.AbstractEventLoop | None = None
_autoflush = True
_handler: "ErrorLogHandler | None" = None
_flush_task: asyncio.Task | None = None
_logger = logging.getLogger("app.core.error_log")


def _secrets() -> list[str]:
    try:
        from app.core.config import get_settings

        settings = get_settings()
        values = [
            settings.finance_api_key, settings.phone_api_key, settings.firefly_api_token, settings.llm_api_key,
            settings.llm_fallback_api_key, settings.field_encryption_key,
        ]
        found = [value.get_secret_value() for value in values if value is not None]
        for account in settings.mail_accounts:
            found += [account.password.get_secret_value(), account.username]
    except Exception:
        return []
    return sorted({value for value in found if value and len(value) >= _MIN_SECRET}, key=len, reverse=True)


def sanitize(text, limit: int = MAX_MESSAGE, scrub=()) -> str:
    """Safe, bounded text for the buffer (see the module docstring)."""
    from app.security.redaction import redact

    value = str(text if text is not None else "")[:_RAW_BODY_LIMIT]
    for literal in sorted((str(s) for s in scrub if s and len(str(s)) >= 8), key=len, reverse=True):
        for form in {literal, literal.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")}:
            value = value.replace(form, "[omitido]")
    for secret in _secrets():
        value = value.replace(secret, "[redacted]")
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    value = _QUOTED.sub(lambda match: f"{match.group(1)}[texto omitido]{match.group(1)}", value)
    return (redact(value) or "")[:limit]


def component_for(logger_name: str) -> str:
    if logger_name.startswith("app.clients.firefly"):
        return "firefly"
    if logger_name.startswith("app.clients.llm"):
        return "llm"
    if logger_name.startswith("app.clients.imap"):
        return "mailbox"
    if logger_name.startswith("app.services.backup"):
        return "backup"
    if logger_name.startswith("app.services.snapshots"):
        return "server"
    if logger_name.startswith(("app.services", "apscheduler")):
        return "cycle"
    if logger_name.startswith(("app.api", "app.main", "uvicorn", "fastapi", "starlette")):
        return "api"
    return "server"


def record(component: str, operation: str, message: str = "", *, exc: BaseException | None = None,
           status_code: int | None = None, body=None, exc_type: str | None = None, scrub=()) -> None:
    """Queue one error. Synchronous, thread-safe enough for the logging handler, and it never raises."""
    try:
        if _opener is None:
            return
        name = exc_type or (type(exc).__name__ if exc is not None else None)
        text = message or (str(exc) if exc is not None else "")
        _pending.append({
            "at": now_utc(),
            "component": component if component in COMPONENTS else "server",
            "operation": sanitize(operation, 100, scrub) or "unknown",
            "message": sanitize(text, MAX_MESSAGE, scrub),
            "exc_type": re.sub(r"[^\w.]", "", name)[:100] if name else None,
            "status_code": status_code if isinstance(status_code, int) and not isinstance(status_code, bool) else None,
            "response_body": sanitize(body, MAX_BODY, scrub) if body not in (None, "") else None,
        })
        _schedule()
    except Exception:  # recording an error must never produce another one
        pass


def _schedule() -> None:
    if not _autoflush or _opener is None:
        return
    loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _start()
    elif loop.is_running():
        loop.call_soon_threadsafe(_start)


def _start() -> None:
    global _flush_task
    if _flush_task is None or _flush_task.done():
        _flush_task = asyncio.get_running_loop().create_task(_drain())


async def _drain() -> None:
    while _pending:
        if not await flush():
            break


async def flush() -> bool:
    """Write the queued rows. Returns False when nothing could be written (they are dropped, never retried forever)."""
    if _opener is None or not _pending:
        return True
    from app.db.models import ServerError

    rows = []
    while _pending:
        rows.append(_pending.popleft())
    try:
        async with _opener() as session:
            session.add_all([ServerError(**row) for row in rows])
            await session.flush()
            await trim(session)
            await session.commit()
        return True
    except Exception as exc:
        _logger.warning("error_log_write_failed: %s", type(exc).__name__)
        return False


async def trim(session) -> int:
    """Keep the newest ``MAX_ROWS`` rows. Never commits."""
    from sqlalchemy import delete, select

    from app.db.models import ServerError

    cutoff = await session.scalar(select(ServerError.id).order_by(ServerError.id.desc()).offset(MAX_ROWS).limit(1))
    if cutoff is None:
        return 0
    return (await session.execute(delete(ServerError).where(ServerError.id <= cutoff))).rowcount or 0


async def purge(session, now: datetime | None = None) -> int:
    """Delete rows older than ``RETENTION`` and trim to ``MAX_ROWS``. Never commits."""
    from sqlalchemy import delete

    from app.db.models import ServerError

    cutoff = (now or now_utc()) - RETENTION
    removed = (await session.execute(delete(ServerError).where(ServerError.at < cutoff))).rowcount or 0
    return removed + await trim(session)


async def entries(session, *, since: datetime | None = None, component: str | None = None, limit: int = 50, offset: int = 0):
    """Newest first: ``(rows, total)``."""
    from sqlalchemy import func, select

    from app.db.models import ServerError

    query = select(ServerError)
    if since is not None:
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        query = query.where(ServerError.at >= since)
    if component:
        query = query.where(ServerError.component == component)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(ServerError.id.desc()).limit(limit).offset(offset))
    return list(rows.all()), total


def error_data(row) -> dict:
    return {
        "id": row.id, "at": row.at, "component": row.component, "operation": row.operation, "message": row.message,
        "exc_type": row.exc_type, "status_code": row.status_code, "response_body": row.response_body,
    }


class ErrorLogHandler(logging.Handler):
    """Records every ERROR log record (structlog events arrive as a dict message)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record_: logging.LogRecord) -> None:
        try:
            if record_.name.startswith(_IGNORED_LOGGERS):
                return
            exc_type = record_.exc_info[0].__name__ if record_.exc_info and record_.exc_info[0] else None
            if isinstance(record_.msg, dict):
                event = record_.msg
                name = str(event.get("event", "error"))
                fields = " ".join(
                    f"{key}={value}" for key, value in event.items()
                    if key not in _EVENT_SKIP and not key.startswith("_") and isinstance(value, str | int | float | bool)
                )
                exc_type = exc_type or (str(event["error_type"]) if event.get("error_type") else None)
                operation, message = name, f"{name} {fields}".strip()
            else:
                message = record_.getMessage()
                operation = record_.funcName or "log"
            record(component_for(record_.name), operation, message, exc_type=exc_type)
        except Exception:
            pass


def install(opener, *, autoflush: bool = True) -> None:
    """Start storing. ``opener()`` returns an async context manager yielding a session that commits on exit."""
    global _opener, _loop, _autoflush, _handler
    _opener, _autoflush = opener, autoflush
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        _loop = None
    if _handler is None:
        _handler = ErrorLogHandler()
        logging.getLogger().addHandler(_handler)


async def uninstall() -> None:
    """Write what is pending and stop storing."""
    global _opener, _loop, _handler, _flush_task
    task = _flush_task
    if task is not None and not task.done():
        await asyncio.gather(task, return_exceptions=True)
    await flush()
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
    _opener = _loop = _handler = _flush_task = None
    _pending.clear()
