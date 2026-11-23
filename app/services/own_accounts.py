"""Own accounts: the single runtime source for «which Firefly account does this source feed» (contract section 7).

The ``own_accounts`` table is authoritative. Every consumer (processor, quick entry, health) reads it through
``load``, which keeps one immutable snapshot per process. The API invalidates the snapshot after each committed
write; the next ``load`` rebuilds it from the database, so a change applies to the very next ingest without a
restart. ``load`` loads lazily, therefore no code path can see an empty list merely because nothing was loaded yet.

Production runs one uvicorn worker (see ``Dockerfile.prod``). With several processes each would keep its own
snapshot and a write would reach the others only after a restart.

``OWN_ACCOUNTS`` (``Settings.own_accounts``) is only the initial seed, see ``seed``.
"""
import re
from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models_assistant import Audit
from app.db.repositories import OwnAccountRepository
from app.security.sender_trust import sender_rule
from app.services import audit

logger = get_logger(__name__)

_PACKAGE = re.compile(r"[a-zA-Z]\w*(\.[a-zA-Z]\w*)+", re.ASCII)
_MAX_PACKAGE_CHARS = 255
_MAX_SMS_CHARS = 64
_SOURCE_PREFIX = {"notification": "app", "sms": "sms", "email": "email"}


class InvalidSourceError(ValueError):
    """A source is not ``app:<package>``, ``sms:<sender>`` or ``email:<address or domain>``."""


@dataclass(frozen=True)
class OwnAccountView:
    """Immutable copy of a row, safe to share between requests, cycles and the cache."""
    firefly_account: str
    bank: str
    kind: str
    last4: tuple[str, ...]
    aliases: tuple[str, ...]
    sources: tuple[str, ...]


_snapshot: tuple[OwnAccountView, ...] | None = None
_generation = 0


def invalidate() -> None:
    """Drop the snapshot. Call it after the commit of every write to the table, never before."""
    global _snapshot, _generation
    _generation += 1
    _snapshot = None


async def load(session) -> tuple[OwnAccountView, ...]:
    """Current own accounts, from the snapshot or, when there is none, from the database."""
    global _snapshot
    cached = _snapshot
    if cached is not None:
        return cached
    generation = _generation
    rows = await OwnAccountRepository(session).all()
    loaded = tuple(OwnAccountView(row.firefly_account, row.bank, row.kind, tuple(row.last4 or ()), tuple(row.aliases or ()), tuple(row.sources or ())) for row in rows)
    # A write committed while this query ran invalidated the generation: the rows read may predate it, so do not keep them.
    if generation == _generation:
        _snapshot = loaded
    return loaded


def for_source(accounts, source: str, identity: str) -> list:
    """Accounts fed by a notification app (``notification``), SMS sender (``sms``) or mail address (``email``)."""
    prefix = _SOURCE_PREFIX[source]
    address = identity.casefold()
    domain = address.rsplit("@", 1)[-1]
    result = []
    for account in accounts:
        for configured in account.sources:
            kind, _, value = configured.casefold().partition(":")
            if kind == prefix and (value == address or (kind == "email" and (domain == value or domain.endswith("." + value)))):
                result.append(account)
                break
    return result


def email_rules(accounts) -> list:
    """Trusted-sender rules (always requiring aligned authentication) for the ``email:`` sources."""
    return [SimpleNamespace(keyword=s.split(":", 1)[1], status="active", require_auth=True) for a in accounts for s in a.sources if s.casefold().startswith("email:")]


def normalize_source(raw: str) -> str | None:
    """Canonical form of a source, or None when it is not valid.

    ``app:`` takes an Android package name (case-sensitive on Android, so it is stored as typed) and ``sms:`` a sender
    id (1-64 printable characters); both lose surrounding spaces. ``email:`` takes an exact address or a domain
    accepted by ``sender_rule`` (generic mailbox providers are rejected) and is lower-cased. Sources are compared
    without regard to case everywhere (``for_source``, duplicates here).
    """
    kind, separator, value = raw.strip().partition(":")
    if not separator:
        return None
    kind, value = kind.casefold(), value.strip()
    if kind == "app":
        return "app:" + value if len(value) <= _MAX_PACKAGE_CHARS and _PACKAGE.fullmatch(value) else None
    if kind == "sms":
        return "sms:" + value if 1 <= len(value) <= _MAX_SMS_CHARS and value.isprintable() else None
    if kind == "email" and not value.casefold().startswith("email:"):
        parsed = sender_rule(value)
        return "email:" + (parsed[0] or parsed[1]) if parsed else None
    return None


def normalize_sources(values) -> list[str]:
    """Normalized sources without duplicates (same source ignoring case); raises ``InvalidSourceError`` for the first bad one."""
    result, seen = [], set()
    for raw in values:
        source = normalize_source(raw)
        if source is None:
            raise InvalidSourceError(raw.strip()[:80])
        if source.casefold() not in seen:
            seen.add(source.casefold())
            result.append(source)
    return result


async def seed(session, settings) -> int:
    """Import ``Settings.own_accounts`` once; returns how many accounts were created.

    This is the deployed configuration's only entry into the table. It runs only while the table is empty AND no
    own-account audit entry (``own_account.seed``, ``.add``, ``.update``, ``.delete``) exists, so accounts the owner
    deleted from the app never reappear from the file after a restart. Sources that no longer pass validation are
    skipped and counted in the log. Commits itself.
    """
    configured = settings.own_accounts
    repository = OwnAccountRepository(session)
    if not configured or await repository.count():
        return 0
    if await session.scalar(select(Audit.id).where(Audit.action.startswith("own_account.")).limit(1)) is not None:
        return 0
    created, skipped = [], 0
    for account in configured:
        name = account.firefly_account.strip()
        if not name or name.casefold() in {row.firefly_account.casefold() for row in created}:
            continue
        valid = [source for source in account.sources if normalize_source(source)]
        skipped += len(account.sources) - len(valid)
        created.append(await repository.add(
            firefly_account=name, bank=account.bank, kind=account.kind, last4=account.last4,
            aliases=account.aliases, sources=normalize_sources(valid),
        ))
    await audit.record(
        session, action="own_account.seed", target_type="own_account", target_id="settings",
        detail={"count": len(created), "accounts": [row.firefly_account for row in created], "skipped_sources": skipped},
    )
    await session.commit()
    invalidate()
    logger.info("own_accounts_seeded", count=len(created), skipped_sources=skipped)
    return len(created)
