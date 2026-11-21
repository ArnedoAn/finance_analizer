"""Durable ingest → extract → review → submit; Firefly external IDs are authoritative."""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.clients.imap import IMAPClient
from app.clients.llm import verify_llm
from app.core.config import get_settings
from app.core.exceptions import PermanentError
from app.core.logging import get_logger
from app.db.models import CycleRun, InboxEvent, InboxItem
from app.db.repositories import InboxRepository, KnownSenderRepository, MailStateRepository
from app.parsers import normalize_merchant, parse
from app.security import is_sensitive_code
from app.security.sender_trust import evaluate_email
from app.services import alerts as alerting
from app.services import audit, flags, rules

logger = get_logger(__name__)

_ECONOMIC_LOCKS = WeakValueDictionary()



def _now():
    return datetime.now(UTC)


def _aware(value, zone):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)).astimezone(UTC)


class Processor:
    def __init__(self, session, firefly, llm, settings=None):
        self.session, self.firefly, self.llm = session, firefly, llm
        self.settings = settings or get_settings()
        self.zone = ZoneInfo(self.settings.timezone)
        self.inbox = InboxRepository(session)
        self.senders = KnownSenderRepository(session)
        self.mail_states = MailStateRepository(session)
        self._accounts = self._categories = None

    async def sync(self, force=False):
        if force or self._accounts is None:
            self._accounts = await self.firefly.accounts()
            self._categories = await self.firefly.categories()
        missing = [a.firefly_account for a in self.settings.own_accounts if not any(x["name"].casefold() == a.firefly_account.casefold() and x["type"] in {"asset", "liability", "liabilities"} for x in self._accounts)]
        for name in missing:
            if await alerting.emit(self.session, "system_failure", title="Cuenta propia inexistente en Firefly", body=f"La cuenta «{name}» configurada en OWN_ACCOUNTS no existe en Firefly.", dedupe_key="missing-account:" + name, min_interval_minutes=360):
                await self.session.commit()
        return {"accounts": len(self._accounts), "categories": len(self._categories), "missing_accounts": missing}

    async def accounts(self):
        await self.sync()
        return self._accounts

    async def categories(self):
        await self.sync()
        return self._categories

    async def _asset(self, name):
        for account in await self.accounts():
            if account["type"] in {"asset", "liability", "liabilities"} and account["name"].casefold() == (name or "").casefold():
                return account
        raise PermanentError("Firefly account does not exist: " + str(name))

    def _source_accounts(self, source, identity):
        prefix = {"notification": "app", "sms": "sms", "email": "email"}[source]
        result = []
        for account in self.settings.own_accounts:
            for configured in account.sources:
                kind, _, value = configured.casefold().partition(":")
                address = identity.casefold()
                domain = address.rsplit("@", 1)[-1]
                if kind == prefix and (value == address or (kind == "email" and (domain == value or domain.endswith("." + value)))):
                    result.append(account)
                    break
        return result

    def _resolve(self, item, hint=None):
        candidates = self._source_accounts(item.source, item.app or "" if item.source == "notification" else item.sender)
        if hint:
            matched = [a for a in candidates if hint.casefold().lstrip("*") in [v.casefold().lstrip("*") for v in a.last4 + a.aliases + [a.firefly_account]]]
            if matched:
                candidates = matched
        return candidates[0] if len(candidates) == 1 else None

    def _counterparty(self, hint, exclude):
        if not hint:
            return None
        hint = hint.casefold().strip().lstrip("*")
        matched = [a for a in self.settings.own_accounts if a.firefly_account != exclude and any(v.casefold().lstrip("*") in hint for v in a.aliases + a.last4 + [a.firefly_account] if v)]
        return matched[0] if len(matched) == 1 else None

    def _notification_date(self, payload):
        raw = payload.post_time if payload.post_time is not None else payload.timestamp or payload.date
        if not raw:
            return _now(), None
        try:
            try:
                occurred = datetime.fromtimestamp(float(raw) / 1000, UTC)
            except (ValueError, TypeError):
                occurred = _aware(raw, self.zone)
            if occurred < datetime(2020, 1, 1, tzinfo=UTC) or occurred > _now() + timedelta(days=1):
                raise ValueError("Timestamp outside allowed range")
            return occurred, None
        except (ValueError, TypeError, OverflowError, OSError):
            return _now(), "invalid phone timestamp"

    async def _insert(self, ref, **values):
        existing = await self.inbox.by_ref(ref)
        if existing:
            return existing, False
        try:
            async with self.session.begin_nested():
                item = await self.inbox.add(source_ref=ref, **values)
        except IntegrityError:
            return await self.inbox.by_ref(ref), False
        await self.session.commit()
        return item, True

    async def ingest_notification(self, payload):
        content = payload.big_text or payload.message or payload.text
        source = payload.type
        stamp = payload.post_time if payload.post_time is not None else payload.timestamp or payload.date
        ref = "evt:" + str(payload.event_id) if payload.event_id else ("phone:" + hashlib.sha256(json.dumps([source, payload.app, payload.sender, content, stamp]).encode()).hexdigest() if stamp else "phone:" + uuid4().hex)
        identity = payload.app if source == "notification" else payload.sender
        known = bool(self._source_accounts(source, identity))
        occurred, reason = self._notification_date(payload)
        sensitive = is_sensitive_code(content, payload.title)
        item, inserted = await self._insert(ref, source=source, app=payload.app, sender=payload.sender, subject_or_title="" if sensitive else payload.title, raw_text=None if sensitive else content[:4000], occurred_at=occurred, status="ignored" if sensitive or not known else "pending", review_reason="sensitive_code" if sensitive else reason, template_id="ignore.sensitive_code" if sensitive else None)
        return item, inserted and known and not sensitive

    async def poll_mail(self, since=None, account_name=None):
        result = {"queued": 0, "ignored": 0, "duplicates": 0, "needs_review": 0, "errors": {}}
        rules = list(await self.senders.all(False))
        rules += [SimpleNamespace(keyword=s.split(":", 1)[1], status="active", require_auth=True) for a in self.settings.own_accounts for s in a.sources if s.startswith("email:")]
        for account in self.settings.mail_accounts:
            if account_name and account.name != account_name:
                continue
            state = await self.mail_states.get(account.name)
            try:
                days = max(0, (_now().date() - since).days + 1) if since else self.settings.email_lookback_days
                validity, highwater, messages = await IMAPClient(account).poll(None if since else (int(state.uidvalidity) if state.uidvalidity else None), 0 if since else state.last_uid, days, want_body=lambda h: evaluate_email(h, rules).trusted)
                for message in messages:
                    decision = evaluate_email(message, rules)
                    trusted = decision.trusted
                    auth_failed = decision.reason == "authentication_failed"
                    sensitive = is_sensitive_code(message.body or "", message.subject)
                    ref = f"mail:{account.name}:{validity}:{message.uid}"
                    duplicate = await self.session.scalar(select(InboxItem).where(InboxItem.message_id == message.message_id, InboxItem.source_ref != ref).limit(1)) if message.message_id else None
                    status = "ignored" if sensitive else "needs_review" if auth_failed or message.error else "duplicate" if duplicate else "pending" if trusted else "ignored"
                    reason = "sensitive_code" if sensitive else "sender_auth_failed" if auth_failed else message.error
                    item, inserted = await self._insert(ref, source="email", account_name=account.name, message_id=message.message_id, sender=message.from_address, subject_or_title="" if sensitive else message.subject, raw_text=None if sensitive or auth_failed else (message.body or message.subject or "")[:4000], occurred_at=message.occurred_at or _now(), status=status, review_reason=reason, duplicate_of_id=duplicate.id if duplicate else None, template_id="ignore.sensitive_code" if sensitive else None)
                    if inserted:
                        await self._announce(item, None)
                    if not inserted and auth_failed and not sensitive and item.status in {"ignored", "pending", "needs_review"}:
                        item.status, item.review_reason, item.raw_text = "needs_review", "sender_auth_failed", None
                    if since and not inserted and trusted and not sensitive and item.status == "ignored":
                        item.raw_text = (message.body or message.subject or "")[:4000]
                        item.status, item.method, item.review_reason = "pending", None, message.error
                    result["duplicates" if not inserted else "needs_review" if item.status == "needs_review" else "queued" if item.status == "pending" else "duplicates" if item.status == "duplicate" else "ignored"] += 1
                state.uidvalidity, state.last_uid = str(validity), highwater
                state.last_error, state.consecutive_failures = None, 0
            except Exception:
                state.last_error = "Mail polling failed; check account credentials/connectivity"
                state.consecutive_failures += 1
                result["errors"][account.name] = state.last_error
            state.last_poll_at = _now()
            await self.session.commit()
            if state.consecutive_failures >= 3:
                if await alerting.emit(self.session, "system_failure", title="Buzón de correo con fallos", body=f"No se pudo consultar el buzón «{account.name}» en {state.consecutive_failures} intentos seguidos. Revisa sus credenciales y la conexión.", dedupe_key="mail:" + account.name, min_interval_minutes=360):
                    await self.session.commit()
        return result

    async def _merchant(self, merchant, kind):
        name = normalize_merchant(merchant or "")
        if not name or len(name) > 60 or "$" in name:
            raise PermanentError("Merchant is not clean")
        account_type = "revenue" if kind in {"deposit", "refund"} else "expense"
        for account in await self.accounts():
            if account["type"] == account_type and normalize_merchant(account["name"]) == name:
                return account
        account = await self.firefly.create_account(name, account_type)
        self._accounts.append(account)
        return account

    async def manual(self, payload):
        ref = "manual:" + payload.client_ref
        existing = await self.inbox.by_ref(ref)
        if existing:
            return existing
        if payload.amount > self.settings.max_amount_cop:
            raise ValueError("Amount exceeds MAX_AMOUNT_COP")
        asset = await self._asset(payload.account or self.settings.manual_default_account)
        destination = None
        if payload.type == "transfer":
            if not payload.destination_account:
                raise ValueError("Transfers require destination_account")
            destination = await self._asset(payload.destination_account)
            if destination["id"] == asset["id"]:
                raise ValueError("Transfer destination must differ from source")
        category = None
        if payload.category:
            category = next((c for c in await self.categories() if c.casefold() == payload.category.casefold()), None)
            if category is None:
                raise ValueError("Unknown category; suggestions: " + ", ".join(await self.categories()))
        occurred = _aware(payload.date, self.zone) if payload.date else _now()
        if occurred > _now() + timedelta(days=1) or occurred < datetime(2020, 1, 1, tzinfo=UTC):
            raise ValueError("Date outside allowed range")
        merchant = normalize_merchant(payload.merchant or ("Ingreso manual" if payload.type in {"deposit", "refund"} else "Gasto manual"))
        if not merchant or len(merchant) > 60 or "$" in merchant:
            raise ValueError("Merchant is not clean")
        item, inserted = await self._insert(ref, source="manual", sender="owner", raw_text=payload.description[:4000], subject_or_title=payload.description, occurred_at=occurred, status="pending", method="manual", amount=payload.amount.quantize(Decimal("0.01")), currency="COP", merchant=merchant, category=category, asset_account=asset["name"], destination_account=destination["name"] if destination else None, transaction_type=payload.type)
        return await self.process(item) if inserted else item

    async def _extract(self, item, cycle_id):
        if item.template_id == "ignore.sensitive_code" or is_sensitive_code(item.raw_text or "", item.subject_or_title or ""):
            item.status, item.raw_text, item.subject_or_title = "ignored", None, ""
            item.method, item.template_id, item.review_reason = "sensitive_code", "ignore.sensitive_code", "sensitive_code"
            return
        if item.review_reason == "sender_auth_failed":
            item.status = "needs_review"
            return
        text = item.raw_text or ""
        extraction = parse(text, item.subject_or_title or "", item.sender or "")
        item.method = "parser"
        if extraction is None:
            item.method = "llm"
            extraction = await self.llm.analyze("\n".join([item.subject_or_title or "", text]), cycle_id=cycle_id or uuid4().hex)
        item.template_id = extraction.template_id
        if extraction.kind == "ignore":
            item.status = "ignored"
            item.review_reason = extraction.reason
            return
        reasons = [item.review_reason] if item.review_reason else []
        if extraction.kind == "review":
            reasons.append(extraction.reason or "parser requests review")
        if item.method == "llm":
            reasons.extend(verify_llm(text, extraction))
        item.amount = extraction.amount.quantize(Decimal("0.01")) if extraction.amount is not None else None
        if item.method == "llm" and (not item.amount or self.settings.llm_auto_approve_max_cop == 0 or item.amount > self.settings.llm_auto_approve_max_cop):
            reasons.append("LLM extraction requires human review")
        if item.source == "sms" and item.method == "parser" and (not item.amount or self.settings.sms_auto_approve_max_cop == 0 or item.amount > self.settings.sms_auto_approve_max_cop):
            reasons.append("SMS extraction requires human review")
        item.currency, item.merchant = extraction.currency, extraction.merchant
        item.transaction_type = extraction.kind
        item.original_kind = extraction.kind
        own = self._resolve(item, extraction.account_hint)
        item.asset_account = own.firefly_account if own else None
        if not own:
            reasons.append("source has no unambiguous own account")
        counterparty = self._counterparty(extraction.counterparty_hint or extraction.merchant, item.asset_account)
        if counterparty:
            if extraction.kind in {"deposit", "refund"}:
                item.destination_account, item.asset_account = item.asset_account, counterparty.firefly_account
            else:
                item.destination_account = counterparty.firefly_account
            item.transaction_type = "transfer"
        elif extraction.kind == "transfer":
            reasons.append("ambiguous transfer counterparty")
        if extraction.occurred_at:
            occurred = _aware(extraction.occurred_at, self.zone)
            if occurred > _now() or abs((occurred - item.occurred_at).total_seconds()) > 3 * 86400:
                reasons.append("transaction date is future or differs by more than 3 days")
            else:
                item.occurred_at = occurred
        elif extraction.date_only and extraction.date_only != item.occurred_at.astimezone(self.zone).date():
            reasons.append("date-only extraction differs from receipt date")
        if item.currency != "COP":
            reasons.append("foreign currency requires confirmed COP amount")
        if item.amount is None or item.amount <= 0 or item.amount > self.settings.review_amount_cop:
            reasons.append("amount absent, invalid, or over review threshold")
        if not item.merchant or normalize_merchant(item.merchant) != item.merchant or "$" in item.merchant or len(item.merchant) > 60:
            reasons.append("merchant is not clean")
        await rules.apply(self.session, item)
        reasons.extend(await flags.policy_reasons(self.session, item))
        if reasons:
            item.status, item.review_reason = "needs_review", "; ".join(dict.fromkeys(reasons))

    async def _split(self, item):
        asset = await self._asset(item.asset_account)
        kind = item.transaction_type
        other = await self._asset(item.destination_account) if kind == "transfer" else await self._merchant(item.merchant, kind)
        source, destination = (other, asset) if kind in {"deposit", "refund"} else (asset, other)
        automatic = ["manual"] if item.source == "manual" else ["auto", item.source]
        split = {"type": "deposit" if kind == "refund" else kind, "date": item.occurred_at.astimezone(self.zone).isoformat(), "amount": str(item.amount.quantize(Decimal("0.01"))), "currency_code": item.currency, "description": item.description or item.subject_or_title or item.merchant, "source_id": str(source["id"]), "destination_id": str(destination["id"]), "external_id": "fa:" + item.source_ref, "tags": automatic + [tag for tag in item.tags or [] if tag not in automatic]}
        if item.category:
            split["category_name"] = item.category
        return {"apply_rules": True, "error_if_duplicate_hash": True, "transactions": [split]}

    @asynccontextmanager
    async def _economic_guard(self, item):
        # Per-event-key, never a global network lock. Ingestion stays independent.
        key = f"{item.asset_account}|{item.amount}|{item.currency}|{item.transaction_type}"
        if self.session.bind.dialect.name == "postgresql":
            lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
            async with self.session.bind.connect() as connection:
                acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id})
                try:
                    yield acquired
                finally:
                    if acquired:
                        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})
        else:
            lock = _ECONOMIC_LOCKS.setdefault(key, asyncio.Lock())
            if lock.locked():
                yield False
            else:
                async with lock:
                    yield True

    async def process(self, item, dry_run=False, cycle_id=None, approved=False):
        if dry_run:
            return {"inbox_id": item.id, "status": item.status, "amount": item.amount, "method": item.method}
        claimed = await self.inbox.claim(item.id, approved=approved)
        if claimed is None:
            return await self.inbox.get(item.id)
        item = claimed
        old_status = item.status
        try:
            if item.method is None:
                await self._extract(item, cycle_id)
            if approved:
                item.status, item.review_reason = "pending", None
            if item.status in {"ignored", "needs_review"}:
                return await self._finish(item, old_status)
            async with self._economic_guard(item) as acquired:
                if not acquired:
                    item.next_attempt_at = _now() + timedelta(seconds=5)
                    return await self._finish(item, old_status)
                # Every attempt, even the first, adopts any externally committed result.
                found = await self.firefly.find_by_external_id("fa:" + item.source_ref)
                if found:
                    item.firefly_transaction_id, item.status = str(found), "created"
                    return await self._finish(item, old_status)
                duplicate = await self.inbox.find_duplicate(item, self.settings.dedup_window_minutes)
                if duplicate and not approved:
                    if item.source == "manual" or duplicate.source == "manual":
                        item.status, item.review_reason = "needs_review", "possible duplicate manual/automatic transaction"
                    else:
                        item.status, item.duplicate_of_id = "duplicate", duplicate.id
                    return await self._finish(item, old_status)
                if item.transaction_type == "transfer":
                    window = timedelta(minutes=self.settings.transfer_pair_window_minutes)
                    paired = await self.session.scalar(select(InboxItem).where(InboxItem.id != item.id, InboxItem.status == "created", InboxItem.transaction_type == "transfer", InboxItem.amount == item.amount, InboxItem.currency == item.currency, InboxItem.asset_account == item.asset_account, InboxItem.destination_account == item.destination_account, InboxItem.original_kind != item.original_kind, InboxItem.occurred_at.between(item.occurred_at - window, item.occurred_at + window)).limit(1))
                    if paired and not approved:
                        item.status, item.duplicate_of_id = "duplicate", paired.id
                        return await self._finish(item, old_status)
                item.status = "submitting"
                await self.session.commit()
                payload = await self._split(item)
                item.firefly_transaction_id = str(await self.firefly.create_transaction(payload))
                item.status, item.last_error, item.next_attempt_at = "created", None, None
                # Commit before releasing the economic guard: another channel must see it.
                return await self._finish(item, old_status)
        except PermanentError as exc:
            item.status = "failed" if item.source == "manual" else "needs_review"
            item.review_reason = item.last_error = str(exc)[:1000]
        except Exception:
            if item.amount is None:
                item.method = None
            item.attempts += 1
            item.last_error = "Upstream processing failed; retry safely by external_id"
            if item.source == "manual" or _now() - item.created_at >= timedelta(hours=self.settings.fail_after_hours):
                item.status, item.next_attempt_at = "failed", None
            else:
                item.status = "submitting" if item.status == "submitting" else "pending"
                item.next_attempt_at = _now() + timedelta(minutes=min(720, 5 * 2 ** min(item.attempts - 1, 8)))
        return await self._finish(item, old_status)

    async def _finish(self, item, old_status):
        item.claim_token = item.claim_until = None
        self.session.add(InboxEvent(inbox_id=item.id, action=item.status, detail=item.review_reason or item.last_error))
        await self.session.commit()
        await self._announce(item, old_status)
        return item

    async def _announce(self, item, old_status):
        """Item alerts are best-effort: a failure here must never fail the finance operation."""
        try:
            async with self.session.begin_nested():
                await alerting.item_event(self.session, self.settings, item, old_status)
            await self.session.commit()
        except Exception as exc:
            logger.error("item_alert_failed", inbox_id=item.id, error_type=type(exc).__name__)

    async def cycle(self, dry_run=False):
        poll = {"skipped": True} if dry_run else await self.poll_mail()
        run = CycleRun() if not dry_run else None
        if run:
            self.session.add(run)
            await self.session.commit()
        counts, previews = {}, []
        for item in await self.inbox.due():
            processed = await self.process(item, dry_run=dry_run, cycle_id=str(run.id) if run else None)
            status = processed["status"] if dry_run else processed.status
            counts[status] = counts.get(status, 0) + 1
            if dry_run:
                previews.append(processed)
        result = {"mail": poll, "processed": sum(counts.values()), "by_status": counts, "dry_run": dry_run}
        if dry_run:
            result["previews"] = previews
        else:
            run.finished_at, run.result = _now(), json.dumps(result)
            await self._purge()
            await self.session.commit()
            await self.alerts()
        return result

    async def alerts(self):
        """Periodic checks after each cycle. A failing check is logged and never aborts the others or the cycle."""
        today = _now().astimezone(self.zone).date()

        async def old_pending():
            await alerting.check_old_pending(self.session, await self.inbox.statistics())

        async def phone_silence():
            await alerting.check_phone_silence(self.session, self.settings)

        async def budgets():
            await alerting.check_budgets(self.session, self.firefly, today)

        async def key_expiring():
            await alerting.check_key_expiring(self.session, self.settings)

        async def backup_failed():
            await alerting.check_backup(self.session, self.settings)

        async def month_close():
            if (today + timedelta(days=1)).day == 1:
                month, final = today.replace(day=1), False
            elif today.day == 1:
                month, final = (today - timedelta(days=1)).replace(day=1), True
            else:
                return
            await alerting.month_close(self.session, await self.report("month", month, insight=False), month, final)

        async def report_alerts():
            from app.services import reports
            await reports.alert_check(self.session, self.settings, self.firefly)

        async def anomaly_alerts():
            from app.services import anomalies
            await anomalies.alert_check(self.session, self.settings, self.firefly)

        for check in (old_pending, phone_silence, budgets, key_expiring, backup_failed, month_close, report_alerts, anomaly_alerts):
            try:
                await check()
                await self.session.commit()
            except Exception as exc:
                await self.session.rollback()
                logger.error("alert_check_failed", check=check.__name__, error_type=type(exc).__name__)

    async def _purge(self):
        from sqlalchemy import update
        # Raw text lives RAW_TEXT_RETENTION_DAYS in every status (contract section 10); only rows that still hold text are touched.
        await self.session.execute(update(InboxItem).where(InboxItem.created_at < _now() - timedelta(days=self.settings.raw_text_retention_days), InboxItem.raw_text.is_not(None)).values(raw_text=None))
        await self.session.execute(update(InboxItem).where(InboxItem.status.in_(["ignored", "rejected"]), InboxItem.created_at < _now() - timedelta(days=self.settings.raw_text_retention_days)).values(subject_or_title=""))

    async def learn_senders(self):
        candidates, errors = set(), {}
        for account in self.settings.mail_accounts:
            try:
                _, _, messages = await IMAPClient(account).poll(None, 0, self.settings.email_lookback_days, want_body=lambda h: False)
                candidates.update(m.from_address for m in messages if m.from_address)
            except Exception:
                errors[account.name] = "Mail sender discovery failed"
        unknown = [s for s in sorted(candidates) if not await self.senders.matches(s)]
        learned = 0
        if unknown:
            for domain in await self.llm.learn_senders(unknown, cycle_id=uuid4().hex):
                if any(s.rsplit("@", 1)[-1] == domain for s in unknown):
                    try:
                        await self.senders.add(domain, source="learned", status="pending_approval")
                        learned += 1
                    except ValueError:
                        pass
        await self.session.commit()
        return {"senders_seen": len(candidates), "senders_learned": learned, "errors": errors}

    @staticmethod
    def _clean_tags(value):
        if value is None:
            return []
        if not isinstance(value, list | tuple) or len(value) > 10:
            raise ValueError("Invalid tags")
        tags = []
        for tag in value:
            tag = tag.strip() if isinstance(tag, str) else ""
            if not tag or len(tag) > 40:
                raise ValueError("Invalid tags")
            if tag not in tags:
                tags.append(tag)
        return tags

    async def _audit_review(self, item, action, changes, principal):
        await audit.record(self.session, action="inbox." + action, target_type="inbox_item", target_id=item.id, detail={"changes": changes}, principal=principal)

    async def review(self, item, action, corrections=None, principal=None):
        if action == "approve" and item.status == "created":
            raise ValueError("Edit already-created transactions instead of approving")
        if item.status == "submitting" or (item.claim_until and item.claim_until > _now()):
            raise ValueError("Item is currently submitting; retry after reconciliation")
        changes = {}
        if action == "reject":
            if item.status == "created":
                raise ValueError("Void created items instead of rejecting")
            changes["status"] = {"old": item.status, "new": "rejected"}
            item.status = "rejected"
        elif action == "void":
            if item.firefly_transaction_id:
                await self.firefly.delete_transaction(item.firefly_transaction_id)
            changes["status"] = {"old": item.status, "new": "voided"}
            item.status = "voided"
        else:
            corrections = corrections or {}
            for key, value in corrections.items():
                field = "asset_account" if key == "firefly_account" else key
                old = getattr(item, field, None)
                if field == "occurred_at":
                    value = _aware(value, self.zone)
                if field == "amount":
                    value = Decimal(str(value))
                    if value <= 0 or value > self.settings.max_amount_cop or value.as_tuple().exponent < -2:
                        raise ValueError("Invalid amount")
                    value = value.quantize(Decimal("0.01"))
                if field == "category" and value:
                    value = next((c for c in await self.categories() if c.casefold() == value.casefold()), None)
                    if value is None:
                        raise ValueError("Unknown category")
                if field == "merchant":
                    value = normalize_merchant(value)
                if field == "description":
                    value = (value or "").strip() or None
                    if value and len(value) > 500:
                        raise ValueError("Description is too long")
                if field == "tags":
                    value, old = self._clean_tags(value), list(old or [])
                setattr(item, field, value)
                if old != value:
                    changes[key] = {"old": old, "new": value}
            if not item.amount or not item.asset_account or not item.merchant:
                raise ValueError("Review requires amount, merchant and own account")
            if item.currency != "COP":
                raise ValueError("Foreign currency requires a corrected COP amount/currency")
            await self._asset(item.asset_account)
            if action == "edit":
                if item.status != "created":
                    raise ValueError("Only created items can be edited")
                payload = await self._split(item)
                if "category" in corrections and not item.category:
                    # Firefly keeps the current category when the key is absent; null clears it.
                    payload["transactions"][0]["category_name"] = None
                await self.firefly.update_transaction(item.firefly_transaction_id, payload)
            else:
                item.method = item.method or "review"
                item.status = "needs_review"
                if changes:
                    await self._audit_review(item, action, changes, principal)
                await self.session.commit()
                return await self.process(item, approved=True)
        await self._audit_review(item, action, changes, principal)
        self.session.add(InboxEvent(inbox_id=item.id, action=action, detail=json.dumps(corrections, default=str)))
        await self.session.commit()
        return item

    async def reconcile(self):
        voided = []
        rows = list((await self.session.scalars(select(InboxItem).where(InboxItem.status == "created"))).all())
        for item in rows:
            found = await self.firefly.find_by_external_id("fa:" + item.source_ref)
            if not found:
                item.status = "voided"
                voided.append(item.id)
                self.session.add(InboxEvent(inbox_id=item.id, action="reconciled_void"))
        known = {str(i.firefly_transaction_id) for i in rows}
        orphaned = [row for row in await self.firefly.list_auto_transactions() if str(row["id"]) not in known]
        await self.session.commit()
        if voided or orphaned:
            await alerting.emit(self.session, "system_failure", title="Conciliación con diferencias", body=f"{len(voided)} movimientos ya no existen en Firefly y {len(orphaned)} transacciones de Firefly no tienen registro en la bandeja.", dedupe_key="reconcile", min_interval_minutes=360)
            await self.session.commit()
        return {"voided": voided, "orphaned": orphaned}

    async def report(self, period="day", date=None, insight=True):
        day = date or _now().astimezone(self.zone).date()
        if isinstance(day, str):
            from datetime import date as date_type
            day = date_type.fromisoformat(day)
        start = day - timedelta(days=day.weekday()) if period == "week" else day.replace(day=1) if period == "month" else day
        end = start + timedelta(days=7 if period == "week" else 1)
        if period == "month":
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        begin = datetime.combine(start, time.min, self.zone).astimezone(UTC)
        finish = datetime.combine(end, time.min, self.zone).astimezone(UTC)
        rows = (await self.session.scalars(select(InboxItem).where(InboxItem.status == "created", InboxItem.occurred_at >= begin, InboxItem.occurred_at < finish))).all()
        totals = {k: Decimal(0) for k in ("withdrawal", "deposit", "transfer", "refund")}
        categories, accounts, merchants = {}, {}, {}
        for row in rows:
            amount = row.amount or Decimal(0)
            totals[row.transaction_type] = totals.get(row.transaction_type, Decimal(0)) + amount
            for mapping, key in [(categories, row.category or "Sin categoría"), (accounts, row.asset_account or "Sin cuenta"), (merchants, row.merchant or "Sin comercio")]:
                mapping[key] = mapping.get(key, Decimal(0)) + amount
        result = {"period": period, "start": start.isoformat(), "end": end.isoformat(), "totals": {k: str(v) for k, v in totals.items()}, "by_category": {k: str(v) for k, v in categories.items()}, "by_account": {k: str(v) for k, v in accounts.items()}, "top_merchants": [{"merchant": k, "amount": str(v)} for k, v in sorted(merchants.items(), key=lambda pair: pair[1], reverse=True)[:5]]}
        if insight and hasattr(self.firefly, "insight"):
            try:
                result["firefly_insight"] = await self.firefly.insight(start, end - timedelta(days=1))
            except Exception:
                result["firefly_insight"] = None
        return result

    async def digest(self, weekly=False):
        day = _now().astimezone(self.zone).date() - timedelta(days=1)
        period = "week" if weekly else "day"
        report = await self.report(period, day)
        previous = await self.report(period, day - timedelta(days=7 if weekly else 1))
        stats = await self.inbox.statistics()
        await alerting.digest(self.session, report, previous, stats, weekly)
        await self.session.commit()
        return report
