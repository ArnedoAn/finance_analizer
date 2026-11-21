"""Alerts for the app (contract sections 5 and 13): rows in ``alerts`` that the phone polls.

``emit`` never commits; the caller owns the transaction. Anti-noise reuses the ``notifications``
table (``key``, ``last_sent_at``) under the ``alert:`` prefix: a repeated ``dedupe_key`` (or type,
when none is given) inside the interval is suppressed. Titles and bodies are Spanish, carry full
amounts and never include raw message text.
"""
import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import InboxItem, Notification
from app.db.models_alerts import Alert
from app.db.models_devices import Device

logger = get_logger(__name__)

ONCE_MINUTES = 60 * 24 * 3650  # effectively "once": per-item and per-month keys never repeat
DAILY_MINUTES = 60 * 24
KEY_REMINDER_MINUTES = 60 * 24 * 3
KEY_EXPIRY_WINDOW = timedelta(days=7)
OLD_PENDING_SECONDS = 7200

_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
_SOURCES = {"sms": "un SMS", "notification": "una notificación del teléfono", "email": "un correo", "manual": "un registro manual"}
_KINDS = {"withdrawal": "Gasto", "deposit": "Ingreso", "transfer": "Transferencia", "refund": "Devolución"}


def money(amount, currency="COP") -> str:
    """Full amount in es-CO style: ``$2.500.000 COP`` (cents only when present)."""
    value = Decimal(str(amount))
    text = f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"
    text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return f"${text} {currency or 'COP'}"


def month_label(day: date) -> str:
    return f"{_MONTHS[day.month - 1]} de {day.year}"


def _local(value: datetime, zone) -> str:
    return value.astimezone(zone).strftime("%d/%m/%Y")


def alert_data(alert: Alert) -> dict:
    """Contract section 14 ``Alert``; not-applicable fields are null or empty lists."""
    return {
        "id": alert.id, "type": alert.type, "created_at": alert.created_at, "title": alert.title,
        "body": alert.body, "item_id": alert.item_id,
        "amount": f"{alert.amount:.2f}" if alert.amount is not None else None,
        "currency": alert.currency, "suggested_categories": list(alert.suggested_categories or []),
        "question": alert.question, "options": list(alert.options or []), "acked_at": alert.acked_at,
    }


async def _claim(session, dedupe: str, minutes: int) -> bool:
    key = "alert:" + (dedupe if len(dedupe) <= 200 else hashlib.sha256(dedupe.encode()).hexdigest())
    now = now_utc()
    result = await session.execute(
        update(Notification)
        .where(Notification.key == key, Notification.last_sent_at <= now - timedelta(minutes=minutes))
        .values(last_sent_at=now).execution_options(synchronize_session=False)
    )
    if result.rowcount:
        return True
    if await session.scalar(select(Notification.key).where(Notification.key == key)) is not None:
        return False
    try:
        async with session.begin_nested():
            session.add(Notification(key=key, last_sent_at=now))
            await session.flush()
    except IntegrityError:
        return False
    return True


async def emit(session, type: str, *, title: str, body: str, item_id=None, amount=None, currency=None,
               suggested_categories=None, question=None, options=None, dedupe_key: str | None = None,
               min_interval_minutes: int | None = None) -> Alert | None:
    """Insert an alert unless the same ``dedupe_key`` (default: the type) fired within the interval
    (default ``NOTIFY_MIN_INTERVAL_MINUTES``). Returns the flushed row, or None when suppressed."""
    if min_interval_minutes is None:
        min_interval_minutes = get_settings().notify_min_interval_minutes
    dedupe = dedupe_key or type
    if not await _claim(session, dedupe, max(0, min_interval_minutes)):
        return None
    alert = Alert(
        type=type, title=title[:255], body=body, item_id=item_id,
        amount=Decimal(str(amount)).quantize(Decimal("0.01")) if amount is not None else None,
        currency=currency, suggested_categories=list(suggested_categories) if suggested_categories is not None else None,
        question=question, options=list(options) if options is not None else None, dedupe_key=dedupe,
    )
    session.add(alert)
    await session.flush()
    return alert


# ---- item events -------------------------------------------------------------------------

async def _suggest(session, item) -> list[str]:
    try:
        from app.services import suggestions
        return [str(name) for name in await suggestions.suggest(session, item)]
    except Exception as exc:  # suggestions improve the alert; they must never block it
        logger.warning("alert_suggestions_failed", error_type=type(exc).__name__)
        return []


async def _question(session, item) -> dict | None:
    try:
        from app.services import suggestions
        return await suggestions.question_for(session, item)
    except Exception as exc:
        logger.warning("alert_question_failed", error_type=type(exc).__name__)
        return None


def _describe(item) -> str:
    kind = _KINDS.get(item.transaction_type, "Movimiento")
    if item.amount is None:
        return f"{kind} sin monto interpretado desde {_SOURCES.get(item.source, 'una fuente')}"
    place = f" en {item.merchant}" if item.merchant else ""
    return f"{kind} de {money(item.amount, item.currency)}{place}"


async def item_event(session, settings, item, old_status) -> None:
    """Alerts for an item whose status just changed (called after the status was committed)."""
    if item.status == old_status:
        return
    common = {"item_id": item.id, "amount": item.amount, "currency": item.currency if item.amount is not None else None}
    if item.status == "needs_review":
        if item.review_reason == "sender_auth_failed":
            dedupe = "pending_review:auth:" + (item.sender or "").rsplit("@", 1)[-1].casefold()
            body = "Un correo bancario no pasó la verificación del remitente; revísalo antes de aprobarlo."
        else:
            dedupe = f"pending_review:{item.id}"
            body = f"{_describe(item)} necesita tu confirmación antes de registrarse en Firefly."
        await emit(session, "pending_review", title="Movimiento por revisar", body=body, dedupe_key=dedupe,
                   suggested_categories=await _suggest(session, item), **common)
        asked = await _question(session, item)
        if asked:
            await emit(session, "question", title="Una pregunta sobre un movimiento", body=asked["question"],
                       question=asked["question"], options=list(asked.get("options") or []),
                       dedupe_key=f"question:{item.id}", **common)
    elif item.status == "duplicate":
        origin = f" (#{item.duplicate_of_id})" if item.duplicate_of_id else ""
        await emit(session, "duplicate_suspect", title="Posible movimiento duplicado", dedupe_key=f"duplicate:{item.id}",
                   body=f"{_describe(item)} parece repetir uno ya registrado{origin}. Anúlalo o confirma que es válido.", **common)
    elif item.status == "failed":
        await emit(session, "system_failure", title="Falló el registro de un movimiento", dedupe_key=f"failed:{item.id}",
                   body=f"{_describe(item)} no pudo registrarse en Firefly; requiere atención.", **common)
    if item.status in {"created", "needs_review", "duplicate"} and item.amount is not None and item.amount > settings.review_amount_cop:
        await emit(session, "big_amount", title="Movimiento de monto alto", dedupe_key=f"big_amount:{item.id}",
                   body=f"{_describe(item)} supera el límite de revisión de {money(settings.review_amount_cop)}.",
                   min_interval_minutes=ONCE_MINUTES, **common)


# ---- periodic checks (called by Processor.alerts) ----------------------------------------

async def check_old_pending(session, stats) -> Alert | None:
    if (stats["oldest_pending_age_s"] or 0) <= OLD_PENDING_SECONDS:
        return None
    return await emit(session, "system_failure", title="Movimientos pendientes hace más de 2 horas", dedupe_key="old-pending",
                      body=f"Hay {stats['pending']} movimientos esperando procesamiento desde hace más de 2 horas.", min_interval_minutes=360)


async def check_phone_silence(session, settings) -> Alert | None:
    last = await session.scalar(select(func.max(InboxItem.created_at)).where(InboxItem.source.in_(["sms", "notification"])))
    if last is not None and now_utc() - last <= timedelta(hours=settings.phone_silence_hours):
        return None
    since = f"desde el {_local(last, ZoneInfo(settings.timezone))}" if last else "todavía"
    return await emit(session, "phone_silence", title="Sin eventos del teléfono", dedupe_key="phone_silence",
                      body=f"El teléfono no ha enviado eventos {since} (límite: {settings.phone_silence_hours} h). Revisa que la app Cofre siga activa.",
                      min_interval_minutes=settings.phone_silence_hours * 60)


async def check_budgets(session, firefly, today: date) -> list[Alert]:
    start = today.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    emitted = []
    for row in await firefly.budget_progress(start, end):
        spent, limit = Decimal(row["spent"]), Decimal(row["limit"])
        if limit <= 0:
            continue
        kind = "budget_100" if spent >= limit else "budget_80" if spent * 5 >= limit * 4 else None
        if kind is None:
            continue
        pct = int(spent * 100 / limit)
        alert = await emit(
            session, kind, dedupe_key=f"{kind}:{row['name']}:{start:%Y-%m}", min_interval_minutes=ONCE_MINUTES,
            title=f"Presupuesto «{row['name']}» al {pct} %" if kind == "budget_80" else f"Presupuesto «{row['name']}» agotado",
            body=f"Llevas {money(spent)} de {money(limit)} ({pct} %) en «{row['name']}» durante {month_label(start)}.",
            amount=spent, currency="COP",
        )
        if alert:
            emitted.append(alert)
    return emitted


async def check_key_expiring(session, settings) -> list[Alert]:
    now = now_utc()
    zone = ZoneInfo(settings.timezone)
    rows = await session.scalars(select(Device).where(
        Device.revoked_at.is_(None), Device.expires_at.is_not(None), Device.expires_at > now, Device.expires_at <= now + KEY_EXPIRY_WINDOW,
    ).order_by(Device.id))
    emitted = []
    for device in rows:
        alert = await emit(
            session, "key_expiring", title="Una clave de dispositivo caduca pronto", min_interval_minutes=KEY_REMINDER_MINUTES,
            body=f"La clave de «{device.name}» caduca el {_local(device.expires_at, zone)}. Vuelve a enrolar el dispositivo antes de esa fecha.",
            dedupe_key=f"key_expiring:{device.id}:{device.expires_at:%Y-%m-%d}",
        )
        if alert:
            emitted.append(alert)
    return emitted


async def check_backup(session, settings) -> Alert | None:
    from app.services import backup

    status = backup.read_status(settings)
    state = status.get("status")
    if state not in {"failed", "stale"}:
        return None
    last = status.get("last_success_at")
    tail = f" Última copia correcta: {_local(datetime.fromisoformat(last), ZoneInfo(settings.timezone))}." if last else ""
    if state == "failed":
        title, body = "Falló la copia de seguridad", "La última copia de seguridad terminó con error. Revisa el servidor."
    else:
        title, body = "Copia de seguridad atrasada", "No hay una copia de seguridad correcta en las últimas 24 horas."
    return await emit(session, "backup_failed", title=title, body=body + tail, dedupe_key=f"backup_failed:{state}", min_interval_minutes=DAILY_MINUTES)


def _totals(report) -> tuple[Decimal, Decimal]:
    return Decimal(report["totals"].get("deposit", "0")), Decimal(report["totals"].get("withdrawal", "0"))


def _top(mapping: dict, count: int = 3) -> str:
    rows = sorted(((name, Decimal(value)) for name, value in mapping.items()), key=lambda pair: pair[1], reverse=True)[:count]
    return ", ".join(f"{name} ({money(value)})" for name, value in rows)


async def month_close(session, report, month: date, final: bool) -> Alert | None:
    """``final`` summarizes the finished month (first day of the next one); otherwise it is the last-day preview."""
    income, spent = _totals(report)
    body = f"Ingresos {money(income)}, gastos {money(spent)}, balance {money(income - spent)}."
    if report["by_category"]:
        body += f" Mayores categorías: {_top(report['by_category'])}."
    title = f"Cierre de {month_label(month)}" if final else f"Hoy cierra {month_label(month)}"
    return await emit(session, "month_close", title=title, body=body, dedupe_key=f"month_close:{month:%Y-%m}:{'final' if final else 'preview'}", min_interval_minutes=ONCE_MINUTES)


async def digest(session, report, previous, stats, weekly: bool) -> Alert | None:
    period = "week" if weekly else "day"
    start = date.fromisoformat(report["start"])
    income, spent = _totals(report)
    _, spent_before = _totals(previous)
    body = f"Gastos {money(spent)} (periodo anterior {money(spent_before)}), ingresos {money(income)}."
    if report["by_category"]:
        body += f" Categorías: {_top(report['by_category'])}."
    if report["top_merchants"]:
        body += " Comercios: " + ", ".join(f"{row['merchant']} ({money(row['amount'])})" for row in report["top_merchants"][:3]) + "."
    body += f" Pendientes {stats['pending']}, por revisar {stats['needs_review']}, fallidos {stats['failed']}."
    title = f"Resumen de la semana del {start:%d/%m/%Y}" if weekly else f"Resumen del {start:%d/%m/%Y}"
    return await emit(session, f"digest_{'weekly' if weekly else 'daily'}", title=title, body=body, dedupe_key=f"digest:{period}:{start}", min_interval_minutes=DAILY_MINUTES)
