"""Repository-only access; callers own transaction commits."""
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_utc
from app.db.models import InboxItem, KnownSender, MailAccountState


class InboxRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, **kwargs) -> InboxItem:
        row = InboxItem(**kwargs)
        self.session.add(row)
        await self.save()
        return row

    async def save(self):
        await self.session.flush()

    async def get(self, item_id: int) -> InboxItem | None:
        return await self.session.get(InboxItem, item_id, populate_existing=True)

    async def by_ref(self, ref: str) -> InboxItem | None:
        return await self.session.scalar(select(InboxItem).where(InboxItem.source_ref == ref))

    async def list(self, status=None, source=None, method=None, limit=50):
        query = select(InboxItem)
        for key, value in (("status", status), ("source", source), ("method", method)):
            if value:
                query = query.where(getattr(InboxItem, key) == value)
        return list((await self.session.scalars(query.order_by(InboxItem.id.desc()).limit(limit))).all())

    async def count(self, status=None, source=None, method=None):
        query = select(func.count()).select_from(InboxItem)
        for key, value in (("status", status), ("source", source), ("method", method)):
            if value:
                query = query.where(getattr(InboxItem, key) == value)
        return await self.session.scalar(query)

    async def due(self):
        query = select(InboxItem).where(InboxItem.status == "pending", or_(InboxItem.next_attempt_at.is_(None), InboxItem.next_attempt_at <= now_utc())).order_by(InboxItem.id)
        return list((await self.session.scalars(query)).all())

    async def find_duplicate(self, item: InboxItem, window_minutes: int):
        if item.source not in {"email", "notification", "sms"}:
            return None
        opposite = ["notification", "sms"] if item.source == "email" else ["email"]
        window = timedelta(minutes=window_minutes)
        query = select(InboxItem).where(InboxItem.status == "created", InboxItem.id != item.id, InboxItem.source.in_(opposite), InboxItem.asset_account == item.asset_account, InboxItem.amount == item.amount, InboxItem.currency == item.currency, InboxItem.transaction_type == item.transaction_type, InboxItem.occurred_at >= item.occurred_at - window, InboxItem.occurred_at <= item.occurred_at + window).order_by(InboxItem.id).limit(1)
        return await self.session.scalar(query)

    async def requeue(self, item_id=None, failed_only=False, **kwargs):
        if item_id is None:
            item_id = kwargs.get("id")
        query = select(InboxItem)
        if item_id is not None:
            query = query.where(InboxItem.id == item_id)
        else:
            query = query.where(InboxItem.status == "failed")
        count = 0
        for row in (await self.session.scalars(query)).all():
            if row.status not in {"pending", "failed"}:
                continue
            row.status, row.attempts, row.next_attempt_at, row.last_error = "pending", 0, None, None
            count += 1
        await self.save()
        return count

    async def statistics(self):
        result = {}
        for key in ("status", "source", "method"):
            field = getattr(InboxItem, key)
            pairs = (await self.session.execute(select(field, func.count()).group_by(field))).all()
            result[f"by_{key}"] = {name or "unprocessed": count for name, count in pairs}
        result["pending"] = result["by_status"].get("pending", 0)
        result["failed"] = result["by_status"].get("failed", 0)
        result["last_run"] = await self.session.scalar(select(func.max(InboxItem.updated_at)).where(InboxItem.method.is_not(None)))
        return result


class MailStateRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, name):
        row = await self.session.get(MailAccountState, name)
        if row is None:
            row = MailAccountState(name=name, last_uid=0)
            self.session.add(row)
            await self.save()
        return row

    async def all(self):
        return list((await self.session.scalars(select(MailAccountState))).all())

    async def save(self):
        await self.session.flush()


SEED_SENDERS = {
    "bancolombia": "Bancolombia", "nequi": "Nequi", "daviplata": "Daviplata",
    "davivienda": "Davivienda", "lulobank.com": "Lulo Bank", "lulo": "Lulo Bank",
    "nu.com.co": "Nu", "bancofalabella": "Falabella", "bbva": "BBVA",
    "bancodebogota": "Banco de Bogotá", "com.bancolombia.personas.superapp": "Bancolombia",
    "com.nequi.mobileapp": "Nequi", "com.lulobank.lulo": "Lulo Bank",
    "com.davivienda.daviplataapp": "Daviplata", "com.nu.production": "Nu",
    "com.davivienda.daviviendaapp": "Davivienda",
    "87400": "Bancolombia", "890099": "Nequi",
}


class KnownSenderRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def all(self, include_inactive=False):
        query = select(KnownSender).order_by(KnownSender.keyword)
        if not include_inactive:
            query = query.where(KnownSender.active.is_(True))
        return list((await self.session.scalars(query)).all())

    async def add(self, keyword, name="", source="manual"):
        keyword = keyword.strip().lower()
        row = await self.session.scalar(select(KnownSender).where(KnownSender.keyword == keyword))
        if row is None:
            row = KnownSender(keyword=keyword, name=name, source=source, active=True)
            self.session.add(row)
        else:
            row.active = True
            if name:
                row.name = name
        await self.session.flush()
        return row

    async def delete(self, keyword):
        row = await self.session.scalar(select(KnownSender).where(KnownSender.keyword == keyword.lower()))
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True

    async def matches(self, sender):
        return any(row.keyword in sender.lower() for row in await self.all())

    async def seed(self):
        existing = {row.keyword for row in await self.all(include_inactive=True)}
        for keyword, name in SEED_SENDERS.items():
            if keyword not in existing:
                await self.add(keyword, name, "seed")
        return len(set(SEED_SENDERS) - existing)
