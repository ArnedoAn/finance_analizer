"""Capture watch: ``capture_gap`` alerts when a source that used to arrive goes quiet (Phase 9).

Three isolated checks, called by ``Processor.alerts`` after every cycle with the owner's ``silence_days``:

* sources: for every own account source (``app:``, ``sms:``, ``email:``), at least ``MIN_EVENTS`` events in the
  30 days before the silent window and none inside the last ``silence_days`` days;
* mailboxes that currently fail to poll and went quiet by the same rule;
* the LLM: the last ``silence_days`` extractions failed (streak kept in ``runtime_flags.llm_health`` by ``note_llm``).

Alerts are deduplicated with ``min_interval_minutes``; they carry no message text, only account and mailbox names.
Functions never commit; callers own the transaction.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.core.timezone import now_utc
from app.db.models import InboxItem, MailAccountState
from app.db.models_flags import RuntimeFlag
from app.services import alerts, own_accounts

LLM_KEY = "llm_health"
MIN_EVENTS = 3
HISTORY_DAYS = 30
FAILURE_STREAK = 3  # failing mailbox / LLM streak that the digest reports as a capture failure
GAP_REMINDER_MINUTES = 60 * 24 * 3
_KIND_LABEL = {"app": "notificaciones", "sms": "SMS", "email": "correo"}
_ITEM_SOURCE = {"app": "notification", "sms": "sms", "email": "email"}


async def llm_streak(session) -> int:
    """Consecutive failed LLM extractions (0 after a success or when none ran)."""
    value = await session.scalar(select(RuntimeFlag.value).where(RuntimeFlag.key == LLM_KEY))
    return int(value.get("failures", 0)) if isinstance(value, dict) else 0


async def note_llm(session, ok: bool) -> None:
    """Record the outcome of one LLM extraction. Writes only when the streak changes; never commits."""
    streak = await llm_streak(session)
    if ok and streak == 0:
        return
    value = {"failures": 0 if ok else streak + 1, "at": now_utc().isoformat()}
    row = await session.get(RuntimeFlag, LLM_KEY, populate_existing=True)
    if row is None:
        session.add(RuntimeFlag(key=LLM_KEY, value=value))
    else:
        row.value = value
    await session.flush()


def _days(delta: timedelta) -> int:
    return max(0, delta.days)


def _ago(days: int) -> str:
    return "Hace 1 día" if days == 1 else f"Hace {days} días"


async def _events(session, now: datetime, silence: int):
    """One row per (source, identity, mailbox) with its events before and inside the silent window."""
    since = now - timedelta(days=HISTORY_DAYS + silence)
    window = now - timedelta(days=silence)
    rows = await session.execute(select(InboxItem.source, InboxItem.app, InboxItem.sender, InboxItem.account_name, InboxItem.created_at).where(
        InboxItem.created_at >= since, InboxItem.source.in_(("sms", "notification", "email"))))
    groups: dict[tuple, dict] = defaultdict(lambda: {"before": 0, "recent": 0, "last": None})
    for source, app, sender, mailbox, created in rows:
        group = groups[(source, (app if source == "notification" else sender) or "", mailbox or "")]
        if created >= window:
            group["recent"] += 1
        else:
            group["before"] += 1
            group["last"] = max(created, group["last"]) if group["last"] else created
    return groups


async def check_sources(session, settings, silence: int, now: datetime | None = None) -> list:
    now = now or now_utc()
    groups = await _events(session, now, silence)
    emitted = []
    for account in await own_accounts.load(session):
        for source in account.sources:
            kind, _, value = source.partition(":")
            if kind.casefold() not in _KIND_LABEL:
                continue
            kind = kind.casefold()
            single = [SimpleNamespace(sources=(source,))]
            before = recent = 0
            last = None
            for (item_source, identity, _), group in groups.items():
                if item_source == _ITEM_SOURCE[kind] and own_accounts.for_source(single, item_source, identity):
                    before, recent = before + group["before"], recent + group["recent"]
                    if group["last"]:
                        last = max(last, group["last"]) if last else group["last"]
            if recent or before < MIN_EVENTS or last is None:
                continue
            alert = await alerts.emit(
                session, "capture_gap", title=f"Sin eventos de {account.firefly_account}", dedupe_key=f"capture_gap:{account.firefly_account}:{source}",
                body=f"{_ago(max(silence, _days(now - last)))} no llega nada de {account.firefly_account} ({_KIND_LABEL[kind]}). Revisa que la fuente siga activa.",
                min_interval_minutes=GAP_REMINDER_MINUTES,
            )
            if alert:
                emitted.append(alert)
    return emitted


async def check_mailboxes(session, settings, silence: int, now: datetime | None = None) -> list:
    now = now or now_utc()
    failing = [state for state in (await session.scalars(select(MailAccountState).order_by(MailAccountState.name))).all() if state.consecutive_failures >= 1]
    if not failing:
        return []
    groups = await _events(session, now, silence)
    emitted = []
    for state in failing:
        mine = [group for (source, _, mailbox), group in groups.items() if source == "email" and mailbox == state.name]
        before, recent = sum(g["before"] for g in mine), sum(g["recent"] for g in mine)
        lasts = [g["last"] for g in mine if g["last"]]
        if recent or before < MIN_EVENTS or not lasts:
            continue
        alert = await alerts.emit(
            session, "capture_gap", title=f"Buzón «{state.name}» sin correos", dedupe_key=f"capture_gap:mailbox:{state.name}",
            body=f"{_ago(max(silence, _days(now - max(lasts))))} no llega nada del buzón «{state.name}» y falla al consultarlo ({state.consecutive_failures} intentos seguidos).",
            min_interval_minutes=GAP_REMINDER_MINUTES,
        )
        if alert:
            emitted.append(alert)
    return emitted


async def check_llm(session, silence: int):
    streak = await llm_streak(session)
    if streak < silence:
        return None
    return await alerts.emit(
        session, "capture_gap", title="La extracción con IA está fallando", dedupe_key="capture_gap:llm",
        body=f"Las últimas {streak} extracciones con IA fallaron. Los movimientos sin plantilla quedan pendientes; revisa el proveedor del modelo.",
        min_interval_minutes=alerts.DAILY_MINUTES,
    )
