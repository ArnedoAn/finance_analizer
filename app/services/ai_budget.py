"""Budget guard for the server's LLM (OpenCode Go): counts calls per day and kind and brakes automatic AI jobs.

Rules (see ``docs/contract-additions/ServerAiReview.md``):

* Every LLM call is recorded with ``record`` (automatic jobs and on-demand owner tasks alike), in ``ai_usage``
  per (day in the server timezone, kind). ``auto_calls`` is the part made by automatic jobs.
* Only automatic jobs ask ``allow`` first. It is False when today's automatic calls plus the estimate exceed the
  daily limit (policy ``ai_budget`` in ``runtime_flags``, default ``DEFAULT_DAILY_CALLS``), or when OpenCode Go
  reports ``rolling.percent >= ROLLING_STOP_PERCENT`` or ``weekly.percent >= WEEKLY_STOP_PERCENT``.
  Normal message processing never calls ``allow``: it must not stop for this budget.
* The Go usage (``GET https://opencode.ai/inference/go/v1/usage`` with the server's LLM key) is fetched at most every
  ``GO_REFRESH`` and stored in ``runtime_flags`` (``ai_go_usage``) so it survives restarts. A failed or unconfigured
  check never blocks by itself (the daily limit still applies) and is not retried before the interval passes.

Functions flush but never commit; the caller owns the transaction.
"""
import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import AiUsage
from app.db.models_flags import RuntimeFlag

logger = get_logger(__name__)

POLICY_KEY = "ai_budget"
GO_USAGE_KEY = "ai_go_usage"
DEFAULT_DAILY_CALLS = 400
MAX_DAILY_CALLS = 100_000
ROLLING_STOP_PERCENT = 60.0
WEEKLY_STOP_PERCENT = 70.0
GO_USAGE_URL = "https://opencode.ai/inference/go/v1/usage"
GO_REFRESH = timedelta(minutes=15)

_refresh_lock = asyncio.Lock()


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def today() -> date:
    return now_utc().astimezone(_zone()).date()


async def _flag(session, key: str) -> dict | None:
    return await session.scalar(select(RuntimeFlag.value).where(RuntimeFlag.key == key))


async def _write_flag(session, key: str, value: dict) -> None:
    row = await session.get(RuntimeFlag, key, populate_existing=True)
    if row is None:
        session.add(RuntimeFlag(key=key, value=value))
    else:
        row.value = value
    await session.flush()


# --- policy ---------------------------------------------------------------------------------------------------------


async def policy(session) -> dict:
    """``{"daily_calls": int}``: the daily limit of AUTOMATIC calls (0 stops every automatic AI job)."""
    stored = await _flag(session, POLICY_KEY)
    calls = stored.get("daily_calls") if stored else None
    if isinstance(calls, int) and not isinstance(calls, bool) and 0 <= calls <= MAX_DAILY_CALLS:
        return {"daily_calls": calls}
    return {"daily_calls": DEFAULT_DAILY_CALLS}


async def set_policy(session, daily_calls: int) -> dict:
    """Store the policy (the caller validates 0 <= daily_calls <= MAX_DAILY_CALLS, audits and commits)."""
    await _write_flag(session, POLICY_KEY, {"daily_calls": int(daily_calls)})
    return {"daily_calls": int(daily_calls)}


# --- Go usage -------------------------------------------------------------------------------------------------------


def _percent(data, window: str) -> float | None:
    block = data.get(window) if isinstance(data, dict) else None
    value = block.get("percent") if isinstance(block, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


async def fetch_go_usage(settings) -> dict:
    """The raw ``usage`` document of OpenCode Go for the server's LLM key (raises on any failure)."""
    key = settings.llm_api_key.get_secret_value()
    if not key:
        raise RuntimeError("no LLM key")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(GO_USAGE_URL, headers={"Authorization": f"Bearer {key}", "User-Agent": settings.llm_user_agent})
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def _aware(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


async def go_usage(session, *, refresh: bool = True) -> dict | None:
    """The last Go usage as ``{"fetched_at": iso, "data": dict | None, "error": str | None}`` (None: never checked).

    With ``refresh`` the document is fetched again when the stored one is older than ``GO_REFRESH`` (and the server has an
    LLM key). A failure keeps the previous ``data`` and records the attempt, so it is not retried for 15 minutes.
    """
    stored = await _flag(session, GO_USAGE_KEY)
    fetched = _aware(stored.get("fetched_at")) if stored else None
    if not refresh or (fetched is not None and now_utc() - fetched < GO_REFRESH):
        return stored
    settings = get_settings()
    if not settings.llm_api_key.get_secret_value():
        return stored
    async with _refresh_lock:
        stored = await _flag(session, GO_USAGE_KEY)
        fetched = _aware(stored.get("fetched_at")) if stored else None
        if fetched is not None and now_utc() - fetched < GO_REFRESH:
            return stored  # another task refreshed while this one waited
        try:
            fresh = {"fetched_at": now_utc().isoformat(), "data": await fetch_go_usage(settings), "error": None}
        except Exception as exc:  # unreachable or malformed: never log the key or the body, only the class
            logger.warning("ai_go_usage_failed", error_type=type(exc).__name__)
            fresh = {"fetched_at": now_utc().isoformat(), "data": stored.get("data") if stored else None, "error": type(exc).__name__}
        await _write_flag(session, GO_USAGE_KEY, fresh)
        return fresh


def go_brake(usage: dict | None) -> str | None:
    """``"go_rolling"`` / ``"go_weekly"`` when the stored Go usage is over its stop threshold, else None."""
    data = usage.get("data") if usage else None
    rolling, weekly = _percent(data, "rolling"), _percent(data, "weekly")
    if rolling is not None and rolling >= ROLLING_STOP_PERCENT:
        return "go_rolling"
    if weekly is not None and weekly >= WEEKLY_STOP_PERCENT:
        return "go_weekly"
    return None


# --- counting -------------------------------------------------------------------------------------------------------


async def auto_calls_today(session) -> int:
    return int(await session.scalar(select(func.coalesce(func.sum(AiUsage.auto_calls), 0)).where(AiUsage.day == today())) or 0)


async def brake_reason(session, estimated_calls: int = 1, *, refresh: bool = True) -> str | None:
    """Why automatic AI jobs must not start now (``daily_limit``, ``go_rolling``, ``go_weekly``), or None."""
    limit = (await policy(session))["daily_calls"]
    if await auto_calls_today(session) + max(0, estimated_calls) > limit:
        return "daily_limit"
    return go_brake(await go_usage(session, refresh=refresh))


async def allow(session, kind: str, estimated_calls: int = 1) -> bool:
    """May an AUTOMATIC job of ``kind`` make ``estimated_calls`` more LLM calls? Never raises.

    Owner-requested work and normal message processing do not ask: they only ``record``.
    """
    try:
        reason = await brake_reason(session, estimated_calls)
    except Exception as exc:  # a broken guard must not take the automatic jobs down in a loop of errors; fail closed
        logger.error("ai_budget_check_failed", kind=kind, error_type=type(exc).__name__)
        return False
    if reason:
        logger.info("ai_budget_blocked", kind=kind, reason=reason)
    return reason is None


async def record(session, kind: str, calls: int, usage: dict | None = None, *, auto: bool = True) -> None:
    """Count ``calls`` LLM calls of ``kind`` today; ``usage`` is the dict LLMClient filled (tokens, ``allowance``).

    ``auto`` False marks an on-demand task of the owner: counted in ``calls`` but not against the automatic limit.
    """
    calls = max(0, int(calls))
    usage = usage or {}
    day = today()
    key = (AiUsage.day == day, AiUsage.kind == kind)
    if await session.scalar(select(AiUsage.id).where(*key)) is None:
        try:
            async with session.begin_nested():
                session.add(AiUsage(day=day, kind=kind, calls=0, auto_calls=0, tokens=0))
                await session.flush()
        except IntegrityError:
            pass  # a concurrent writer created today's row first
    tokens = usage.get("total_tokens")
    if not isinstance(tokens, int):
        tokens = sum(value for name in ("prompt_tokens", "completion_tokens") if isinstance(value := usage.get(name), int))
    values = {
        "calls": AiUsage.calls + calls, "auto_calls": AiUsage.auto_calls + (calls if auto else 0),
        "tokens": AiUsage.tokens + max(0, tokens), "updated_at": now_utc(),
    }
    if usage.get("allowance") is not None:
        values["allowance"] = usage["allowance"]
    await session.execute(update(AiUsage).where(*key).values(**values).execution_options(synchronize_session=False))
    await session.flush()


# --- reports --------------------------------------------------------------------------------------------------------


def _totals(rows) -> dict:
    by_kind: dict[str, dict] = {}
    for row in rows:
        entry = by_kind.setdefault(row.kind, {"calls": 0, "auto_calls": 0, "tokens": 0})
        entry["calls"] += row.calls
        entry["auto_calls"] += row.auto_calls
        entry["tokens"] += row.tokens
    return {
        "calls": sum(entry["calls"] for entry in by_kind.values()),
        "auto_calls": sum(entry["auto_calls"] for entry in by_kind.values()),
        "tokens": sum(entry["tokens"] for entry in by_kind.values()),
        "by_kind": dict(sorted(by_kind.items())),
    }


async def usage_report(session) -> dict:
    """Body of ``GET /ai/usage``: today and this month by kind, the last Go usage and whether automatic jobs are braked."""
    day = today()
    month_start = day.replace(day=1)
    rows = list((await session.scalars(select(AiUsage).where(AiUsage.day >= month_start, AiUsage.day <= day).order_by(AiUsage.day, AiUsage.kind))).all())
    today_rows = [row for row in rows if row.day == day]
    limit = (await policy(session))["daily_calls"]
    go = await go_usage(session)
    reason = await brake_reason(session, 1, refresh=False)
    allowances = [row for row in sorted(rows, key=lambda r: r.updated_at) if row.allowance is not None]
    return {
        "today": {"day": day.isoformat(), **_totals(today_rows)},
        "month": {"month": day.strftime("%Y-%m"), **_totals(rows)},
        "daily_limit": limit,
        "go_usage": go,
        "allowance": allowances[-1].allowance if allowances else None,
        "throttled": reason is not None,
        "throttle_reason": reason,
    }
