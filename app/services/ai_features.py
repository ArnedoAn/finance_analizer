"""Switches, confidence threshold and tag vocabulary of the owner-approved AI functions, plus the one way they call the LLM.

Stored in ``runtime_flags``: ``ai_features`` (``{categorize, narrative, anomalies, subscriptions, tags, confidence_threshold}``)
and ``ai_tags`` (``{"tags": [...]}``, the closed vocabulary the model may use). Everything is on by default.
``call`` is the single place where these services reach the model: it asks ``ai_budget.allow`` first when the job is
automatic (the owner's own requests only ``record``), redacts nothing itself (callers send redacted text) and records
the usage even when the call fails. Functions flush but never commit.
"""
import re
from uuid import uuid4

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models_flags import RuntimeFlag
from app.security.redaction import redact
from app.services import ai_budget

logger = get_logger(__name__)

FEATURES = ("categorize", "narrative", "anomalies", "subscriptions", "tags")
FEATURES_KEY = "ai_features"
TAGS_KEY = "ai_tags"
DEFAULT_THRESHOLD = 0.85
MIN_THRESHOLD = 0.5
DEFAULT_TAGS = ("viaje", "trabajo", "salud", "hogar", "mascotas", "educación", "regalos", "ocio")
MAX_VOCABULARY = 20
MAX_TAG_CHARS = 40


_NUMBER = re.compile(r"\d[\d.,]*")
_DECIMALS = re.compile(r"[.,]\d{2}$")


def digit_groups(text: str) -> set[str]:
    """The digits of every number in ``text`` (separators dropped); a number ending in two decimals also yields its integer part."""
    groups = set()
    for token in _NUMBER.findall(text or ""):
        token = token.rstrip(".,")
        groups.add(re.sub(r"\D", "", token))
        if _DECIMALS.search(token):
            groups.add(re.sub(r"\D", "", token[:-3]))
    groups.discard("")
    return groups


def numbers_ok(text: str, *sources: str) -> bool:
    """True when every number of four or more digits in ``text`` appears in one of ``sources``: the model may write
    figures the server computed, never invent or calculate its own."""
    allowed: set[str] = set()
    for source in sources:
        allowed |= digit_groups(source)
    return all(len(group) < 4 or group in allowed for group in digit_groups(text))


class BlockedError(Exception):
    """The budget guard refused an automatic call."""


def clean(value, limit: int = 200) -> str | None:
    """Redacted, single-spaced and bounded text: the only form in which free text goes to the provider."""
    return " ".join((redact(value) if isinstance(value, str) else "").split())[:limit] or None


def normalize_tags(values) -> list[str]:
    """Lower-cased, trimmed, unique tags; ``ValueError`` when a tag is empty/too long or the list is empty/too long."""
    tags: list[str] = []
    for value in values or []:
        tag = " ".join(value.split()).lower() if isinstance(value, str) else ""
        if not tag or len(tag) > MAX_TAG_CHARS:
            raise ValueError(f"Cada etiqueta debe tener entre 1 y {MAX_TAG_CHARS} caracteres.")
        if tag not in tags:
            tags.append(tag)
    if not 1 <= len(tags) <= MAX_VOCABULARY:
        raise ValueError(f"El vocabulario admite entre 1 y {MAX_VOCABULARY} etiquetas.")
    return tags


async def _flag(session, key: str) -> dict | None:
    return await session.scalar(select(RuntimeFlag.value).where(RuntimeFlag.key == key))


async def _write(session, key: str, value: dict) -> None:
    row = await session.get(RuntimeFlag, key, populate_existing=True)
    if row is None:
        session.add(RuntimeFlag(key=key, value=value))
    else:
        row.value = value
    await session.flush()


async def get(session) -> dict:
    """``{"features": {name: bool}, "confidence_threshold": float, "tags": [str]}`` with defaults for what is not stored."""
    stored = await _flag(session, FEATURES_KEY) or {}
    threshold = stored.get("confidence_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, int | float) or not MIN_THRESHOLD <= threshold <= 1:
        threshold = DEFAULT_THRESHOLD
    try:
        tags = normalize_tags((await _flag(session, TAGS_KEY) or {}).get("tags"))
    except ValueError:
        tags = list(DEFAULT_TAGS)
    return {
        "features": {name: stored.get(name) is not False for name in FEATURES},
        "confidence_threshold": float(threshold), "tags": tags,
    }


async def enabled(session, name: str) -> bool:
    return (await get(session))["features"][name]


async def update(session, *, features: dict | None = None, confidence_threshold: float | None = None, tags=None) -> dict:
    """Merge the given changes into the stored state and return the new one (the caller validates names, audits and commits)."""
    current = await get(session)
    merged = {**current["features"], **(features or {})}
    threshold = current["confidence_threshold"] if confidence_threshold is None else float(confidence_threshold)
    await _write(session, FEATURES_KEY, {**merged, "confidence_threshold": threshold})
    if tags is not None:
        await _write(session, TAGS_KEY, {"tags": normalize_tags(tags)})
    return await get(session)


async def call(session, llm, kind: str, prompt: str, content: dict, *, auto: bool) -> tuple[dict, dict]:
    """One JSON answer from the model, counted in ``ai_budget``. ``(data, usage)``.

    Raises ``BlockedError`` when an automatic job is refused by the guard and ``RuntimeError`` when the model chain fails.
    The caller validates ``data``; ``content`` must already be redacted.
    """
    if auto and not await ai_budget.allow(session, kind, 1):
        raise BlockedError(kind)
    usage: dict = {}
    try:
        data = await llm.complete_json(prompt, content, uuid4().hex, usage=usage)
    finally:
        try:
            await ai_budget.record(session, kind, usage.get("calls", 1), usage, auto=auto)
        except Exception as exc:  # accounting must never fail the work
            logger.warning("ai_insights_budget_record_failed", kind=kind, error_type=type(exc).__name__)
    return data, usage
