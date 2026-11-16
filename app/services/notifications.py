"""Best-effort push delivery with durable cross-process throttling."""
from datetime import timedelta

import httpx
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.core.timezone import now_utc
from app.db.models import Notification


class Notifier:
    def __init__(self, session, settings):
        self.session = session
        self.settings = settings

    async def send(self, key, message, minutes=360, silent=False):
        if not self.settings.notify_url:
            return False
        now = now_utc()
        try:
            row = await self.session.get(Notification, key)
            if row is None:
                try:
                    async with self.session.begin_nested():
                        self.session.add(Notification(key=key, last_sent_at=now))
                        await self.session.flush()
                except IntegrityError:
                    return False
            else:
                result = await self.session.execute(update(Notification).where(Notification.key == key, Notification.last_sent_at <= now - timedelta(minutes=minutes)).values(last_sent_at=now).execution_options(synchronize_session=False))
                if not result.rowcount:
                    return False
            await self.session.commit()
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await client.post(self.settings.notify_url, headers={"x-api-key": self.settings.notify_api_key.get_secret_value()}, json={"message": message[:4000], "silent": silent})
                response.raise_for_status()
            return True
        except Exception:
            # Never turn a finance operation into a failure because Telegram is down.
            await self.session.rollback()
            return False
