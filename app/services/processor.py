"""Durable single-owner ingestion and processing, with repository-only persistence."""

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from app.clients.imap import IMAPClient
from app.core.config import get_settings
from app.db.repositories import InboxRepository, KnownSenderRepository, MailStateRepository
from app.models.schemas import TransactionAnalysis
from app.parsers import normalize_merchant, parse_transaction, should_ignore

_PROCESS_LOCK = asyncio.Lock()
_POLL_LOCK = asyncio.Lock()
_CACHE_LOCK = asyncio.Lock()
_CACHES = WeakKeyDictionary()
_BACKOFF_MINUTES = (5, 15, 60, 180, 360, 720)


def _now():
    return datetime.now(UTC)


def _aware(value, local_zone):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone)
    return parsed.astimezone(UTC)


class Processor:
    def __init__(self, session, firefly, llm, settings=None):
        self.session = session
        self.firefly = firefly
        self.llm = llm
        self.settings = settings or get_settings()
        self.zone = ZoneInfo(self.settings.timezone)
        self.inbox = InboxRepository(session)
        self.senders = KnownSenderRepository(session)
        self.mail_states = MailStateRepository(session)

    async def sync(self, force=False):
        async with _CACHE_LOCK:
            cache = _CACHES.get(self.firefly)
            ttl = self.settings.firefly_cache_ttl_minutes * 60
            if force or cache is None or monotonic() - cache["at"] >= ttl:
                accounts, categories = await asyncio.gather(
                    self.firefly.accounts(), self.firefly.categories()
                )
                cache = {"at": monotonic(), "accounts": accounts, "categories": categories}
                _CACHES[self.firefly] = cache
            return {"accounts": len(cache["accounts"]), "categories": len(cache["categories"])}

    async def accounts(self):
        await self.sync()
        return _CACHES[self.firefly]["accounts"]

    async def categories(self):
        await self.sync()
        return _CACHES[self.firefly]["categories"]

    def _notification_date(self, payload):
        if payload.timestamp:
            try:
                return datetime.fromtimestamp(float(Decimal(payload.timestamp) / 1000), UTC)
            except (ValueError, TypeError, ArithmeticError, OverflowError):
                try:
                    return _aware(payload.timestamp, self.zone)
                except (ValueError, TypeError):
                    pass
        if payload.date:
            return _aware(payload.date, self.zone)
        return _now()

    async def ingest_notification(self, payload):
        content = payload.big_text or payload.message or payload.text
        source = "sms" if payload.type == "sms" else "notification"
        fingerprint = "|".join((payload.app, payload.sender, content, payload.timestamp or payload.date))
        ref = hashlib.sha256(fingerprint.encode()).hexdigest()
        # Serialize reference lookup and insertion, including the persistence boundary.
        async with _POLL_LOCK:
            existing = await self.inbox.by_ref(ref)
            if existing:
                return existing, False
            identity = payload.sender if source == "sms" else payload.app
            known = bool(identity) and await self.senders.matches(identity)
            item = await self.inbox.add(
                source=source, source_ref=ref, app=payload.app, sender=payload.sender,
                subject_or_title=payload.title, raw_text=content if known else None,
                occurred_at=self._notification_date(payload),
                status="pending" if known else "ignored",
            )
            await self.session.commit()
            return item, bool(known)

    async def poll_mail(self):
        result = {"queued": 0, "ignored": 0, "duplicates": 0, "errors": {}}
        async with _POLL_LOCK:
            for account in self.settings.mail_accounts:
                state = await self.mail_states.get(account.name)
                try:
                    validity, highwater, messages = await IMAPClient(account).poll(
                        state.uidvalidity, state.last_uid, self.settings.email_lookback_days
                    )
                    for message in messages:
                        ref = f"{account.name}:{validity}:{message['uid']}"
                        if await self.inbox.by_ref(ref):
                            result["duplicates"] += 1
                            continue
                        known = await self.senders.matches(message["sender"])
                        await self.inbox.add(
                            source="email", source_ref=ref, account_name=account.name,
                            sender=message["sender"], subject_or_title=message["subject"],
                            raw_text=message["body"][:self.settings.email_max_body_chars] if known else None,
                            occurred_at=_aware(message["occurred_at"], self.zone),
                            status="pending" if known else "ignored",
                        )
                        result["queued" if known else "ignored"] += 1
                    # Persist messages and high-water mark atomically, before processing.
                    state.uidvalidity = validity
                    state.last_uid = highwater
                    state.last_error = None
                    state.last_poll_at = _now()
                    await self.mail_states.save()
                    await self.session.commit()
                except Exception as exc:
                    # Any already inserted messages remain durable; the unchanged cursor replays safely.
                    state.last_poll_at = _now()
                    state.last_error = str(exc)
                    await self.mail_states.save()
                    await self.session.commit()
                    result["errors"][account.name] = str(exc)
        return result

    def _asset_name(self, item):
        mapping = self.settings.account_map
        if item.source == "manual":
            return item.asset_account or self.settings.manual_default_account
        if item.source == "notification" and item.app in mapping:
            return mapping[item.app]
        if item.source == "sms":
            for key, name in mapping.items():
                if key.casefold() == f"sms:{item.sender}".casefold():
                    return name
        if item.source == "email":
            for key, name in mapping.items():
                if key and not key.startswith("sms:") and key.casefold() in item.sender.casefold():
                    return name
        return self.settings.firefly_default_asset_account

    def _transaction_date(self, date, occurred_at):
        occurred_at = _aware(occurred_at, self.zone)
        if not date:
            return occurred_at
        parsed = _aware(date, self.zone)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date.strip()):
            if parsed.astimezone(self.zone).date() == occurred_at.astimezone(self.zone).date():
                return occurred_at
        return parsed

    async def _asset(self, name):
        for refresh in (False, True):
            if refresh:
                await self.sync(force=True)
            for account in await self.accounts():
                if account["type"] == "asset" and account["name"].casefold() == name.casefold():
                    return account
        raise ValueError(f"Firefly asset account does not exist: {name}")

    async def _merchant(self, merchant, transaction_type):
        name = normalize_merchant(merchant) or ("INGRESO" if transaction_type == "deposit" else "COMERCIO")
        account_type = "revenue" if transaction_type == "deposit" else "expense"
        for refresh in (False, True):
            if refresh:
                await self.sync(force=True)
            for account in await self.accounts():
                existing = normalize_merchant(account["name"])
                if account["type"] != account_type:
                    continue
                if existing == name or (
                    min(len(existing), len(name)) >= 10
                    and (existing.startswith(name) or name.startswith(existing))
                ):
                    return account
        account = await self.firefly.create_account(name, account_type)
        _CACHES[self.firefly]["accounts"].append(account)
        return account

    async def manual(self, payload):
        item = await self.inbox.add(
            source="manual", source_ref=f"manual:{uuid4().hex}", sender="owner",
            raw_text=payload.description, subject_or_title=payload.description,
            occurred_at=_aware(payload.date, self.zone) if payload.date else _now(),
            status="pending", method="manual", amount=payload.amount, currency="COP",
            merchant=normalize_merchant(payload.merchant or payload.description),
            category=payload.category, asset_account=payload.account or self.settings.manual_default_account,
            transaction_type=payload.type,
        )
        await self.session.commit()
        return await self.process(item)

    async def _preview(self, item, analysis, method):
        preview = {
            "inbox_id": item.id, "method": method,
            "amount": None, "currency": None, "merchant": None, "category": None,
            "asset_account": None, "transaction_type": None,
            "date": item.occurred_at.astimezone(self.zone).isoformat(),
            "duplicate_of": None, "status": "ignored",
        }
        if analysis is None or not analysis.is_transaction:
            return preview
        occurred_at = self._transaction_date(analysis.date, item.occurred_at)
        values = {
            "amount": Decimal(str(analysis.amount)), "currency": analysis.currency,
            "merchant": normalize_merchant(analysis.merchant or analysis.description),
            "category": analysis.category if analysis.category in await self.categories() else None,
            "asset_account": self._asset_name(item), "transaction_type": analysis.transaction_type,
        }
        candidate = SimpleNamespace(id=item.id, source=item.source, occurred_at=occurred_at, **values)
        duplicate = await self.inbox.find_duplicate(candidate, self.settings.dedup_window_minutes)
        preview.update(values)
        preview.update(date=occurred_at.astimezone(self.zone).isoformat(),
                       duplicate_of=duplicate.id if duplicate else None,
                       status="duplicate" if duplicate else "created")
        return preview

    async def process(self, item, dry_run=False, cycle_id=None):
        async with _PROCESS_LOCK:
            # Refresh under lock: another request may have completed this same row.
            item = await self.inbox.get(item.id)
            if item.status != "pending":
                return item
            if item.next_attempt_at and item.next_attempt_at > _now():
                return item
            try:
                if item.source == "manual":
                    analysis = TransactionAnalysis(
                        amount=item.amount, currency=item.currency, merchant=item.merchant,
                        description=item.raw_text or "", category=item.category,
                        transaction_type=item.transaction_type,
                    )
                    method = "manual"
                elif should_ignore(item.raw_text or "", item.subject_or_title or ""):
                    if dry_run:
                        return await self._preview(item, None, "rule-ignore")
                    if not dry_run:
                        item.status, item.method = "ignored", "rule-ignore"
                        await self.inbox.save()
                        await self.session.commit()
                    return item
                else:
                    analysis = parse_transaction(
                        item.raw_text or "", item.subject_or_title or "", item.sender or ""
                    )
                    method = "parser"
                    if analysis is None:
                        method = "llm"
                        analysis = await self.llm.analyze(
                            "\n".join(filter(None, (item.subject_or_title, item.raw_text))),
                            categories=await self.categories(),
                            accounts=[a["name"] for a in await self.accounts() if a["type"] == "asset"],
                            cycle_id=cycle_id or uuid4().hex,
                        )
                    if not isinstance(analysis, TransactionAnalysis):
                        analysis = TransactionAnalysis.model_validate(analysis)
                if dry_run:
                    return await self._preview(item, analysis, method)
                item.method = method
                if not analysis.is_transaction:
                    item.status = "ignored"
                else:
                    item.amount = Decimal(str(analysis.amount))
                    item.currency = analysis.currency
                    item.merchant = normalize_merchant(analysis.merchant or analysis.description)
                    item.category = analysis.category if analysis.category in await self.categories() else None
                    item.asset_account = self._asset_name(item)
                    item.transaction_type = analysis.transaction_type
                    item.occurred_at = self._transaction_date(analysis.date, item.occurred_at)
                    duplicate = await self.inbox.find_duplicate(item, self.settings.dedup_window_minutes)
                    if duplicate:
                        item.status = "duplicate"
                        item.duplicate_of_id = duplicate.id
                    else:
                        asset = await self._asset(item.asset_account)
                        merchant = await self._merchant(item.merchant, item.transaction_type)
                        source, destination = (merchant, asset) if item.transaction_type == "deposit" else (asset, merchant)
                        split = {
                            "type": item.transaction_type,
                            "date": item.occurred_at.astimezone(self.zone).isoformat(),
                            "amount": str(item.amount), "currency_code": item.currency,
                            "description": analysis.description or item.subject_or_title or item.merchant,
                            "source_id": str(source["id"]), "destination_id": str(destination["id"]),
                            "external_id": f"fa:{item.source_ref}", "tags": ["auto", item.source],
                        }
                        if item.category:
                            split["category_name"] = item.category
                        item.firefly_transaction_id = await self.firefly.create_transaction({
                            "apply_rules": True, "error_if_duplicate_hash": False,
                            "transactions": [split],
                        })
                        item.status = "created"
                item.last_error = None
                item.next_attempt_at = None
            except Exception as exc:
                if dry_run:
                    raise
                item.attempts += 1
                item.last_error = str(exc)
                if item.attempts >= self.settings.max_attempts:
                    item.status = "failed"
                    item.next_attempt_at = None
                else:
                    delay = _BACKOFF_MINUTES[min(item.attempts - 1, len(_BACKOFF_MINUTES) - 1)]
                    item.next_attempt_at = _now() + timedelta(minutes=delay)
            await self.inbox.save()
            # Visibility must precede releasing the cross-channel serialization lock.
            await self.session.commit()
            return item

    async def cycle(self, dry_run=False):
        # Previewing must not ingest messages or advance IMAP checkpoints.
        poll = {"skipped": True} if dry_run else await self.poll_mail()
        counts = {}
        previews = []
        cycle_id = uuid4().hex
        for item in await self.inbox.due():
            result = await self.process(item, dry_run=dry_run, cycle_id=cycle_id)
            status = result["status"] if dry_run else item.status
            counts[status] = counts.get(status, 0) + 1
            if dry_run:
                previews.append(result)
        result = {"mail": poll, "processed": sum(counts.values()), "by_status": counts, "dry_run": dry_run}
        if dry_run:
            result["previews"] = previews
        return result

    async def learn_senders(self):
        candidates = set()
        errors = {}
        for account in self.settings.mail_accounts:
            try:
                _, _, messages = await IMAPClient(account).poll(None, 0, self.settings.email_lookback_days)
                candidates.update(message["sender"] for message in messages if message["sender"])
            except Exception as exc:
                errors[account.name] = str(exc)
        unknown = [sender for sender in sorted(candidates) if not await self.senders.matches(sender)]
        learned = 0
        if unknown:
            keywords = await self.llm.learn_senders(unknown, cycle_id=uuid4().hex)
            for keyword in dict.fromkeys(word.strip().casefold() for word in keywords if word.strip()):
                # Never trust an invented keyword that matches none of the observed senders.
                if not any(keyword in sender.casefold() for sender in unknown):
                    continue
                if not await self.senders.matches(keyword):
                    await self.senders.add(keyword, source="learned")
                    learned += 1
        await self.session.commit()
        return {"senders_seen": len(candidates), "senders_learned": learned, "errors": errors}
