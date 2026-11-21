"""Rule-based anomaly detection over the inbox (Phase 5 A6, contract sections 8 and 14).

Plain statistics, no LLM: unusual amounts per category, first-seen merchants with a high
amount, duplicate charges seen through two sources, and subscription price increases.
Every entry has a ``key`` that is stable across runs so alerts can be deduplicated.
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import fmean, pstdev
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.timezone import now_utc
from app.db.models import InboxItem
from app.services.rules import merchant_name

HISTORY_DAYS = 90
MIN_SAMPLES = 5
DUPLICATE_MINUTES = 10
DEFAULT_SINCE_DAYS = 7
ALERT_SINCE_DAYS = 2
OPEN_STATUSES = ("created", "needs_review")
CENT = Decimal("0.01")


def _money(value) -> str:
    return format(Decimal(value).quantize(CENT), "f")


def _spanish(value, currency) -> str:
    """Full amount in Spanish format, e.g. ``12.345,00 COP``."""
    return f"{Decimal(value).quantize(CENT):,.2f}".translate(str.maketrans(",.", ".,")) + f" {currency}"


def _entry(kind, key, item, *, related=(), score, detail) -> dict:
    return {
        "key": key, "kind": kind, "item_id": item.id, "related_item_ids": list(related),
        "merchant": item.merchant, "category": item.category, "amount": _money(item.amount),
        "currency": item.currency, "occurred_at": item.occurred_at.isoformat(),
        "score": round(float(score), 2), "detail": detail,
    }


def _percentile_90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def _earlier(history: list[InboxItem], item: InboxItem) -> list[float]:
    """Amounts of ``history`` in the 90 days before ``item`` (never the item itself)."""
    start = item.occurred_at - timedelta(days=HISTORY_DAYS)
    return [float(h.amount) for h in history if h.id != item.id and start <= h.occurred_at < item.occurred_at]


def _unusual_amount(candidates, by_category):
    for item in candidates:
        if not item.category:
            continue
        samples = _earlier(by_category.get((item.category, item.currency), []), item)
        if len(samples) < MIN_SAMPLES:
            continue
        mean, std = fmean(samples), pstdev(samples)
        # Identical history has no deviation: fall back to 1% of the mean so any rise counts.
        spread = std or mean * 0.01
        if float(item.amount) > mean + 3 * spread:
            score = (float(item.amount) - mean) / spread
            detail = (f"{_spanish(item.amount, item.currency)} supera el gasto habitual en {item.category} "
                      f"(promedio {_spanish(Decimal(str(mean)), item.currency)}, {score:.1f} desviaciones)")
            yield _entry("unusual_amount", f"unusual_amount:{item.id}", item, score=score, detail=detail)


async def _first_seen(session, candidates) -> dict[str, int]:
    merchants = {item.merchant for item in candidates if item.merchant}
    if not merchants:
        return {}
    rows = await session.execute(select(InboxItem.id, InboxItem.merchant, InboxItem.occurred_at).where(
        InboxItem.merchant.in_(merchants), InboxItem.status.in_((*OPEN_STATUSES, "pending", "submitting"))))
    first: dict[str, tuple] = {}
    for item_id, merchant, occurred_at in rows:
        if merchant not in first or (occurred_at, item_id) < first[merchant]:
            first[merchant] = (occurred_at, item_id)
    return {merchant: key[1] for merchant, key in first.items()}


async def _new_merchant(session, candidates, by_category, everything):
    first = await _first_seen(session, [item for item in candidates if merchant_name(item.merchant)])
    for item in candidates:
        if not item.merchant or first.get(item.merchant) != item.id:
            continue
        # Uncategorized merchants are compared with every expense of the same currency.
        pool = by_category.get((item.category, item.currency), []) if item.category else everything.get(item.currency, [])
        samples = _earlier(pool, item)
        if len(samples) < MIN_SAMPLES:
            continue
        limit = _percentile_90(samples)
        if float(item.amount) > limit:
            detail = (f"Primer cobro de {item.merchant} por {_spanish(item.amount, item.currency)}, "
                      f"más alto que el 90 % de tus gastos ({_spanish(Decimal(str(limit)), item.currency)})")
            yield _entry("new_merchant", f"new_merchant:{item.id}", item, score=float(item.amount) / limit if limit else 0, detail=detail)


def _duplicate_charge(created, since):
    groups = defaultdict(list)
    for item in created:
        if item.merchant:
            groups[(item.merchant, item.amount, item.currency, item.transaction_type)].append(item)
    window = timedelta(minutes=DUPLICATE_MINUTES)
    for group in groups.values():
        group.sort(key=lambda i: (i.occurred_at, i.id))
        for index, later in enumerate(group):
            if later.occurred_at < since:
                continue
            for earlier in group[:index]:
                gap = later.occurred_at - earlier.occurred_at
                if gap <= window and earlier.source != later.source:
                    low, high = sorted((earlier.id, later.id))
                    detail = (f"Dos avisos ({earlier.source} y {later.source}) de {_spanish(later.amount, later.currency)} "
                              f"en {later.merchant} con {gap.total_seconds() / 60:.0f} min de diferencia, ambos registrados")
                    yield _entry("duplicate_charge", f"duplicate_charge:{low}:{high}", later, related=[earlier.id],
                                 score=1 - gap / window, detail=detail)


async def _price_increases(session, settings, since):
    try:
        from app.services import reports
    except ImportError:  # reports (A5) not installed: skip this kind
        return []
    zone = ZoneInfo(settings.timezone)
    found = []
    for sub in (await reports.subscriptions(session, settings))["items"]:
        if sub["status"] != "price_increase":
            continue
        occurred = datetime.fromisoformat(sub["last_date"]).replace(tzinfo=zone)
        if occurred < since:
            continue
        previous = Decimal(sub["previous_amount"])
        detail = (f"{sub['merchant']} subió de {_spanish(previous, sub['currency'])} a "
                  f"{_spanish(Decimal(sub['amount']), sub['currency'])} ({sub['change_pct']} %)")
        found.append({
            "key": f"price_increase:{sub['merchant']}:{sub['amount']}", "kind": "price_increase", "item_id": None,
            "related_item_ids": [], "merchant": sub["merchant"], "category": sub["category"],
            "amount": sub["amount"], "currency": sub["currency"], "occurred_at": occurred.isoformat(),
            "score": round(float(sub["change_pct"]), 2), "detail": detail,
        })
    return found


async def detect(session, settings, since=None) -> list[dict]:
    """Anomalies whose item occurred at or after ``since`` (default: last 7 days), newest first.

    - ``unusual_amount``: amount > mean + 3 standard deviations of the same category and currency
      over the 90 days before the item (at least 5 created expenses).
    - ``new_merchant``: first appearance of the merchant with an amount above the p90 of its
      category (all expenses when uncategorized), same sample rules.
    - ``duplicate_charge``: same merchant, amount and type within 10 minutes from different
      sources, both ``created``.
    - ``price_increase``: subscriptions flagged by ``reports.subscriptions``.
    """
    since = since or now_utc() - timedelta(days=DEFAULT_SINCE_DAYS)
    rows = (await session.scalars(select(InboxItem).where(
        InboxItem.status.in_(OPEN_STATUSES), InboxItem.amount.is_not(None),
        InboxItem.occurred_at >= since - timedelta(days=HISTORY_DAYS),
    ))).all()
    expenses = [item for item in rows if item.transaction_type == "withdrawal"]
    history = [item for item in expenses if item.status == "created"]
    by_category, everything = defaultdict(list), defaultdict(list)
    for item in history:
        by_category[(item.category, item.currency)].append(item)
        everything[item.currency].append(item)
    candidates = [item for item in expenses if item.occurred_at >= since]
    found = list(_unusual_amount(candidates, by_category))
    found.extend([entry async for entry in _new_merchant(session, candidates, by_category, everything)])
    found.extend(_duplicate_charge([item for item in rows if item.status == "created"], since))
    found.extend(await _price_increases(session, settings, since))
    found.sort(key=lambda entry: (entry["occurred_at"], entry["key"]), reverse=True)
    return found


_ALERTS = {
    "unusual_amount": ("anomaly", "Monto inusual en {merchant}"),
    "new_merchant": ("anomaly", "Comercio nuevo: {merchant}"),
    "duplicate_charge": ("duplicate_suspect", "Posible cobro duplicado en {merchant}"),
}


async def alert_check(session, settings, firefly=None) -> int:
    """Emit ``anomaly`` (``duplicate_suspect`` for duplicates) alerts for recent anomalies.

    ``dedupe_key`` is the anomaly key. ``price_increase`` is left to ``reports.alert_check``,
    which already alerts it. Returns the number of alerts emitted."""
    from app.services.alerts import ONCE_MINUTES, emit

    emitted = 0
    since = now_utc() - timedelta(days=ALERT_SINCE_DAYS)
    for entry in await detect(session, settings, since=since):
        if entry["kind"] not in _ALERTS:
            continue
        alert_type, title = _ALERTS[entry["kind"]]
        alert = await emit(
            session, alert_type, title=title.format(merchant=entry["merchant"]), body=entry["detail"],
            item_id=entry["item_id"], amount=Decimal(entry["amount"]), currency=entry["currency"],
            dedupe_key=entry["key"], min_interval_minutes=ONCE_MINUTES,
        )
        emitted += alert is not None
    if emitted:
        await session.commit()
    return emitted
