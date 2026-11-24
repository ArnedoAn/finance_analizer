"""TRM (COP per USD): cache in ``fx_rates``, fallback to the last known rate, and the conversion of USD movements.

The official source is datos.gov.co (``app.clients.trm``). Reads hit the cache first (a stored rate governs every day
from ``valid_from`` to ``valid_until``), then the service, and only when the service fails or has nothing for the day do they
fall back to the newest rate known on or before the day, marked ``stale``. The daily job (``refresh``) stores the latest
rates ahead of the demand. Functions never commit; callers own the transaction.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.clients.trm import TrmClient, TrmRate
from app.core.exceptions import TrmError
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models_debts import FxRate

logger = get_logger(__name__)

SOURCE = "datos.gov.co"
# A USD movement is converted with a rate at most this many days older than the movement (a long weekend plus a holiday).
MAX_BACK_DAYS = 5
CENTS = Decimal("0.01")


def new_client(settings) -> TrmClient:
    """The HTTP client of one operation; tests replace this function with one over a mock transport."""
    return TrmClient(settings.trm_base_url, settings.trm_timeout_seconds)


@dataclass(frozen=True)
class Quote:
    day: date  # the day asked for
    rate: Decimal
    fetched_at: datetime
    stale: bool
    valid_from: date
    valid_until: date


def _quote(day: date, row: FxRate, stale: bool) -> Quote:
    return Quote(day, row.rate, row.fetched_at, stale, row.valid_from, row.valid_until)


async def _store(session, rate: TrmRate) -> FxRate:
    row = await session.get(FxRate, rate.valid_from, populate_existing=True)
    if row is None:
        row = FxRate(valid_from=rate.valid_from)
        session.add(row)
    row.valid_until, row.rate, row.source, row.fetched_at = rate.valid_until, rate.rate, SOURCE, now_utc()
    await session.flush()
    return row


async def _covering(session, day: date) -> FxRate | None:
    return await session.scalar(select(FxRate).where(FxRate.valid_from <= day, FxRate.valid_until >= day).order_by(FxRate.valid_from.desc()).limit(1))


async def _latest_before(session, day: date) -> FxRate | None:
    return await session.scalar(select(FxRate).where(FxRate.valid_from <= day).order_by(FxRate.valid_from.desc()).limit(1))


async def quote(session, settings, day: date, max_back_days: int | None = None) -> Quote | None:
    """The TRM of ``day``: cached, else fetched and cached, else the newest older one (``stale``), else None.

    ``max_back_days`` limits how old the stale fallback may be (days between its last valid day and ``day``); without it the
    newest known rate is used, however old, and only an empty cache gives None.
    """
    cached = await _covering(session, day)
    if cached is not None:
        return _quote(day, cached, False)
    client = new_client(settings)
    try:
        found = await client.for_date(day)
        if found is not None:
            return _quote(day, await _store(session, found), False)
        # The dataset has nothing for that day (not yet published): learn the recent rates so the previous one is known.
        if await _latest_before(session, day) is None:
            for rate in await client.latest(15):
                await _store(session, rate)
    except TrmError as exc:
        logger.warning("trm_fetch_failed", error_type=type(exc).__name__)
    finally:
        await client.close()
    older = await _latest_before(session, day)
    if older is None and max_back_days is None:
        older = await session.scalar(select(FxRate).order_by(FxRate.valid_from.desc()).limit(1))  # every known rate is newer than the day: the last known one
    if older is None or (max_back_days is not None and (day - older.valid_until).days > max_back_days):
        return None
    return _quote(day, older, True)


async def refresh(session, settings) -> dict:
    """Daily job: store the latest published rates. A failure is reported, never raised (the cache keeps serving)."""
    client = new_client(settings)
    try:
        rates = await client.latest(15)
    except TrmError as exc:
        logger.warning("trm_refresh_failed", error_type=type(exc).__name__)
        return {"stored": 0, "error": "unavailable"}
    finally:
        await client.close()
    for rate in rates:
        await _store(session, rate)
    await session.commit()
    return {"stored": len(rates), "latest": rates[0].valid_from.isoformat() if rates else None}


@dataclass(frozen=True)
class Converted:
    cop: Decimal
    rate: Decimal
    rate_day: date  # first day the rate governs
    stale: bool


async def to_cop(session, settings, usd: Decimal, day: date) -> Converted | None:
    """``usd`` in COP at the TRM of ``day`` (or the newest of the days before, up to ``MAX_BACK_DAYS``); None without one."""
    found = await quote(session, settings, day, max_back_days=MAX_BACK_DAYS)
    if found is None:
        return None
    return Converted((Decimal(usd) * found.rate).quantize(CENTS), found.rate, found.valid_from, found.stale)
