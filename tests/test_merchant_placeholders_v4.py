"""Placeholder merchants («PENDING») never become a Firefly account, and a counterparty the bank masks («PAU*** BET***»)
reuses the one already known: both decided in ``Processor`` before anything is written to Firefly."""
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models import AiSuggestion, InboxEvent
from app.db.models_rules import Merchant
from app.parsers import (
    is_masked_counterparty,
    is_placeholder_merchant,
    match_masked_counterparty,
    normalize_merchant,
    parse,
)
from app.services import ai_review
from app.services import processor as pipeline

API = "/api/v1"
ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
BANCOLOMBIA_SENDER = "alertasynotificaciones@an.notificacionesbancolombia.com"
GENERIC = "Alertas y Notificaciones"
MASKED = "PAU*** BET***"


async def item(service, **changes):
    values = dict(
        source="email", source_ref=str(uuid4()), sender="alertas@nequi.com.co", raw_text="Pagaste $12.000 en TIENDA",
        subject_or_title="Compra", occurred_at=datetime.now(UTC), status="pending",
    )
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


def known(clients, *names, kind="expense"):
    firefly, _ = clients
    firefly.accounts.return_value = [*firefly.accounts.return_value, *({"id": str(100 + n), "name": name, "type": kind} for n, name in enumerate(names))]


def nothing_created(clients):
    firefly, _ = clients
    firefly.create_account.assert_not_awaited()
    firefly.create_transaction.assert_not_awaited()


async def events(db, row, action):
    return list((await db.scalars(select(InboxEvent).where(InboxEvent.inbox_id == row.id, InboxEvent.action == action))).all())


# --- the rule itself ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "PENDING", "pending", "Pendiente", "N/A", "NA", "n/a", "NULL", "None", "UNKNOWN", "Desconocido", "SIN COMERCIO", "sin-comercio",
    "COMERCIO", "-", "", "   ", None, "***", "*", "- * -", "PENDING.", "PENDING con tu tarjeta 1234",
])
def test_placeholders_are_recognised(name):
    assert is_placeholder_merchant(name)


@pytest.mark.parametrize("name", ["UBER", "NETFLIX", "UBER *TRIP", "PAYU*NETFLIX", "PENDING PIZZA", "COMERCIO DESCONOCIDO", "PAU*** BET***", "NATALIA"])
def test_real_names_are_not_placeholders(name):
    assert not is_placeholder_merchant(name)


def test_bancolombia_template_keeps_pending_literally_because_the_mail_says_it():
    extraction = parse("Compraste $14.981 en PENDING con tu T.Cred *1234, el 18/11/2026.", GENERIC, BANCOLOMBIA_SENDER)
    assert extraction.template_id == "bancolombia.compraste" and extraction.amount == Decimal("14981.00")
    assert extraction.merchant == "PENDING" and is_placeholder_merchant(extraction.merchant)


@pytest.mark.parametrize("name", [MASKED, "PAU*** BET", "pau** bet**", "Mar*** de la Cru***", "J*** PEREZ"])
def test_masks_are_prefix_plus_asterisks_per_person_token(name):
    assert is_masked_counterparty(name)


@pytest.mark.parametrize("name", ["UBER *TRIP", "PAYU*NETFLIX", "UBR*CAFE", "PAULA BETANCUR", "TIENDA 24*** ", "*** BET", "PAU* BET*", "", None])
def test_legitimate_asterisks_are_not_masks(name):
    assert not is_masked_counterparty(name)


def test_matching_is_token_by_token_by_prefix_without_accents_or_case():
    known_names = ["Paula Betancur", "PAULO BETANCOURT", "PAULA MARIA BETANCUR", "PAU*** BET***", "JOSÉ PÉREZ", ""]
    assert match_masked_counterparty("PAU*** BET***", known_names[:1]) == ["PAULA BETANCUR"]
    assert match_masked_counterparty("PAU*** BET", known_names) == ["PAULA BETANCUR", "PAULO BETANCOURT"]
    assert match_masked_counterparty("JOS*** PER***", known_names) == ["JOSÉ PÉREZ"]
    assert match_masked_counterparty("PAU*** BET***", ["paula betancur", "PAULA  BETANCUR"]) == ["PAULA BETANCUR"]


def test_normalisation_keeps_the_closing_mask_but_not_stray_asterisks():
    assert normalize_merchant(MASKED) == MASKED
    assert normalize_merchant("TIENDA *") == "TIENDA" and normalize_merchant("** TIENDA -") == "TIENDA"
    assert normalize_merchant("UBER *TRIP") == "UBER *TRIP"
    assert normalize_merchant(normalize_merchant(MASKED)) == MASKED


# --- «PENDING» never reaches Firefly -----------------------------------------------------------------------------------


async def test_pending_from_the_parser_waits_for_a_name_and_then_registers(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: replace(extraction, merchant="PENDING", amount=Decimal("14981.00"), template_id="bancolombia.compraste"))
    row = await service.process(await item(service))
    assert row.status == "needs_review" and row.review_reason == "merchant_unknown" and row.merchant is None
    nothing_created(clients)

    row = await service.review(row, "approve", {"merchant": "UBER"})
    assert row.status == "created" and row.merchant == "UBER"
    clients[0].create_account.assert_awaited_once_with("UBER", "expense")
    clients[0].create_transaction.assert_awaited_once()


async def test_pending_from_the_real_bancolombia_parser_never_creates_the_account(service, clients, add_own_account, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", parse)
    await add_own_account("Bancolombia", bank="bancolombia", kind="credit", last4=["1234"], sources=["email:notificacionesbancolombia.com"])
    row = await service.process(await item(
        service, sender=BANCOLOMBIA_SENDER, subject_or_title=GENERIC, raw_text="Compraste $13.976 en PENDING con tu T.Cred *1234.",
    ))
    assert (row.status, row.review_reason, row.merchant, row.amount) == ("needs_review", "merchant_unknown", None, Decimal("13976.00"))
    nothing_created(clients)


async def test_pending_from_the_llm_waits_for_a_name_and_then_registers(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: None)
    clients[1].analyze.return_value = replace(extraction, merchant="PENDING", template_id="llm:test")
    row = await service.process(await item(service, raw_text="Compra por $12.000 en PENDING con tu cuenta Nequi"))
    assert row.method == "llm" and row.status == "needs_review" and row.review_reason == "merchant_unknown" and row.merchant is None
    nothing_created(clients)

    row = await service.review(row, "approve", {"merchant": "Tienda Uno"})
    assert row.status == "created" and row.merchant == "TIENDA UNO"
    clients[0].create_account.assert_awaited_once_with("TIENDA UNO", "expense")


@pytest.mark.parametrize("placeholder", ["N/A", "sin comercio", "Pendiente", "-"])
async def test_a_placeholder_extraction_of_any_spelling_is_unknown(service, clients, extraction, monkeypatch, placeholder):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: replace(extraction, merchant=placeholder))
    row = await service.process(await item(service))
    assert row.status == "needs_review" and row.review_reason == "merchant_unknown"
    nothing_created(clients)


async def test_a_placeholder_counterparty_of_a_transfer_is_unknown_too(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: replace(extraction, kind="transfer", merchant="PENDING", counterparty_hint="PENDING"))
    row = await service.process(await item(service))
    assert row.status == "needs_review" and row.review_reason == "merchant_unknown"
    nothing_created(clients)


async def test_an_absent_merchant_keeps_its_own_reason(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: replace(extraction, merchant=None))
    row = await service.process(await item(service))
    assert row.status == "needs_review" and row.review_reason == "merchant absent"
    nothing_created(clients)


async def test_an_old_row_stored_with_a_placeholder_cannot_be_approved_without_a_name(service, clients):
    row = await item(service, status="needs_review", method="parser", amount=Decimal("12000.00"), currency="COP", merchant="PENDING",
                     asset_account="Nequi", transaction_type="withdrawal", original_kind="withdrawal")
    with pytest.raises(ValueError, match="Review requires"):
        await service.review(row, "approve", {})
    nothing_created(clients)
    row = await service.review(row, "approve", {"merchant": "Rappi"})
    assert row.status == "created" and row.merchant == "RAPPI"


async def test_the_final_guard_refuses_a_placeholder_account_even_when_nothing_else_caught_it(service, clients):
    with pytest.raises(pipeline.PermanentError, match="merchant_unknown"):
        await service._merchant("PENDING", "withdrawal")
    clients[0].create_account.assert_not_awaited()


async def test_manual_entry_refuses_a_placeholder_merchant(service, clients):
    from app.models.schemas import ManualTransaction

    with pytest.raises(ValueError, match="Merchant is not clean"):
        await service.manual(ManualTransaction(client_ref="m-pending", amount=7000, description="Algo", merchant="PENDING", account="Nequi"))
    nothing_created(clients)


@pytest.mark.parametrize("merchant", ["PENDING", "n/a", "Sin comercio"])
async def test_a_correction_to_a_placeholder_is_refused_with_422(client, service, clients, merchant):
    row = await item(service, status="needs_review", method="parser", amount=Decimal("12000.00"), currency="COP", merchant=None,
                     asset_account="Nequi", transaction_type="withdrawal", original_kind="withdrawal", review_reason="merchant_unknown")
    for path in ("approve", "classify"):
        response = await client.post(f"{API}/inbox/{row.id}/{path}", headers=ADMIN, json={"merchant": merchant})
        assert response.status_code == 422 and "Merchant is not clean" in response.text
    nothing_created(clients)
    assert (await service.inbox.get(row.id)).status == "needs_review"


async def test_ai_review_never_proposes_a_placeholder_and_cannot_apply_one(service, clients, db):
    firefly, llm = clients
    row = await item(service, status="needs_review", method="parser", amount=Decimal("12000.00"), currency="COP", merchant=None,
                     asset_account="Nequi", transaction_type="withdrawal", original_kind="withdrawal", review_reason="merchant_unknown")
    row_id = row.id
    llm.complete_json.return_value = {
        "action": "approve", "firefly_account": "Nequi", "category": "Transporte", "merchant": "PENDING", "transaction_type": "withdrawal",
        "amount": None, "duplicate_of_id": None, "confidence": 0.99, "reason": "Parece un cobro de transporte",
    }
    assert (await ai_review.suggest_items(db, firefly, llm, [row_id], auto=False))["suggested"] == 1
    suggestion = await db.scalar(select(AiSuggestion))
    assert suggestion.merchant is None and suggestion.confidence <= ai_review.INCOMPLETE_CONFIDENCE
    applied, failed = await ai_review.apply(db, service, None, ids=[suggestion.id], all_above=None)
    assert applied == [] and failed[0]["detail"] == "Review requires amount, merchant and own account"
    nothing_created(clients)


async def test_ai_review_may_name_the_merchant_and_then_the_normal_flow_runs(service, clients, db):
    firefly, llm = clients
    row = await item(service, status="needs_review", method="parser", amount=Decimal("12000.00"), currency="COP", merchant=None,
                     asset_account="Nequi", transaction_type="withdrawal", original_kind="withdrawal", review_reason="merchant_unknown")
    row_id = row.id
    llm.complete_json.return_value = {
        "action": "approve", "firefly_account": "Nequi", "category": "Transporte", "merchant": "Uber", "transaction_type": "withdrawal",
        "amount": None, "duplicate_of_id": None, "confidence": 0.99, "reason": "Cobro de Uber",
    }
    await ai_review.suggest_items(db, firefly, llm, [row_id], auto=False)
    suggestion = await db.scalar(select(AiSuggestion))
    assert suggestion.merchant == "UBER"
    applied, failed = await ai_review.apply(db, service, None, ids=[suggestion.id], all_above=None)
    assert (applied, failed) == ([suggestion.id], [])
    assert (await service.inbox.get(row_id)).status == "created"
    firefly.create_account.assert_awaited_once_with("UBER", "expense")


def test_prompts_say_that_a_placeholder_is_null():
    from app.clients.llm import _PROMPT

    assert "PENDING" in _PROMPT and "null" in _PROMPT.split("PENDING", 1)[1][:80]
    assert "PENDING" in ai_review._PROMPT


# --- a counterparty the bank masked ------------------------------------------------------------------------------------


def masked_transfer(extraction, name=MASKED):
    return lambda *args, **kwargs: replace(
        extraction, kind="transfer", amount=Decimal("30000.00"), merchant=name, counterparty_hint=name, template_id="llm:deepseek-chat",
    )


@pytest.mark.parametrize("name", [MASKED, "PAU*** BET", "pau** bet**"])
async def test_a_masked_counterparty_with_one_known_match_reuses_its_account(service, clients, db, extraction, monkeypatch, name):
    monkeypatch.setattr(pipeline, "parse", masked_transfer(extraction, name))
    known(clients, "PAULA BETANCUR")
    row = await service.process(await item(service))
    assert row.status == "created" and row.merchant == "PAULA BETANCUR" and row.transaction_type == "withdrawal"
    clients[0].create_account.assert_not_awaited()
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert split["destination_id"] == "100"
    matched = await events(db, row, "masked_counterparty_matched")
    assert len(matched) == 1 and "PAULA BETANCUR" in matched[0].detail and "PAU***" in matched[0].detail


async def test_a_masked_counterparty_matches_a_revenue_account_by_name(service, clients, db, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", masked_transfer(extraction))
    known(clients, "PAULA BETANCUR", kind="revenue")
    row = await service.process(await item(service))
    assert row.status == "created" and row.merchant == "PAULA BETANCUR"
    # the name is reused; Firefly still needs an expense account for a withdrawal
    clients[0].create_account.assert_awaited_once_with("PAULA BETANCUR", "expense")
    assert len(await events(db, row, "masked_counterparty_matched")) == 1


async def test_a_masked_counterparty_matches_the_merchants_table(service, clients, db, extraction, monkeypatch):
    db.add(Merchant(name="MARTHA LUCIA RIOS", count=1))
    await db.commit()
    monkeypatch.setattr(pipeline, "parse", masked_transfer(extraction, "MAR*** LUC*** RI***"))
    row = await service.process(await item(service))
    assert row.status == "created" and row.merchant == "MARTHA LUCIA RIOS"
    clients[0].create_account.assert_awaited_once_with("MARTHA LUCIA RIOS", "expense")
    assert len(await events(db, row, "masked_counterparty_matched")) == 1


async def test_a_masked_merchant_of_a_plain_withdrawal_is_resolved_too(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: replace(extraction, merchant=MASKED))
    known(clients, "PAULA BETANCUR")
    row = await service.process(await item(service))
    assert row.status == "created" and row.merchant == "PAULA BETANCUR"
    clients[0].create_account.assert_not_awaited()


async def test_a_masked_counterparty_with_two_candidates_goes_to_review(service, clients, db, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", masked_transfer(extraction))
    known(clients, "PAULA BETANCUR", "PAULO BETANCOURT")
    row = await service.process(await item(service))
    assert row.status == "needs_review" and row.review_reason == "counterparty_ambiguous" and row.merchant == MASKED
    nothing_created(clients)
    assert await events(db, row, "masked_counterparty_matched") == []

    # the owner may say which one, or approve the masked name as it is
    chosen = await service.review(row, "approve", {"merchant": "Paulo Betancourt"})
    assert chosen.status == "created" and chosen.merchant == "PAULO BETANCOURT"
    clients[0].create_account.assert_not_awaited()


async def test_a_correction_with_an_ambiguous_mask_is_refused_and_one_with_a_single_match_is_resolved(service, clients, db, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", masked_transfer(extraction))
    known(clients, "PAULA BETANCUR", "PAULO BETANCOURT")
    row = await service.process(await item(service))
    with pytest.raises(ValueError, match="counterparty_ambiguous"):
        await service.review(row, "approve", {"merchant": "PAU*** BET***"})
    nothing_created(clients)

    clients[0].accounts.return_value = [account for account in clients[0].accounts.return_value if account["name"] != "PAULO BETANCOURT"]
    await service.sync(force=True)
    row = await service.review(row, "approve", {"merchant": "PAU*** BET***"})
    assert row.status == "created" and row.merchant == "PAULA BETANCUR"
    assert len(await events(db, row, "masked_counterparty_matched")) == 1
    clients[0].create_account.assert_not_awaited()


async def test_a_masked_counterparty_without_candidates_keeps_its_masked_name(service, clients, db, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", masked_transfer(extraction, "PAU***** BET**"))
    row = await service.process(await item(service))
    assert row.status == "created" and row.merchant == MASKED
    clients[0].create_account.assert_awaited_once_with(MASKED, "expense")
    assert await events(db, row, "masked_counterparty_matched") == []


async def test_masked_names_already_in_firefly_are_not_candidates(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", masked_transfer(extraction, "PAU*** BET***"))
    known(clients, "PAU*** BET***", "PAU*** BET")
    row = await service.process(await item(service))
    assert row.status == "created" and row.merchant == MASKED
    clients[0].create_account.assert_not_awaited()  # the masked account that exists is reused, not "matched"


async def test_a_name_with_a_different_number_of_tokens_does_not_match(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", masked_transfer(extraction))
    known(clients, "PAULA MARIA BETANCUR", "PAULA")
    row = await service.process(await item(service))
    assert row.status == "created" and row.merchant == MASKED
    clients[0].create_account.assert_awaited_once_with(MASKED, "expense")


@pytest.mark.parametrize(("raw", "expected"), [("UBER *TRIP", "UBER *TRIP"), ("PAYU*NETFLIX", "NETFLIX"), ("UBR*CAFE", "CAFE")])
async def test_legitimate_asterisks_are_left_alone(service, clients, db, extraction, monkeypatch, raw, expected):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: replace(extraction, merchant=raw))
    known(clients, "UBER TRIP", "UBER TRIPS", "NETFLIX COM", "CAFE SOL")
    row = await service.process(await item(service))
    assert row.status == "created" and row.merchant == expected
    assert await events(db, row, "masked_counterparty_matched") == []
    if raw == "UBER *TRIP":
        clients[0].create_account.assert_awaited_once_with("UBER *TRIP", "expense")
