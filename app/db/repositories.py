"""Durable inbox queries and atomic claims; callers own commits."""
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import String, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_utc
from app.db.models import CycleRun, InboxItem, KnownSender, MailAccountState


class InboxRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, **kwargs):
        row = InboxItem(**kwargs)
        self.session.add(row)
        await self.save()
        return row

    async def save(self):
        await self.session.flush()

    async def get(self, item_id):
        return await self.session.get(InboxItem, item_id, populate_existing=True)

    async def by_ref(self, ref):
        return await self.session.scalar(select(InboxItem).where(InboxItem.source_ref == ref))

    @staticmethod
    def _filtered(query, status=None, source=None, method=None, since=None, until=None, review_reason=None, q=None, tag=None, unparsed=False):
        for key, value in (("status", status), ("source", source), ("method", method)):
            if value:
                query = query.where(getattr(InboxItem, key) == value)
        if since is not None:
            query = query.where(InboxItem.occurred_at >= since)
        if until is not None:
            query = query.where(InboxItem.occurred_at <= until)
        if review_reason:
            query = query.where(InboxItem.review_reason.icontains(review_reason, autoescape=True))
        if q:
            query = query.where(or_(InboxItem.merchant.icontains(q, autoescape=True), InboxItem.description.icontains(q, autoescape=True)))
        if tag:
            # Portable JSON-array membership: match the quoted element in the serialized column.
            query = query.where(cast(InboxItem.tags, String).contains(json.dumps(tag), autoescape=True))
        if unparsed:
            query = query.where(InboxItem.status.in_(["needs_review", "failed"]), or_(InboxItem.amount.is_(None), InboxItem.merchant.is_(None), InboxItem.merchant == ""))
        return query

    async def list(self, status=None, source=None, method=None, limit=50, offset=0, since=None, until=None, review_reason=None, q=None, tag=None, unparsed=False):
        query = self._filtered(select(InboxItem), status, source, method, since, until, review_reason, q, tag, unparsed)
        return list((await self.session.scalars(query.order_by(InboxItem.id.desc()).offset(offset).limit(limit))).all())

    async def count(self, status=None, source=None, method=None, since=None, until=None, review_reason=None, q=None, tag=None, unparsed=False):
        query = self._filtered(select(func.count()).select_from(InboxItem), status, source, method, since, until, review_reason, q, tag, unparsed)
        return await self.session.scalar(query)

    async def due(self):
        query = select(InboxItem).where(InboxItem.status.in_(["pending", "submitting"]), InboxItem.source != "manual", or_(InboxItem.next_attempt_at.is_(None), InboxItem.next_attempt_at <= now_utc()), or_(InboxItem.claim_until.is_(None), InboxItem.claim_until < now_utc())).order_by(InboxItem.id)
        return list((await self.session.scalars(query)).all())

    async def claim(self, item_id, approved=False):
        token = str(uuid4())
        statuses = ["pending", "submitting", "needs_review"] if approved else ["pending", "submitting"]
        query = select(InboxItem).where(InboxItem.id == item_id).with_for_update(skip_locked=True)
        row = await self.session.scalar(query)
        if row is None:
            # Commit, not rollback: rollback expires every loaded instance and later attribute
            # access outside the async context raises MissingGreenlet (sessions use expire_on_commit=False).
            await self.session.commit()
            return None
        result = await self.session.execute(update(InboxItem).where(InboxItem.id == item_id, InboxItem.status.in_(statuses), or_(InboxItem.claim_until.is_(None), InboxItem.claim_until < now_utc())).values(claim_token=token, claim_until=now_utc() + timedelta(minutes=10)).execution_options(synchronize_session=False))
        await self.session.commit()
        return await self.get(item_id) if result.rowcount else None

    async def find_duplicate(self, item, window_minutes):
        window = timedelta(minutes=window_minutes)
        query = select(InboxItem).where(InboxItem.status == "created", InboxItem.id != item.id, InboxItem.source != item.source, InboxItem.asset_account == item.asset_account, InboxItem.amount == item.amount, InboxItem.currency == item.currency, InboxItem.transaction_type == item.transaction_type, InboxItem.occurred_at >= item.occurred_at - window, InboxItem.occurred_at <= item.occurred_at + window).order_by(InboxItem.id).limit(1)
        return await self.session.scalar(query)

    async def requeue(self, item_id=None, failed_only=False, **kwargs):
        query = select(InboxItem)
        query = query.where(InboxItem.id == item_id) if item_id is not None else query.where(InboxItem.status == "failed")
        count = 0
        for row in (await self.session.scalars(query)).all():
            if row.status not in {"ignored", "duplicate", "failed", "rejected", "needs_review"}:
                continue
            row.status, row.attempts, row.next_attempt_at, row.last_error = "pending", 0, None, None
            row.method = "manual" if row.source == "manual" else None
            if row.review_reason != "sender_auth_failed":
                row.review_reason = None
            row.claim_token = row.claim_until = None
            count += 1
        await self.save()
        return count

    async def statistics(self):
        result = {}
        for key in ("status", "source", "method"):
            field = getattr(InboxItem, key)
            pairs = (await self.session.execute(select(field, func.count()).group_by(field))).all()
            result[f"by_{key}"] = {name or "unprocessed": count for name, count in pairs}
        for key in ("pending", "failed", "needs_review"):
            result[key] = result["by_status"].get(key, 0)
        oldest = await self.session.scalar(select(func.min(InboxItem.created_at)).where(InboxItem.status.in_(["pending", "submitting"])))
        result["oldest_pending_age_s"] = max(0, (now_utc() - oldest).total_seconds()) if oldest else None
        cycle = await self.session.scalar(select(CycleRun).order_by(CycleRun.id.desc()).limit(1))
        result["last_cycle"] = {"started_at": cycle.started_at, "finished_at": cycle.finished_at, "result": cycle.result} if cycle else None
        from zoneinfo import ZoneInfo

        from app.core.config import get_settings

        midnight = now_utc().astimezone(ZoneInfo(get_settings().timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        result["amounts_today"] = dict((await self.session.execute(select(InboxItem.transaction_type, func.sum(InboxItem.amount)).where(InboxItem.status == "created", InboxItem.occurred_at >= midnight).group_by(InboxItem.transaction_type))).all())
        return result


class MailStateRepository:
    def __init__(self, session):
        self.session = session

    async def get(self, name):
        row = await self.session.get(MailAccountState, name)
        if row is None:
            row = MailAccountState(name=name, last_uid=0, consecutive_failures=0)
            self.session.add(row)
            await self.save()
        return row

    async def all(self):
        return list((await self.session.scalars(select(MailAccountState))).all())

    async def save(self):
        await self.session.flush()


class KnownSenderRepository:
    def __init__(self, session):
        self.session = session

    async def all(self, include_inactive=True):
        query = select(KnownSender).order_by(KnownSender.keyword)
        if not include_inactive:
            query = query.where(KnownSender.status == "active")
        return list((await self.session.scalars(query)).all())

    async def add(self, keyword, name="", source="manual", status="active", require_auth=True):
        keyword = keyword.strip().casefold().removeprefix("email:")
        if "." not in keyword or "@" in keyword or keyword in {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "live.com", "aol.com"}:
            raise ValueError("An exact non-generic email domain is required")
        row = await self.session.scalar(select(KnownSender).where(KnownSender.keyword == keyword))
        if row is None:
            row = KnownSender(keyword=keyword, name=name, source=source, status=status, require_auth=require_auth)
            self.session.add(row)
        await self.session.flush()
        return row

    async def approve(self, sender_id):
        row = await self.session.get(KnownSender, sender_id)
        if row:
            row.status = "active"
            await self.session.flush()
        return row

    async def delete(self, sender_id):
        row = await self.session.get(KnownSender, sender_id)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True

    async def matches(self, sender):
        domain = sender.casefold().rsplit("@", 1)[-1]
        return any(domain == row.keyword or domain.endswith("." + row.keyword) for row in await self.all(False))
