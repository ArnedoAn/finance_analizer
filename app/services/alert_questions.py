"""Alerts that ask the owner something (type ``question``) and the answer that applies and teaches.

After each cycle ``after_cycle`` looks at the movements of the last ``WINDOW_DAYS`` days and asks at most ``daily_limit``
questions per Bogotá day (largest amount first; the rest stays an ordinary alert or suggestion). Four cases, at most one
question per movement and never the same question twice (``alert_questions`` is unique per ``(kind, key)``):

* ``duplicate``: a movement in status ``duplicate``. Choices: void it / keep it.
* ``personal``: a spend on a shared own account (policy ``shared_accounts``) from a merchant seen so far only on other accounts.
* ``category``: a registered movement whose pending AI category has a confidence in ``[0.5, threshold)``.
* ``new_merchant``: a registered movement of a merchant seen for the first time and without an active rule.

``answer`` applies the choice through the existing paths (``ledger.bulk``, ``Processor.review``) and learns:
the category feeds ``rules.learn_item``; a new merchant gets a learned ``merchant_exact`` rule; personal/shared answers become
assistant memories. Every choice list ends with ``skip`` («Ahora no»), which only closes the question.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from app.core.exceptions import FinanceAnalyzerError
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import AiItemSuggestion, AlertQuestion, InboxItem
from app.db.models_alerts import Alert
from app.db.models_flags import RuntimeFlag
from app.db.models_rules import Merchant, Rule
from app.services import ai_features, alerts, audit, ledger, memories, rules, suggestions
from app.services.ledger import LedgerBulk, LedgerError

logger = get_logger(__name__)

POLICY_KEY = "alert_questions"
DEFAULT_DAILY_LIMIT = 3
MAX_DAILY_LIMIT = 20
WINDOW_DAYS = 3
SCAN_ROWS = 200
MIN_CONFIDENCE = 0.5
CLAIM_TIMEOUT = timedelta(minutes=10)
SKIP = {"id": "skip", "label": "Ahora no"}
PERSONAL_TAG = "personal"


class QuestionError(Exception):
    """A client-visible failure; the router renders it as ``{"detail", "code"}`` with ``status``."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


# --- policy -----------------------------------------------------------------------------------------------------------


async def policy(session) -> dict:
    """``{"daily_limit": int, "shared_accounts": [str]}`` with defaults for what is not stored."""
    stored = await session.scalar(select(RuntimeFlag.value).where(RuntimeFlag.key == POLICY_KEY)) or {}
    limit = stored.get("daily_limit")
    accounts = stored.get("shared_accounts")
    return {
        "daily_limit": limit if isinstance(limit, int) and not isinstance(limit, bool) and 0 <= limit <= MAX_DAILY_LIMIT else DEFAULT_DAILY_LIMIT,
        "shared_accounts": [str(name) for name in accounts] if isinstance(accounts, list) else [],
    }


async def set_policy(session, *, daily_limit: int, shared_accounts: list[str]) -> dict:
    """Store the policy (the caller validates, audits and commits)."""
    value = {"daily_limit": int(daily_limit), "shared_accounts": list(shared_accounts)}
    row = await session.get(RuntimeFlag, POLICY_KEY, populate_existing=True)
    if row is None:
        session.add(RuntimeFlag(key=POLICY_KEY, value=value))
    else:
        row.value = value
    await session.flush()
    return value


def _day_start(settings) -> datetime:
    zone = ZoneInfo(settings.timezone)
    return datetime.combine(now_utc().astimezone(zone).date(), time.min, zone)


async def asked_today(session, settings) -> int:
    return int(await session.scalar(select(func.count()).select_from(AlertQuestion).where(AlertQuestion.created_at >= _day_start(settings))) or 0)


# --- reading ----------------------------------------------------------------------------------------------------------


async def attach(session, items: list[dict]) -> None:
    """Add ``choices`` (``[{id, label}]``) and ``question_status`` to the alert dicts that carry an answerable question.

    Other alerts are left untouched, so the existing ``Alert`` shape only grows for the new question alerts.
    """
    ids = [item["id"] for item in items if item.get("type") == "question"]
    if not ids:
        return
    found = {row.alert_id: row for row in (await session.scalars(select(AlertQuestion).where(AlertQuestion.alert_id.in_(ids)))).all()}
    for item in items:
        row = found.get(item["id"])
        if row is not None:
            item["choices"], item["question_status"] = list(row.choices), row.status


# --- generation -------------------------------------------------------------------------------------------------------


def _text(item: InboxItem) -> str:
    return f"{alerts.money(item.amount, item.currency)} en {item.merchant}"


async def _has_rule(session, name: str) -> bool:
    active = (await session.scalars(select(Rule).where(Rule.active.is_(True), Rule.match.in_(("merchant_exact", "merchant_contains"))))).all()
    return any((rule.match == "merchant_exact" and rule.pattern == name) or (rule.match == "merchant_contains" and rule.pattern in name) for rule in active)


def _choices(labels: list[str]) -> list[dict]:
    return [{"id": f"c{number}", "label": label} for number, label in enumerate(labels, start=1)] + [dict(SKIP)]


async def _category_options(session, item: InboxItem, first: str | None = None) -> list[str]:
    found = [first] if first else []
    for name in await suggestions.suggest(session, item, 3):
        if name.casefold() not in {value.casefold() for value in found}:
            found.append(name)
    return found[:3]


async def _candidate(session, item: InboxItem, shared: set[str], threshold: float) -> dict | None:
    """The question this movement deserves, or ``None``."""
    name = rules.merchant_name(item.merchant)
    where = _text(item) if item.amount is not None and item.merchant else None
    if item.status == "duplicate":
        if where is None or not item.duplicate_of_id:
            return None
        return {"kind": "duplicate", "key": f"item:{item.id}", "item": item, "payload": {"duplicate_of_id": item.duplicate_of_id},
                "title": "Posible movimiento duplicado", "question": f"¿El movimiento de {where} es un duplicado del #{item.duplicate_of_id}?",
                "choices": [{"id": "void", "label": "Sí, anularlo"}, {"id": "keep", "label": "No, es distinto"}, dict(SKIP)]}
    if item.status != "created" or where is None or not name or item.transaction_type != "withdrawal" or not item.firefly_transaction_id:
        return None
    account = item.asset_account or ""
    if account.casefold() in shared:
        others = [row for row in (await session.execute(
            select(InboxItem.asset_account).where(InboxItem.status == "created", InboxItem.id != item.id, func.upper(InboxItem.merchant) == name.upper())
        )).all()]
        seen_elsewhere = any((row[0] or "").casefold() not in shared for row in others)
        seen_here = any((row[0] or "").casefold() in shared for row in others)
        if seen_elsewhere and not seen_here:
            return {"kind": "personal", "key": f"merchant:{name}:{account.casefold()}", "item": item, "payload": {"merchant": name, "account": account},
                    "title": "¿Gasto personal?", "question": f"¿El gasto de {where} es personal? Lo pagaste con {account}, una cuenta compartida.",
                    "choices": [{"id": "personal", "label": "Sí, es personal"}, {"id": "shared", "label": "No, es compartido"}, dict(SKIP)]}
    pending = await session.scalar(select(AiItemSuggestion).where(AiItemSuggestion.item_id == item.id, AiItemSuggestion.status == "pending"))
    if (pending is not None and pending.category and MIN_CONFIDENCE <= pending.category_confidence < threshold
            and (item.category or "").casefold() != pending.category.casefold()):
        labels = await _category_options(session, item, pending.category)
        return {"kind": "category", "key": f"item:{item.id}", "item": item, "payload": {"categories": labels, "suggestion_id": pending.id},
                "title": "Una pregunta sobre un movimiento", "question": f"¿Este cobro de {where} es {pending.category}?", "choices": _choices(labels)}
    merchant = await session.scalar(select(Merchant).where(Merchant.name == name))
    if merchant is not None and merchant.count <= 1 and not await _has_rule(session, name):
        labels = await _category_options(session, item, item.category)
        if labels:
            return {"kind": "new_merchant", "key": f"merchant:{name}", "item": item, "payload": {"categories": labels, "merchant": name},
                    "title": "Comercio nuevo", "question": f"{item.merchant} es un comercio nuevo ({where}): ¿en qué categoría lo registro?",
                    "choices": _choices(labels)}
    return None


async def generate(session, settings) -> int:
    """Emit the questions of this pass (within the daily limit); returns how many were asked. Commits."""
    current = await policy(session)
    remaining = current["daily_limit"] - await asked_today(session, settings)
    if remaining <= 0:
        return 0
    shared = {name.casefold() for name in current["shared_accounts"]}
    threshold = (await ai_features.get(session))["confidence_threshold"]
    asked_items = select(AlertQuestion.item_id).where(AlertQuestion.item_id.is_not(None))
    since = now_utc() - timedelta(days=WINDOW_DAYS)
    items = (await session.scalars(
        select(InboxItem).where(InboxItem.status.in_(("created", "duplicate")), InboxItem.created_at >= since, InboxItem.id.not_in(asked_items))
        .order_by(InboxItem.id.desc()).limit(SCAN_ROWS)
    )).all()
    found = []
    for item in items:
        candidate = await _candidate(session, item, shared, threshold)
        if candidate is not None:
            found.append(candidate)
    found.sort(key=lambda row: row["item"].amount, reverse=True)
    asked = 0
    for candidate in found:
        if asked >= remaining:
            break
        if await session.scalar(select(AlertQuestion.id).where(AlertQuestion.kind == candidate["kind"], AlertQuestion.key == candidate["key"])):
            continue
        item = candidate["item"]
        alert = await alerts.emit(
            session, "question", title=candidate["title"], body=candidate["question"], item_id=item.id, amount=item.amount, currency=item.currency,
            question=candidate["question"], options=[choice["label"] for choice in candidate["choices"]],
            dedupe_key=f"question:{candidate['kind']}:{candidate['key']}", min_interval_minutes=alerts.ONCE_MINUTES,
        )
        if alert is None:  # muted or already sent
            continue
        session.add(AlertQuestion(alert_id=alert.id, kind=candidate["kind"], key=candidate["key"], item_id=item.id, amount=item.amount,
                                  choices=candidate["choices"], payload=candidate["payload"]))
        await session.commit()
        asked += 1
    await session.commit()
    return asked


async def after_cycle(session, settings) -> dict:
    """Automatic step after each cycle. Never raises."""
    try:
        return {"asked": await generate(session, settings)}
    except Exception as exc:
        logger.error("alert_questions_failed", error_type=type(exc).__name__)
        try:
            await session.rollback()
        except Exception:
            pass
        return {"error": type(exc).__name__}


# --- answering --------------------------------------------------------------------------------------------------------


async def _registered(session, question: AlertQuestion) -> InboxItem:
    item = await session.get(InboxItem, question.item_id, populate_existing=True) if question.item_id is not None else None
    if item is None or item.status != "created" or not item.firefly_transaction_id:
        raise QuestionError(409, "question_stale", "El movimiento ya cambió; la pregunta ya no aplica.")
    return item


async def _bulk(session, processor, item: InboxItem, changes: dict, principal) -> None:
    try:
        _, _, failed = await ledger.bulk(session, processor, LedgerBulk(ids=[item.firefly_transaction_id], **{"set": changes}), principal)
    except (LedgerError, FinanceAnalyzerError) as exc:
        logger.warning("question_apply_failed", item_id=item.id, error_type=type(exc).__name__)
        raise QuestionError(409, "apply_failed", "No se pudo aplicar la respuesta en Firefly.") from exc
    if failed:
        raise QuestionError(409, "apply_failed", "No se pudo aplicar la respuesta en Firefly.")
    await session.refresh(item)



async def _apply_category(session, processor, question: AlertQuestion, item: InboxItem, label: str, principal) -> tuple[dict, str | None]:
    """Set the chosen category; ``(result, previous category to learn from)``."""
    previous = item.category
    if (previous or "").casefold() != label.casefold():
        await _bulk(session, processor, item, {"category": label}, principal)
    suggestion = await session.get(AiItemSuggestion, question.payload.get("suggestion_id")) if question.payload.get("suggestion_id") else None
    if suggestion is not None and suggestion.status == "pending":
        suggestion.status, suggestion.applied_at = "applied", now_utc()
        suggestion.applied_fields = sorted({*(suggestion.applied_fields or []), "category"})
    return {"action": "category", "category": label}, previous


async def _learn_merchant(session, name: str, category: str, item_id: int, principal) -> dict | None:
    """The owner told what a new merchant is: create the learned rule unless an active manual one already decides it."""
    existing = await session.scalar(select(Rule).where(Rule.match == "merchant_exact", Rule.pattern == name))
    if existing is not None and existing.active and existing.source == "manual":
        return None
    rule = await rules.create_rule(session, match="merchant_exact", pattern=name, category=category, merchant=name, source="learned")
    await audit.record(session, action="rule.learn", target_type="rule", target_id=rule.id, principal=principal,
                       detail={"match": rule.match, "pattern": rule.pattern, "category": category, "source": "learned", "item_id": item_id, "via": "question"})
    return rules.rule_data(rule)


async def _apply(session, processor, question: AlertQuestion, choice: str, principal) -> tuple[dict, dict]:
    """Run the chosen answer; returns ``(result, learning hints)``."""
    if choice == "skip":
        return {"action": "skip"}, {}
    kind = question.kind
    if kind == "duplicate":
        item = await session.get(InboxItem, question.item_id, populate_existing=True)
        if item is None or item.status != "duplicate":
            raise QuestionError(409, "question_stale", "El movimiento ya cambió; la pregunta ya no aplica.")
        try:
            await processor.review(item, "reject" if choice == "void" else "approve", principal=principal)
        except ValueError as exc:
            raise QuestionError(409, "apply_failed", str(exc)) from exc
        return {"action": "voided" if choice == "void" else "kept", "status": item.status}, {}
    item = await _registered(session, question)
    if kind == "personal":
        merchant, account = question.payload["merchant"], question.payload["account"]
        if choice == "personal":
            await _bulk(session, processor, item, {"tags_add": [PERSONAL_TAG]}, principal)
            await memories.remember(session, f"Los gastos en {merchant} con la cuenta {account} son personales.")
        else:
            await memories.remember(session, f"Los gastos en {merchant} con la cuenta {account} son compartidos.")
        return {"action": choice, "merchant": merchant}, {}
    label = question.payload["categories"][int(choice[1:]) - 1]
    result, previous = await _apply_category(session, processor, question, item, label, principal)
    if kind == "new_merchant":
        result["rule"] = await _learn_merchant(session, question.payload["merchant"], label, item.id, principal)
        return result, {}
    return result, {"item_id": item.id, "previous": previous}


async def answer(session, processor, principal, alert_id: int, choice: str) -> tuple[Alert, dict]:
    """Apply ``choice`` to the question of alert ``alert_id``; ``(alert, result)``. Audited; commits.

    ``QuestionError``: 404 ``alert_not_found``; 409 ``question_not_answerable`` / ``already_answered`` / ``question_busy`` /
    ``question_stale`` / ``apply_failed``; 422 ``invalid_choice``.
    """
    alert = await session.get(Alert, alert_id, populate_existing=True)
    if alert is None:
        raise QuestionError(404, "alert_not_found", "No existe esa alerta.")
    question = await session.scalar(select(AlertQuestion).where(AlertQuestion.alert_id == alert_id).execution_options(populate_existing=True))
    if question is None:
        raise QuestionError(409, "question_not_answerable", "Esta alerta no es una pregunta que se pueda responder.")
    if question.status == "answered":
        raise QuestionError(409, "already_answered", "Esta pregunta ya se respondió.")
    if question.status == "stale":
        raise QuestionError(409, "question_stale", "El movimiento ya cambió; la pregunta ya no aplica.")
    if choice not in {entry["id"] for entry in question.choices}:
        raise QuestionError(422, "invalid_choice", "Esa opción no existe en la pregunta.")
    now = now_utc()
    claimed = await session.execute(
        update(AlertQuestion).where(
            AlertQuestion.id == question.id,
            (AlertQuestion.status == "open") | ((AlertQuestion.status == "answering") & (AlertQuestion.claimed_at < now - CLAIM_TIMEOUT)),
        ).values(status="answering", claimed_at=now).execution_options(synchronize_session=False)
    )
    await session.commit()
    if not claimed.rowcount:
        raise QuestionError(409, "question_busy", "Esa pregunta se está respondiendo.")
    question_id = question.id
    try:
        result, learn = await _apply(session, processor, question, choice, principal)
    except Exception as exc:
        await session.rollback()
        row = await session.get(AlertQuestion, question_id, populate_existing=True)
        row.status, row.claimed_at = ("stale" if isinstance(exc, QuestionError) and exc.code == "question_stale" else "open"), None
        await session.commit()
        if isinstance(exc, QuestionError):
            raise
        logger.error("question_apply_crashed", error_type=type(exc).__name__)
        raise QuestionError(409, "apply_failed", "No se pudo aplicar la respuesta.") from exc
    question = await session.get(AlertQuestion, question_id, populate_existing=True)
    alert = await session.get(Alert, alert_id, populate_existing=True)
    question.status, question.answer, question.answered_at, question.claimed_at = "answered", choice, now_utc(), None
    if alert.acked_at is None:
        alert.acked_at = now_utc()
    await audit.record(session, action="alert.answer", target_type="alert", target_id=alert_id, principal=principal,
                       detail={"kind": question.kind, "choice": choice, "item_id": question.item_id, "result": result.get("action"), "category": result.get("category")})
    await session.commit()
    if learn:
        await rules.learn_item(session, learn["item_id"], learn["previous"], principal)
    return alert, result

