"""Own accounts managed from the app (contract section 7): which Firefly account each bank maps to and which sources feed it.

The table is the runtime source for the ingest pipeline. Every write is audited, committed and only then
invalidates the process snapshot (``app.services.own_accounts``), so the next ingest already uses it.
The analyzer never creates asset accounts: the app creates them in Firefly with its own token first.
"""
from typing import Annotated

from fastapi import APIRouter, HTTPException

from app.api.dependencies import READ, REVIEW, Principal, get_firefly
from app.api.routers.common import DB
from app.core.logging import get_logger
from app.db.repositories import OwnAccountExistsError, OwnAccountRepository
from app.models.schemas import OwnAccountIn, OwnAccountList, OwnAccountOut, OwnAccountPatch
from app.services import audit
from app.services import own_accounts as registry

logger = get_logger(__name__)
router = APIRouter()

_LEDGER_TYPES = {"asset", "liability", "liabilities"}


def _error(status: int, detail: str, code: str) -> HTTPException:
    return HTTPException(status, detail={"detail": detail, "code": code})


def _not_found() -> HTTPException:
    return _error(404, "Cuenta propia no encontrada.", "own_account_not_found")


def _exists() -> HTTPException:
    return _error(409, "Ya existe una cuenta propia con ese nombre de Firefly.", "own_account_exists")


def _sources(values: list[str]) -> list[str]:
    try:
        return registry.normalize_sources(values)
    except registry.InvalidSourceError as exc:
        raise _error(422, f"La fuente «{exc}» no es válida: usa app:<paquete>, sms:<remitente> o email:<dirección o dominio>.", "invalid_source") from exc


async def _firefly_names() -> set[str] | None:
    """Lower-cased names of Firefly's asset and liability accounts, or None when Firefly does not answer."""
    try:
        rows = await get_firefly().accounts()
        return {row["name"].casefold() for row in rows if row["type"] in _LEDGER_TYPES}
    except Exception as exc:  # optional enrichment: an outage must not block managing accounts
        logger.warning("own_accounts_firefly_unavailable", error_type=type(exc).__name__)
        return None


def _require_in_firefly(name: str, names: set[str] | None) -> None:
    if names is not None and name.casefold() not in names:
        raise _error(422, "Firefly no tiene una cuenta de activo o pasivo con ese nombre; créala primero en Firefly.", "firefly_account_missing")


def _out(row, names: set[str] | None) -> OwnAccountOut:
    return OwnAccountOut(
        id=row.id, firefly_account=row.firefly_account, bank=row.bank, kind=row.kind, last4=list(row.last4 or []),
        aliases=list(row.aliases or []), sources=list(row.sources or []),
        firefly_exists=None if names is None else row.firefly_account.casefold() in names, updated_at=row.updated_at,
    )


async def _commit(db) -> None:
    await db.commit()
    registry.invalidate()


@router.get("/own-accounts", dependencies=[READ], response_model=OwnAccountList)
async def list_own_accounts(db: DB):
    rows = await OwnAccountRepository(db).all()
    names = await _firefly_names() if rows else None
    return {"items": [_out(row, names) for row in rows]}


@router.post("/own-accounts", status_code=201, response_model=OwnAccountOut)
async def add_own_account(payload: OwnAccountIn, db: DB, principal: Annotated[Principal, REVIEW]):
    repository = OwnAccountRepository(db)
    sources = _sources(payload.sources)
    if await repository.taken(payload.firefly_account):
        raise _exists()
    names = await _firefly_names()
    _require_in_firefly(payload.firefly_account, names)
    try:
        row = await repository.add(
            firefly_account=payload.firefly_account, bank=payload.bank, kind=payload.kind,
            last4=payload.last4, aliases=payload.aliases, sources=sources,
        )
    except OwnAccountExistsError as exc:
        raise _exists() from exc
    await audit.record(db, action="own_account.add", target_type="own_account", target_id=row.id,
                       detail={"firefly_account": row.firefly_account, "sources": len(row.sources)}, principal=principal)
    await _commit(db)
    return _out(row, names)


@router.patch("/own-accounts/{account_id}", response_model=OwnAccountOut)
async def update_own_account(account_id: int, payload: OwnAccountPatch, db: DB, principal: Annotated[Principal, REVIEW]):
    repository = OwnAccountRepository(db)
    row = await repository.get(account_id)
    if row is None:
        raise _not_found()
    fields = payload.model_dump(exclude_unset=True)
    if "sources" in fields:
        fields["sources"] = _sources(fields["sources"])
    renamed = "firefly_account" in fields and fields["firefly_account"].casefold() != row.firefly_account.casefold()
    if "firefly_account" in fields and await repository.taken(fields["firefly_account"], exclude_id=row.id):
        raise _exists()
    names = await _firefly_names()
    if renamed:
        _require_in_firefly(fields["firefly_account"], names)
    previous = row.firefly_account
    try:
        await repository.update(row, **fields)
    except OwnAccountExistsError as exc:
        raise _exists() from exc
    detail = {"firefly_account": row.firefly_account, "changed": sorted(fields), "sources": len(row.sources)}
    if previous != row.firefly_account:
        detail["previous_account"] = previous
    await audit.record(db, action="own_account.update", target_type="own_account", target_id=row.id, detail=detail, principal=principal)
    await _commit(db)
    return _out(row, names)


@router.delete("/own-accounts/{account_id}")
async def delete_own_account(account_id: int, db: DB, principal: Annotated[Principal, REVIEW]):
    repository = OwnAccountRepository(db)
    row = await repository.get(account_id)
    if row is None:
        raise _not_found()
    await audit.record(db, action="own_account.delete", target_type="own_account", target_id=row.id,
                       detail={"firefly_account": row.firefly_account, "sources": len(row.sources or [])}, principal=principal)
    await repository.delete(row)
    await _commit(db)
    return {"deleted": True}
