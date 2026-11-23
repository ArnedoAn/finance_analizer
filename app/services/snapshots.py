"""Instant dashboard: the month's figures that the app used to assemble against Firefly, stored in ``dashboard_snapshots``.

``compute`` builds one month the way ``ledger_insights.dart`` and the summary screen do, on the America/Bogota
calendar: totals by type, spending by category and account, top merchants, spending per day, 12-month cash flow,
budgets with their consumption and, for the current month, the analyzer forecast with the daily allowance. Withdrawals
are spending, deposits are income, transfers only count in ``totals``; only COP is summed. A section that Firefly
cannot answer (cash flow, budgets, forecast) is ``null`` and named in ``unavailable``; the ledger itself is required.

Recomputation is single-flight per month (``run``): a second request joins the running task, and a change that arrives
while one runs schedules exactly one more. Automatic triggers only exist after ``enable`` (the application lifespan):
``after_cycle`` (current month every cycle, previous month once a day) and ``touch`` (an item became ``created``, was
corrected or voided; debounced ``DEBOUNCE_SECONDS`` per month). Without ``enable`` they do nothing, so scripts and tests
never open background sessions by accident.
"""
import asyncio
import calendar
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import DashboardSnapshot
from app.services import reports

logger = get_logger(__name__)

VERSION = 1
STALE_AFTER = timedelta(hours=2)
PREVIOUS_MONTH_INTERVAL = timedelta(hours=24)
DEBOUNCE_SECONDS = 60
TOP_MERCHANTS = 8
CASHFLOW_MONTHS = 12
CONCURRENCY = 4
MONTH_PATTERN = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")
CENT = Decimal("0.01")
ZERO = Decimal(0)
NO_CATEGORY, NO_ACCOUNT, NO_MERCHANT = "Sin categoría", "Sin cuenta", "Sin comercio"

_opener = None  # () -> async context manager yielding (session, firefly, settings)
_inflight: dict[str, asyncio.Task] = {}
_rerun: set[str] = set()
_debounce: dict[str, asyncio.Task] = {}


def _money(value: Decimal) -> str:
    return f"{value.quantize(CENT, ROUND_HALF_UP):.2f}"


def valid_month(value: str) -> bool:
    """``YYYY-MM`` with a month 01-12 and a year from 2000."""
    return bool(MONTH_PATTERN.fullmatch(value or "")) and int(value[:4]) >= 2000


def month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def current_month(settings, now: datetime | None = None) -> str:
    return month_of((now or now_utc()).astimezone(ZoneInfo(settings.timezone)).date())


def previous_month(month: str) -> str:
    year, number = int(month[:4]), int(month[5:])
    return f"{year - 1:04d}-12" if number == 1 else f"{year:04d}-{number - 1:02d}"


def _bounds(month: str) -> tuple[date, date]:
    year, number = int(month[:4]), int(month[5:])
    return date(year, number, 1), date(year, number, calendar.monthrange(year, number)[1])


def _cop(split: dict) -> bool:
    return split.get("currency_code") in (None, "", "COP")


def _local_day(value, zone) -> date | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=zone)).astimezone(zone).date()


async def _optional(unavailable: list[str], name: str, work):
    try:
        return await work
    except Exception as exc:
        unavailable.append(name)
        logger.warning("snapshot_section_unavailable", section=name, error_type=type(exc).__name__)
        return None


async def _cashflow(firefly, month: str) -> list[dict]:
    months = [month]
    while len(months) < CASHFLOW_MONTHS:
        months.insert(0, previous_month(months[0]))
    gate = asyncio.Semaphore(CONCURRENCY)

    async def total(kind: str, name: str) -> Decimal:
        async with gate:
            return await firefly.insight_total(kind, *_bounds(name))

    jobs = [total(kind, name) for name in months for kind in ("expense", "income")]
    results = await asyncio.gather(*jobs, return_exceptions=True)  # no sibling is left running with an unread error
    for result in results:
        if isinstance(result, BaseException):
            raise result
    rows = list(zip(results[0::2], results[1::2], strict=True))
    return [{"month": name, "spent": _money(spent), "income": _money(income)} for name, (spent, income) in zip(months, rows, strict=True)]


async def _budgets(firefly, first: date, last: date) -> list[dict]:
    rows = []
    for row in await firefly.budget_progress(first, last):
        spent, limit = Decimal(row["spent"]), Decimal(row["limit"])
        rows.append({"name": row["name"], "spent": _money(spent), "limit": _money(limit), "pct": int(spent * 100 / limit) if limit > 0 else 0})
    return rows


async def compute(session, firefly, settings, month: str, now: datetime | None = None) -> dict:
    """The snapshot ``data`` of ``month`` (see the module docstring). Raises when Firefly cannot give the movements."""
    zone = ZoneInfo(settings.timezone)
    today = (now or now_utc()).astimezone(zone).date()
    first, last = _bounds(month)
    as_of = min(max(today, first - timedelta(days=1)), last)
    is_current = first <= today <= last
    splits = await firefly.period_splits(first, last)

    totals = {"withdrawal": ZERO, "deposit": ZERO, "transfer": ZERO}
    days = {first + timedelta(days=i): [ZERO, ZERO] for i in range((last - first).days + 1)}
    categories: dict[str, list] = {}
    accounts: dict[str, Decimal] = {}
    merchants: dict[str, list] = {}
    for split in splits:
        kind = split.get("type")
        day = _local_day(split.get("date"), zone)
        if kind not in totals or day is None or not _cop(split) or not first <= day <= min(last, as_of):
            continue
        amount = abs(Decimal(str(split.get("amount") or 0)))
        totals[kind] += amount
        if kind == "withdrawal":
            days[day][0] += amount
            for table, key in ((categories, (split.get("category_name") or "").strip() or NO_CATEGORY),
                               (merchants, (split.get("destination_name") or "").strip() or NO_MERCHANT)):
                entry = table.setdefault(key, [ZERO, 0])
                entry[0] += amount
                entry[1] += 1
            name = (split.get("source_name") or "").strip() or NO_ACCOUNT
            accounts[name] = accounts.get(name, ZERO) + amount
        elif kind == "deposit":
            days[day][1] += amount

    def ranked(table, label):
        return [{label: key, "spent": _money(value[0]), "count": value[1]} for key, value in sorted(table.items(), key=lambda pair: (-pair[1][0], pair[0]))]

    unavailable: list[str] = []
    cashflow = await _optional(unavailable, "cashflow", _cashflow(firefly, month))
    budgets = await _optional(unavailable, "budgets", _budgets(firefly, first, last))
    forecast = await _optional(unavailable, "forecast", reports.forecast(session, settings, today)) if is_current else None
    spent, income = totals["withdrawal"], totals["deposit"]
    merchants_ranked = ranked(merchants, "merchant")
    return {
        "month": month, "as_of": as_of.isoformat(), "currency": "COP", "is_current": is_current,
        "totals": {kind: _money(value) for kind, value in totals.items()},
        "spent": _money(spent), "income": _money(income), "balance": _money(income - spent),
        "by_category": ranked(categories, "category"),
        "by_account": [{"account": key, "spent": _money(value)} for key, value in sorted(accounts.items(), key=lambda pair: (-pair[1], pair[0]))],
        "top_merchants": merchants_ranked[:TOP_MERCHANTS],
        "daily": [{"date": day.isoformat(), "spent": _money(values[0]), "income": _money(values[1])} for day, values in days.items()],
        "cashflow": cashflow, "budgets": budgets, "forecast": forecast,
        "daily_available": forecast["daily_available"] if forecast else None,
        "unavailable": unavailable,
    }


async def get(session, month: str) -> DashboardSnapshot | None:
    return await session.get(DashboardSnapshot, month, populate_existing=True)


def is_stale(row: DashboardSnapshot, now: datetime | None = None) -> bool:
    return row.version != VERSION or (now or now_utc()) - row.computed_at > STALE_AFTER


async def store(session, month: str, data: dict) -> DashboardSnapshot:
    """Upsert the month's row. Never commits."""
    row = await session.get(DashboardSnapshot, month, populate_existing=True)
    if row is None:
        row = DashboardSnapshot(month=month)
        session.add(row)
    row.data, row.computed_at, row.version = data, now_utc(), VERSION
    await session.flush()
    return row


def public(row: DashboardSnapshot, now: datetime | None = None) -> dict:
    return {"month": row.month, "computed_at": row.computed_at, "stale": is_stale(row, now), "data": row.data}


# ---- single-flight recomputation -------------------------------------------------------------

@asynccontextmanager
async def _fallback(resources):
    yield resources


async def _work(month: str, fallback) -> bool:
    try:
        async with (_opener() if _opener is not None else _fallback(fallback)) as (session, firefly, settings):
            data = await compute(session, firefly, settings, month)
            await store(session, month, data)
            await session.commit()
        return True
    except Exception as exc:
        # The error log's logging handler stores this ERROR record.
        logger.error("snapshot_recompute_failed", month=month, error_type=type(exc).__name__)
        return False


async def _guard(month: str, fallback) -> bool:
    try:
        ok = await _work(month, fallback)
        while month in _rerun:
            _rerun.discard(month)
            ok = await _work(month, fallback)
        return ok
    finally:
        _inflight.pop(month, None)


def run(month: str, fallback=None, *, again: bool = False) -> asyncio.Task:
    """Recompute ``month`` in a task; join the one already running. ``again`` asks for one more pass after it
    (something changed while it ran). ``fallback`` is ``(session, firefly, settings)`` when ``enable`` was not called.
    The task's result is True when the snapshot was stored."""
    task = _inflight.get(month)
    if task is not None and not task.done():
        if again:
            _rerun.add(month)
        return task
    task = asyncio.get_running_loop().create_task(_guard(month, fallback))
    _inflight[month] = task
    return task


def enable(opener) -> None:
    """Allow background recomputation; ``opener()`` yields ``(session, firefly, settings)`` and commits on exit."""
    global _opener
    _opener = opener


async def disable() -> None:
    global _opener
    _opener = None
    pending = [*_debounce.values(), *_inflight.values()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    _debounce.clear()
    _inflight.clear()
    _rerun.clear()


def schedule(month: str) -> None:
    if _opener is not None:
        run(month)


async def after_cycle(session, settings, now: datetime | None = None) -> None:
    """End of every cycle: refresh the current month; the previous one when its snapshot is a day old or missing."""
    if _opener is None:
        return
    now = now or now_utc()
    month = current_month(settings, now)
    run(month)
    before = previous_month(month)
    row = await get(session, before)
    if row is None or row.version != VERSION or now - row.computed_at > PREVIOUS_MONTH_INTERVAL:
        run(before)


async def _after_delay(month: str) -> None:
    await asyncio.sleep(DEBOUNCE_SECONDS)
    _debounce.pop(month, None)
    run(month, again=True)


def touch(month: str, settings, now: datetime | None = None) -> None:
    """Something that the month's figures show changed: recompute it once the changes stop for ``DEBOUNCE_SECONDS``.
    Only the current and the previous month are tracked; older ones refresh on demand."""
    if _opener is None:
        return
    current = current_month(settings, now)
    if month not in (current, previous_month(current)):
        return
    waiting = _debounce.pop(month, None)
    if waiting is not None:
        waiting.cancel()
    _debounce[month] = asyncio.get_running_loop().create_task(_after_delay(month))


def touch_item(item, settings) -> None:
    """``touch`` for the month of an inbox item's movement date."""
    if item.occurred_at is not None:
        touch(month_of(item.occurred_at.astimezone(ZoneInfo(settings.timezone)).date()), settings)

