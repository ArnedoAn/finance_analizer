"""Descriptions derived from the extraction, generic e-mail subjects, merchant tails and the fix of created movements."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.routers import maintenance
from app.db.models import InboxItem
from app.db.models_assistant import Audit
from app.db.models_ledger import LedgerChange
from app.models.schemas import ManualTransaction
from app.parsers import Extraction, normalize_merchant, parse
from app.services import descriptions
from app.services import processor as pipeline
from tests.test_devices_v4 import auth, enroll_key

API = "/api/v1"
ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
FIX = f"{API}/maintenance/descriptions/fix"
GENERIC = "Alertas y Notificaciones"
BODY = "Bancolombia le informa Compra por $45.000,00 en EXITO CALLE 80 23:10. 22/11/2026 T.Cred *1234"


@pytest.fixture(autouse=True)
def fresh_cache():
    descriptions.invalidate()
    yield
    descriptions.invalidate()


def fields(**changes):
    values = dict(
        merchant="EXITO", transaction_type="withdrawal", tags=[], destination_account=None, asset_account="Nequi",
        description=None, subject_or_title="", sender="alertas@nequi.com.co",
    )
    values.update(changes)
    return SimpleNamespace(**values)


def extraction(kind="withdrawal", template="bancolombia.compraste", **changes):
    return Extraction(kind=kind, amount=Decimal("45000.00"), merchant="EXITO", template_id=template, **changes)


# ---- 1. description by type ------------------------------------------------------------------------------


@pytest.mark.parametrize(("item", "ext", "text", "expected"), [
    (fields(), extraction(), "", "Compra en EXITO"),
    (fields(merchant="RAPPI"), extraction(template="nequi.pagaste"), "", "Pago a RAPPI"),
    (fields(merchant="RAPPI"), extraction(template="bancolombia.pagaste"), "Pago por PSE a RAPPI", "Pago PSE a RAPPI"),
    (fields(merchant="RAPPI"), extraction(), "Compra PSE", "Pago PSE a RAPPI"),
    (fields(merchant="CAJERO 12"), extraction(template="bancolombia.retiro"), "", "Retiro en CAJERO 12"),
    (fields(merchant="CAJERO 12"), extraction(template="llm:x"), "Retiraste $100.000 en CAJERO 12", "Retiro en CAJERO 12"),
    (fields(merchant="MARIA PEREZ"), extraction(template="nequi.enviaste"), "", "Transferencia a MARIA PEREZ"),
    (fields(merchant="MARIA PEREZ", tags=["transferencia"]), extraction(kind="transfer", template="llm:x"), "", "Transferencia a MARIA PEREZ"),
    (fields(merchant="ANA RUIZ", transaction_type="deposit", tags=["transferencia"]), extraction(kind="transfer", template="llm:x"), "", "Recibiste de ANA RUIZ"),
    (fields(merchant="ANA RUIZ", transaction_type="deposit"), extraction(kind="deposit", template="nequi.recibiste"), "", "Recibiste de ANA RUIZ"),
    (fields(merchant="TIENDA", transaction_type="refund"), extraction(kind="refund", template="bancolombia.refund"), "", "Devolución de TIENDA"),
    (fields(transaction_type="transfer", destination_account="Bancolombia"), extraction(template="nequi.enviaste"), "", "Transferencia a Bancolombia"),
    (fields(transaction_type="transfer", asset_account="Bancolombia"), extraction(kind="deposit", template="nequi.recibiste"), "", "Recibiste de Bancolombia"),
    (fields(merchant=None), extraction(), "", None),
])
def test_description_by_type(item, ext, text, expected):
    assert descriptions.derive(item, ext, text) == expected


def test_llm_summary_wins_and_loses_its_amount():
    ext = extraction(summary="Compra en EXITO por $45.000,00 COP el 22/11/2026")
    assert descriptions.derive(fields(), ext, "") == "Compra en EXITO el 22/11/2026"
    assert descriptions.clean_summary("Pago PSE a RAPPI de $20.000") == "Pago PSE a RAPPI"
    assert descriptions.clean_summary("compra de $5.000 en TIENDA") == "Compra en TIENDA"
    assert descriptions.clean_summary("$5.000") == "$5.000"  # nothing else to say: the sentence is kept
    assert descriptions.clean_summary("  ") is None and descriptions.clean_summary(None) is None
    assert "1234567" not in descriptions.clean_summary("Compra en EXITO tarjeta 1234567890123456")


def test_title_prefers_description_then_a_specific_subject_then_the_merchant():
    assert descriptions.title(fields(description="Mi nota", subject_or_title="Cobro de Spotify")) == "Mi nota"
    assert descriptions.title(fields(subject_or_title="Cobro de Spotify")) == "Cobro de Spotify"
    assert descriptions.title(fields(subject_or_title=GENERIC)) == "EXITO"
    assert descriptions.title(fields(subject_or_title="")) == "EXITO"


# ---- 2. generic subjects ---------------------------------------------------------------------------------


@pytest.mark.parametrize("subject", [
    "Alertas y Notificaciones", "  ALERTAS   Y NOTIFICACIONES. ", "Re: Bancolombia te informa:", "Notificación de transacción",
    "Nequi te cuenta", "Transacción exitosa", "", None,
])
def test_known_generic_subjects(subject):
    assert descriptions.is_generic_subject("alertas@bancolombia.com.co", subject) is True


def test_specific_subjects_and_sms_titles():
    assert descriptions.is_generic_subject("alertas@bancolombia.com.co", "Cobro de Spotify noviembre") is False
    assert descriptions.is_generic_subject("85954", "85954") is True  # an SMS title is its sender
    assert descriptions.is_generic_subject("85954", "Recibiste dinero") is False


async def test_a_subject_repeated_five_times_by_one_sender_is_generic(db):
    for _ in range(5):
        db.add(InboxItem(source="email", source_ref=str(uuid4()), sender="avisos@tienda.co", subject_or_title="Tu pedido llegó"))
    for _ in range(4):
        db.add(InboxItem(source="email", source_ref=str(uuid4()), sender="avisos@tienda.co", subject_or_title="Cuatro veces"))
    for _ in range(5):
        db.add(InboxItem(source="manual", source_ref=str(uuid4()), sender="owner", subject_or_title="Almuerzo"))
    for _ in range(5):
        db.add(InboxItem(source="email", source_ref=str(uuid4()), sender="otro@tienda.co", subject_or_title="Otro remitente"))
    db.add(InboxItem(source="email", source_ref=str(uuid4()), sender="avisos@tienda.co", subject_or_title="Otro remitente"))
    await db.commit()
    assert descriptions.is_generic_subject("avisos@tienda.co", "Tu pedido llegó") is False  # not loaded yet
    await descriptions.refresh(db)
    assert descriptions.is_generic_subject("avisos@tienda.co", "Tu pedido llegó") is True
    assert descriptions.is_generic_subject("AVISOS@tienda.co", "tu pedido llegó!") is True
    assert descriptions.is_generic_subject("otro@tienda.co", "Tu pedido llegó") is False
    assert descriptions.is_generic_subject("avisos@tienda.co", "Cuatro veces") is False
    assert descriptions.is_generic_subject("owner", "Almuerzo") is False  # manual entries never count
    assert descriptions.is_generic_subject("avisos@tienda.co", "Otro remitente") is False  # repeats are per sender


async def test_the_repetition_set_is_cached_per_process(db):
    for _ in range(5):
        db.add(InboxItem(source="email", source_ref=str(uuid4()), sender="a@x.co", subject_or_title="Repetido"))
    await db.commit()
    await descriptions.refresh(db)
    for _ in range(5):
        db.add(InboxItem(source="email", source_ref=str(uuid4()), sender="a@x.co", subject_or_title="Nuevo repetido"))
    await db.commit()
    await descriptions.refresh(db)
    assert descriptions.is_generic_subject("a@x.co", "Nuevo repetido") is False
    await descriptions.refresh(db, force=True)
    assert descriptions.is_generic_subject("a@x.co", "Nuevo repetido") is True


# ---- 3. what the LLM receives ---------------------------------------------------------------------------


async def email(service, subject, text="Pagaste $12.000 en SUPERMERCADO CENTRAL", **changes):
    values = dict(
        source="email", source_ref=str(uuid4()), sender="alertas@nequi.com.co", raw_text=text, subject_or_title=subject,
        occurred_at=datetime.now(UTC), status="pending",
    )
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


@pytest.fixture
def llm_only(monkeypatch, clients, extraction):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: None)
    clients[1].analyze.return_value = replace(extraction, template_id="llm:test", summary="Compra en SUPERMERCADO CENTRAL por $12.000")
    return clients[1]


async def test_the_llm_does_not_receive_a_generic_subject(service, llm_only, clients):
    row = await service.process(await email(service, GENERIC))
    sent = llm_only.analyze.call_args.args[0]
    assert "Alertas" not in sent and "Pagaste $12.000" in sent
    assert row.status == "created" and row.description == "Compra en SUPERMERCADO CENTRAL"
    assert clients[0].create_transaction.call_args.args[0]["transactions"][0]["description"] == "Compra en SUPERMERCADO CENTRAL"


async def test_the_llm_still_receives_a_specific_subject(service, llm_only):
    row = await service.process(await email(service, "Cobro de Spotify"))
    assert "Cobro de Spotify" in llm_only.analyze.call_args.args[0]
    assert row.description == "Compra en SUPERMERCADO CENTRAL"  # the derived description comes before the subject


async def test_the_extraction_prompt_asks_for_a_summary_that_starts_with_the_type():
    from app.clients.llm import _PROMPT

    assert "EMPIEZA por el tipo de movimiento" in _PROMPT and "«Pago PSE a COMERCIO»" in _PROMPT


# ---- derived description in the pipeline -----------------------------------------------------------------


async def test_parser_items_get_a_template_description_and_a_clean_merchant(service, add_own_account, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", parse)
    await add_own_account("Bancolombia", bank="bancolombia", kind="credit", last4=["1234"], sources=["email:notificacionesbancolombia.com"])
    row = await service.process(await email(service, GENERIC, BODY, sender="alertasynotificaciones@an.notificacionesbancolombia.com"))
    assert row.merchant == "EXITO CALLE 80" and row.amount == Decimal("45000.00")
    assert row.description == "Compra en EXITO CALLE 80"


async def test_the_owners_description_is_kept(service, clients):
    row = await service.process(await email(service, GENERIC, description="Mercado de la semana"))
    assert row.status == "created" and row.description == "Mercado de la semana"
    assert clients[0].create_transaction.call_args.args[0]["transactions"][0]["description"] == "Mercado de la semana"


async def test_manual_items_do_not_get_a_derived_description(service):
    row = await service.manual(ManualTransaction(
        client_ref="m-1", amount=7000, description="Almuerzo", merchant="SUPERMERCADO CENTRAL", account="Nequi",
    ))
    assert row.status == "created" and row.description is None


async def test_correcting_the_merchant_updates_the_derived_description(service, clients):
    row = await service.process(await email(service, GENERIC))
    assert row.status == "created" and row.description == "Pago a SUPERMERCADO CENTRAL"
    edited = await service.review(row, "edit", {"merchant": "EXITO"})
    assert edited.description == "Pago a EXITO"
    assert clients[0].update_transaction.call_args.args[1]["transactions"][0]["description"] == "Pago a EXITO"
    kept = await service.review(edited, "edit", {"merchant": "ALKOSTO", "description": "A mano"})
    assert kept.description == "A mano"


async def test_inbox_title_shows_the_description_when_the_subject_is_generic(client, service):
    row = await service.process(await email(service, GENERIC))
    shown = (await client.get(f"{API}/inbox/{row.id}", headers=ADMIN)).json()
    assert shown["subject_or_title"] == row.description == "Pago a SUPERMERCADO CENTRAL"
    specific = await service.process(await email(service, "Cobro de Spotify", "Pagaste $13.000 en SUPERMERCADO CENTRAL"))
    assert (await client.get(f"{API}/inbox/{specific.id}", headers=ADMIN)).json()["subject_or_title"] == "Cobro de Spotify"


# ---- 4. merchant without the time -------------------------------------------------------------------------


def test_parser_cuts_the_merchant_before_the_time_and_the_date():
    result = parse(BODY, GENERIC, "87400")
    assert result.merchant == "EXITO CALLE 80" and result.amount == Decimal("45000.00")
    assert parse("Bancolombia le informa Compra por $9.000,00 en TIENDA UNO 22/11/2026 08:05", "", "87400").merchant == "TIENDA UNO"
    assert parse("Bancolombia le informa Compra por $9.000,00 en CAFE 7 11:30 AM", "", "87400").merchant == "CAFE 7"


@pytest.mark.parametrize(("raw", "expected"), [
    ("EXITO CALLE 80 23:10", "EXITO CALLE 80"), ("EXITO 22/11/2026", "EXITO"), ("TIENDA 24 HORAS", "TIENDA 24 HORAS"),
    ("LOCAL 80", "LOCAL 80"), ("FARMACIA 5/6", "FARMACIA 5/6"),
])
def test_normalize_merchant_tails(raw, expected):
    assert normalize_merchant(raw) == expected


# ---- 5. fixing what was already created -------------------------------------------------------------------


def firefly_group(firefly_id, description, ref):
    return {"id": firefly_id, "attributes": {"group_title": None, "transactions": [{
        "transaction_journal_id": firefly_id, "type": "withdrawal", "date": "2026-11-20T12:00:00-05:00", "amount": "12000.00",
        "currency_code": "COP", "source_id": "1", "source_name": "Nequi", "destination_id": "2", "destination_name": "SUPERMERCADO CENTRAL",
        "description": description, "external_id": f"fa:{ref}", "tags": ["auto", "email"],
    }]}}


def created(db, ref, firefly_id, **changes):
    values = dict(
        source="email", source_ref=ref, sender="alertas@nequi.com.co", subject_or_title=GENERIC, raw_text="Pagaste $12.000", status="created",
        method="parser", template_id="bancolombia.compraste", original_kind="withdrawal", transaction_type="withdrawal", amount=Decimal("12000.00"),
        currency="COP", merchant="SUPERMERCADO CENTRAL", asset_account="Nequi", firefly_transaction_id=firefly_id, occurred_at=datetime.now(UTC),
    )
    values.update(changes)
    row = InboxItem(**values)
    db.add(row)
    return row


@pytest.fixture
def ledger_fake(clients):
    """Firefly with state: ``update_transaction`` changes the stored description, ``get_transaction`` reads it."""
    firefly = clients[0]
    store = {}

    async def get(firefly_id):
        return store.get(str(firefly_id))

    async def update(firefly_id, payload):
        store[str(firefly_id)]["attributes"]["transactions"][0]["description"] = payload["transactions"][0]["description"]
        return str(firefly_id)

    firefly.get_transaction.side_effect = get
    firefly.update_transaction.side_effect = update
    return store


@pytest.fixture
async def seeded(db, ledger_fake):
    created(db, "ref-a", "10")
    created(db, "ref-b", "11", template_id="bancolombia.pagaste")
    created(db, "ref-c", "12", subject_or_title="Cobro especial")  # specific subject
    created(db, "ref-d", "13", description="Ya tiene")  # already described
    created(db, "ref-e", "14", method="llm", template_id="llm:x", raw_text="Compra en SUPERMERCADO CENTRAL por $12.000")
    created(db, "ref-f", "15", source="sms", subject_or_title="85954", sender="85954")  # not an e-mail
    created(db, "ref-g", "16", merchant=None)  # nothing better to say
    await db.commit()
    for ref, firefly_id, text in (("ref-a", "10", GENERIC), ("ref-b", "11", "Mi descripción"), ("ref-e", "14", GENERIC)):
        ledger_fake[firefly_id] = firefly_group(firefly_id, text, ref)


@pytest.fixture
def inline_fix(monkeypatch, db, service):
    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(maintenance, "get_db_session", session)
    monkeypatch.setattr(maintenance, "get_firefly", lambda: service.firefly)
    monkeypatch.setattr(maintenance, "get_llm", lambda: service.llm)
    monkeypatch.setattr(maintenance, "_fix_task", None)


async def test_dry_run_counts_and_shows_examples_without_changing_anything(client, seeded, ledger_fake, clients):
    response = await client.post(FIX, headers=ADMIN, params={"dry_run": "true"})
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True and body["count"] == 3 and len(body["examples"]) == 3
    by_id = {example["id"]: example for example in body["examples"]}
    assert sorted(example["after"] for example in by_id.values()) == [
        "Compra en SUPERMERCADO CENTRAL", "Compra en SUPERMERCADO CENTRAL", "Pago a SUPERMERCADO CENTRAL",
    ]
    assert all(example["before"] == GENERIC and set(example) == {"id", "before", "after"} for example in by_id.values())
    clients[0].update_transaction.assert_not_awaited()
    assert ledger_fake["10"]["attributes"]["transactions"][0]["description"] == GENERIC


async def test_dry_run_shows_at_most_ten_examples(client, db, ledger_fake):
    for number in range(12):
        created(db, f"many-{number}", str(100 + number))
    await db.commit()
    body = (await client.post(FIX, headers=ADMIN, params={"dry_run": "true"})).json()
    assert body["count"] == 12 and len(body["examples"]) == 10


async def test_fix_retitles_in_firefly_through_the_audited_edit_path(client, seeded, ledger_fake, clients, db, inline_fix):
    response = await client.post(FIX, headers=ADMIN)
    assert response.status_code == 202
    body = response.json()
    assert body["queued"] == 3 and len(body["group_id"]) == 32
    await maintenance._fix_task
    description = {key: value["attributes"]["transactions"][0]["description"] for key, value in ledger_fake.items()}
    assert description["10"] == "Compra en SUPERMERCADO CENTRAL" and description["14"] == "Compra en SUPERMERCADO CENTRAL"
    assert description["11"] == "Mi descripción"  # the owner changed it in Firefly: left alone
    changes = (await db.scalars(select(LedgerChange).order_by(LedgerChange.id))).all()
    assert len(changes) == 2 and {change.group_id for change in changes} == {body["group_id"]}
    assert all(change.action == "update" and change.before_version["description"] == GENERIC for change in changes)
    items = {row.source_ref: row for row in (await db.scalars(select(InboxItem))).all()}
    assert items["ref-a"].description == "Compra en SUPERMERCADO CENTRAL" and items["ref-b"].description is None
    audits = (await db.scalars(select(Audit).where(Audit.action == "maintenance.descriptions_fix"))).all()
    assert len(audits) == 1 and audits[0].detail["queued"] == 3
    assert (await client.post(FIX, headers=ADMIN, params={"dry_run": "true"})).json()["count"] == 1  # only the skipped one remains


async def test_the_fix_can_be_undone_as_a_group(client, seeded, ledger_fake, db, inline_fix):
    group = (await client.post(FIX, headers=ADMIN)).json()["group_id"]
    await maintenance._fix_task
    undone = await client.post(f"{API}/ledger/groups/{group}/undo", headers=ADMIN)
    assert undone.status_code == 200 and len(undone.json()["undone"]) == 2
    assert ledger_fake["10"]["attributes"]["transactions"][0]["description"] == GENERIC


async def test_nothing_to_fix_queues_nothing(client, db, ledger_fake):
    assert (await client.post(FIX, headers=ADMIN)).json() == {"queued": 0, "group_id": None}
    assert (await client.post(FIX, headers=ADMIN, params={"dry_run": "true"})).json() == {"dry_run": True, "count": 0, "examples": []}


async def test_only_one_fix_runs_at_a_time(client, seeded, monkeypatch):
    monkeypatch.setattr(maintenance, "_fix_task", asyncio.get_running_loop().create_future())
    busy = await client.post(FIX, headers=ADMIN)
    assert busy.status_code == 409 and busy.json()["code"] == "descriptions_fix_running"
    assert (await client.post(FIX, headers=ADMIN, params={"dry_run": "true"})).status_code == 200


async def test_the_fix_needs_review_scope(client, seeded):
    reader = (await enroll_key(client, ["read"]))["key"]
    assert (await client.post(FIX, headers=auth(reader))).status_code == 403
    assert (await client.post(FIX, headers=auth(reader), params={"dry_run": "true"})).status_code == 403
    reviewer = (await enroll_key(client, ["review"]))["key"]
    assert (await client.post(FIX, headers=auth(reviewer), params={"dry_run": "true"})).status_code == 200
