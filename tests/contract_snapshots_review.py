"""Contract fixtures of ServerSnapshots and ServerAiReview (offline, Firefly and the LLM are mocks).

Instant dashboard, digest policy, server errors, description fix, AI usage, AI suggestions of the review queue and
counterparty consolidation. Every clock an endpoint reads is pinned to a fixed 2026 instant, and everything that is
patched lives in ``monkeypatch.context()`` blocks so nothing leaks into the other sections.
"""
import itertools
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import SecretStr
from sqlalchemy import delete

from app.api.routers import inbox as inbox_router
from app.api.routers import maintenance
from app.clients.firefly import FireflyClient
from app.db.models import AiUsage, InboxItem, ServerError
from app.db.models_flags import RuntimeFlag
from app.services import ai_budget, ai_review, counterparties, descriptions, snapshots
from tests.test_counterparties_v4 import FakeLedger

SNAPSHOT_NOW = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)  # 12:00 in Bogota
AI_NOW = datetime(2026, 11, 8, 17, 0, tzinfo=UTC)  # the "today" of the AI budget and of the merge deadline
SEEN = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)  # movement date of every item seeded here: it never touches the November reports
GENERIC = "Alertas y Notificaciones"


def plain_firefly():
    """A Firefly mock with the defaults of the shared one (own account, one expense account, two categories)."""
    firefly = AsyncMock(spec=FireflyClient)
    firefly.accounts.return_value = [
        {"id": "1", "name": "Nequi", "type": "asset"},
        {"id": "2", "name": "SUPERMERCADO CENTRAL", "type": "expense"},
    ]
    firefly.categories.return_value = ["Alimentación", "Transporte"]
    firefly.find_by_external_id.return_value = None
    firefly.create_account.return_value = {"id": "5", "name": "OTRO", "type": "expense"}
    return firefly


def session_of(db):
    """A ``get_db_session`` stand-in that hands the test session to a background task."""
    @asynccontextmanager
    async def opener():
        yield db

    return opener


async def record(env):
    await _snapshots(env)
    await _digest(env)
    await _server_errors(env)
    await _descriptions(env)
    await _ai_review(env)


# --- Instant dashboard ---------------------------------------------------------------------------------------------


def _split(kind, when, amount, category=None, dest="TIENDA", source="Nequi", currency="COP"):
    return {
        "type": kind, "date": when, "amount": amount, "category_name": category, "destination_name": dest,
        "source_name": source, "currency_code": currency,
    }


SPLITS = [
    _split("withdrawal", "2026-11-02T12:00:00-05:00", "100000.00", "Mercado", "EXITO"),
    _split("withdrawal", "2026-11-02T18:00:00-05:00", "50000.00", "Mercado", "EXITO"),
    _split("withdrawal", "2026-11-03T09:00:00-05:00", "30000.00", None, ""),
    _split("deposit", "2026-11-01T10:00:00-05:00", "1000000.00", None, "Nequi", "EMPRESA"),
    _split("transfer", "2026-11-04T10:00:00-05:00", "5000.00"),
    _split("withdrawal", "2026-11-05T10:00:00-05:00", "20.00", "Ocio", "STEAM", currency="USD"),  # not COP: ignored
    _split("withdrawal", "2026-11-09T10:00:00-05:00", "9999.00", "Futuro"),  # after "today"
    _split("withdrawal", "2026-10-31T23:30:00-05:00", "7000.00", "Octubre"),  # still October in Bogota
    _split("withdrawal", "2026-11-01T02:00:00+00:00", "3000.00", "Octubre"),  # 21:00 on October 31 in Bogota
]


async def _snapshots(env):
    rec, reviewer = env.rec, env.reviewer
    firefly = AsyncMock(spec=FireflyClient)
    firefly.period_splits.return_value = SPLITS
    firefly.insight_total.side_effect = lambda kind, start, end: Decimal("100.00") if kind == "expense" else Decimal("300.00")
    firefly.budget_progress.return_value = [{"name": "Mercado", "spent": Decimal("850000.00"), "limit": Decimal("1000000.00")}]
    with env.monkeypatch.context() as patch:
        patch.setattr(snapshots, "now_utc", lambda: SNAPSHOT_NOW)
        patch.setattr("app.services.reports.now_utc", lambda: SNAPSHOT_NOW)
        patch.setattr("app.api.routers.snapshots.get_firefly", lambda: firefly)
        patch.setattr("app.api.routers.snapshots.get_settings", lambda: env.settings)

        # First read: nothing stored yet, so it is computed in line.
        first = await rec.call("get_dashboard_snapshot", "GET", "/dashboard/snapshot", reviewer, params={"month": "2026-11"})
        data = first["data"]
        assert first["month"] == "2026-11" and first["stale"] is False
        assert data["is_current"] is True and data["as_of"] == "2026-11-06" and data["currency"] == "COP"
        assert data["totals"] == {"withdrawal": "180000.00", "deposit": "1000000.00", "transfer": "5000.00"}
        assert (data["spent"], data["income"], data["balance"]) == ("180000.00", "1000000.00", "820000.00")
        assert data["by_category"] == [
            {"category": "Mercado", "spent": "150000.00", "count": 2}, {"category": "Sin categoría", "spent": "30000.00", "count": 1},
        ]
        assert data["by_account"] == [{"account": "Nequi", "spent": "180000.00"}]
        assert data["top_merchants"][0] == {"merchant": "EXITO", "spent": "150000.00", "count": 2}
        assert data["top_merchants"][1]["merchant"] == "Sin comercio"
        assert len(data["daily"]) == 30 and data["daily"][1] == {"date": "2026-11-02", "spent": "150000.00", "income": "0.00"}
        assert len(data["cashflow"]) == 12 and data["cashflow"][-1] == {"month": "2026-11", "spent": "100.00", "income": "300.00"}
        assert data["budgets"] == [{"name": "Mercado", "spent": "850000.00", "limit": "1000000.00", "pct": 85}]
        assert data["forecast"]["month"] == "2026-11" and data["daily_available"] == data["forecast"]["daily_available"]
        assert data["unavailable"] == []

        # A past month has no forecast.
        october = await rec.call(None, "GET", "/dashboard/snapshot", reviewer, params={"month": "2026-10"})
        assert october["data"]["is_current"] is False and october["data"]["forecast"] is None and october["data"]["daily_available"] is None
        assert october["data"]["totals"]["withdrawal"] == "10000.00"

        # Refresh recomputes with what Firefly says now.
        firefly.period_splits.return_value = [*SPLITS, _split("withdrawal", "2026-11-06T09:30:00-05:00", "12000.00", "Transporte", "UBER")]
        refreshed = await rec.call("refresh_dashboard_snapshot", "POST", "/dashboard/snapshot/refresh", reviewer, params={"month": "2026-11"})
        assert refreshed["stale"] is False and refreshed["data"]["spent"] == "192000.00" and refreshed["data"]["balance"] == "808000.00"
        assert [row["category"] for row in refreshed["data"]["by_category"]] == ["Mercado", "Sin categoría", "Transporte"]

        await rec.error("error_422_invalid_month", "GET", "/dashboard/snapshot", reviewer, 422, "invalid_month", params={"month": "2026-13"})


# --- Digest policy -------------------------------------------------------------------------------------------------


async def _digest(env):
    rec, reviewer = env.rec, env.reviewer
    defaults = await rec.call("get_digest_settings", "GET", "/settings/digest", reviewer)
    assert defaults == {"daily": True, "weekly": True, "hour": 20, "silence_days": 3}
    saved = await rec.call(
        "update_digest_settings", "PUT", "/settings/digest", reviewer, json={"daily": True, "weekly": False, "hour": 7, "silence_days": 5},
    )
    assert saved == {"daily": True, "weekly": False, "hour": 7, "silence_days": 5}
    await rec.call(None, "PUT", "/settings/digest", reviewer, json=defaults)  # the policy goes back to what the other sections expect


# --- Server errors -------------------------------------------------------------------------------------------------


async def _server_errors(env):
    rec, reviewer = env.rec, env.reviewer
    rows = [
        ServerError(
            at=datetime(2026, 11, 3, 9, 15, tzinfo=UTC), component="firefly", operation="POST transactions", message="Firefly 422: The given data was invalid.",
            exc_type="PermanentError", status_code=422, response_body='{"message": "The given data was invalid.", "errors": {"description": ["required"]}}',
        ),
        ServerError(at=datetime(2026, 11, 3, 18, 40, tzinfo=UTC), component="llm", operation="complete_json", message="LLM chain exhausted", exc_type="RuntimeError"),
        ServerError(
            at=datetime(2026, 11, 4, 7, 5, tzinfo=UTC), component="mailbox", operation="poll gmail", message="IMAP poll failed (2 consecutive)", exc_type="OSError",
        ),
        ServerError(at=datetime(2026, 11, 4, 22, 30, tzinfo=UTC), component="cycle", operation="job cycle", message="job cycle failed", exc_type="ValueError"),
        ServerError(
            at=datetime(2026, 11, 5, 14, 2, tzinfo=UTC), component="firefly", operation="GET accounts", message="Firefly temporarily unavailable (503)",
            exc_type="FireflyError", status_code=503, response_body="Service Unavailable",
        ),
    ]
    env.db.add_all(rows)
    await env.db.commit()
    operations = [row.operation for row in rows]

    page = await rec.call("list_server_errors", "GET", "/system/errors", reviewer, params={"since": "2026-11-01T00:00:00Z", "limit": 50, "offset": 0})
    assert page["total"] >= 5 and [item["operation"] for item in page["items"][:5]] == operations[::-1]
    newest = page["items"][0]
    assert (newest["component"], newest["exc_type"], newest["status_code"], newest["response_body"]) == ("firefly", "FireflyError", 503, "Service Unavailable")
    assert page["items"][3]["exc_type"] == "RuntimeError" and page["items"][3]["status_code"] is None and page["items"][3]["response_body"] is None

    only = await rec.call(None, "GET", "/system/errors", reviewer, params={"component": "firefly", "since": "2026-11-01T00:00:00Z"})
    assert {item["component"] for item in only["items"]} == {"firefly"} and [item["operation"] for item in only["items"][:2]] == ["GET accounts", "POST transactions"]
    paged = await rec.call(None, "GET", "/system/errors", reviewer, params={"since": "2026-11-01T00:00:00Z", "limit": 1, "offset": 1})
    assert len(paged["items"]) == 1 and paged["items"][0]["operation"] == "job cycle" and paged["total"] == page["total"]


# --- Description fix -----------------------------------------------------------------------------------------------


def _firefly_group(firefly_id, description, ref):
    return {"id": firefly_id, "attributes": {"group_title": None, "transactions": [{
        "transaction_journal_id": firefly_id, "type": "withdrawal", "date": "2026-09-14T10:00:00-05:00", "amount": "12000.00",
        "currency_code": "COP", "source_id": "1", "source_name": "Nequi", "destination_id": "2", "destination_name": "SUPERMERCADO CENTRAL",
        "description": description, "external_id": f"fa:{ref}", "tags": ["auto", "email"],
    }]}}


def _created_email(ref, firefly_id, **changes):
    values = dict(
        source="email", source_ref=ref, sender="alertas@bancolombia.com.co", subject_or_title=GENERIC, raw_text="Pagaste $12.000", status="created",
        method="parser", template_id="bancolombia.compraste", original_kind="withdrawal", transaction_type="withdrawal", amount=Decimal("12000.00"),
        currency="COP", merchant="EXITO CALLE 80", asset_account="Nequi", firefly_transaction_id=firefly_id, occurred_at=SEEN,
    )
    values.update(changes)
    return InboxItem(**values)


async def _descriptions(env):
    rec, reviewer, db = env.rec, env.reviewer, env.db
    descriptions.invalidate()
    compra = _created_email("ctr-desc-compra", "9101")
    pago = _created_email("ctr-desc-pago", "9102", template_id="bancolombia.pagaste", merchant="DROGUERIA LA 80")
    summary = _created_email("ctr-desc-llm", "9103", method="llm", template_id="llm:x", merchant="CAFE LUNA", raw_text="Compra en CAFE LUNA")
    edited = _created_email("ctr-desc-edited", "9104", template_id="bancolombia.pagaste", merchant="PAPELERIA NORTE")  # the owner retitled it in Firefly
    db.add_all([compra, pago, summary, edited])
    await db.commit()
    mine = {compra.id: "Compra en EXITO CALLE 80", pago.id: "Pago a DROGUERIA LA 80", summary.id: "Compra en CAFE LUNA", edited.id: "Pago a PAPELERIA NORTE"}

    firefly = plain_firefly()
    store = {
        "9101": _firefly_group("9101", GENERIC, "ctr-desc-compra"), "9102": _firefly_group("9102", GENERIC, "ctr-desc-pago"),
        "9103": _firefly_group("9103", GENERIC, "ctr-desc-llm"), "9104": _firefly_group("9104", "Papelería del barrio", "ctr-desc-edited"),
    }

    async def get_transaction(firefly_id):
        return store.get(str(firefly_id))

    async def update_transaction(firefly_id, payload):
        store[str(firefly_id)]["attributes"]["transactions"][0]["description"] = payload["transactions"][0]["description"]
        return str(firefly_id)

    firefly.get_transaction.side_effect = get_transaction
    firefly.update_transaction.side_effect = update_transaction

    with env.monkeypatch.context() as patch:
        patch.setattr(maintenance, "get_db_session", session_of(db))
        patch.setattr(maintenance, "get_firefly", lambda: firefly)
        patch.setattr(maintenance, "get_llm", lambda: env.clients[1])
        patch.setattr(maintenance, "_fix_task", None)

        dry = await rec.call("fix_descriptions", "POST", "/maintenance/descriptions/fix", reviewer, params={"dry_run": "true"})
        assert dry["dry_run"] is True and dry["count"] >= 4 and len(dry["examples"]) == min(dry["count"], 10)
        by_id = {example["id"]: example for example in dry["examples"]}
        for item_id, after in mine.items():
            assert by_id[item_id] == {"id": item_id, "before": GENERIC, "after": after}
        firefly.update_transaction.assert_not_awaited()

        queued = await rec.call("fix_descriptions_queued", "POST", "/maintenance/descriptions/fix", reviewer, status=202)
        assert queued["queued"] == dry["count"] and len(queued["group_id"]) == 32
        await maintenance._fix_task
        written = {key: value["attributes"]["transactions"][0]["description"] for key, value in store.items()}
        assert written == {"9101": "Compra en EXITO CALLE 80", "9102": "Pago a DROGUERIA LA 80", "9103": "Compra en CAFE LUNA", "9104": "Papelería del barrio"}
        rest = await rec.call(None, "POST", "/maintenance/descriptions/fix", reviewer, params={"dry_run": "true"})
        assert rest["count"] < dry["count"] and edited.id in {example["id"] for example in rest["examples"]}  # only what was skipped stays


# --- AI usage, AI suggestions and counterparties -------------------------------------------------------------------


def _answer(**fields):
    return {
        "action": "approve", "firefly_account": "Nequi", "category": "Alimentación", "merchant": "Tienda Uno", "transaction_type": "withdrawal",
        "amount": None, "duplicate_of_id": None, "confidence": 0.9, "reason": "Comercio habitual",
    } | fields


async def _seed_item(env, ref, merchant, **changes):
    values = dict(
        source="sms", source_ref=ref, sender="85954", subject_or_title="Compra", raw_text=f"Compraste $12.000 en {merchant}", occurred_at=SEEN,
        status="needs_review", method="parser", transaction_type="withdrawal", amount=Decimal("12000.00"), currency="COP", merchant=merchant,
        asset_account="Nequi", review_reason="merchant is not clean",
    )
    values.update(changes)
    row = await env.service.inbox.add(**values)
    await env.db.commit()
    return row


async def _ai_review(env):
    rec, reviewer, db = env.rec, env.reviewer, env.db
    firefly, llm = env.clients
    assert "Alimentación" in await firefly.categories()
    original = await _seed_item(
        env, "ctr-ai-original", "ALMACEN UNO", status="created", review_reason=None, firefly_transaction_id="ff-ctr-ai-original", category="Alimentación",
    )
    sure = await _seed_item(env, "ctr-ai-sure", "TIENDA UNO")
    void = await _seed_item(env, "ctr-ai-void", "PROMO PUBLICIDAD", raw_text="Promoción de la semana")
    amount = await _seed_item(env, "ctr-ai-amount", "TIENDA TRES", amount=None, raw_text="Pago por $45.000 en TIENDA TRES")
    unsure = await _seed_item(env, "ctr-ai-unsure", "CAFE DOS")
    duplicate = await _seed_item(env, "ctr-ai-duplicate", "ALMACEN UNO")
    mine = {sure.id, void.id, amount.id, unsure.id, duplicate.id}
    answers = {
        "TIENDA UNO": _answer(confidence=0.95, reason="Comercio habitual de mercado"),
        "PROMO PUBLICIDAD": _answer(action="ignore", confidence=0.99, reason="Es publicidad, no un movimiento"),
        "TIENDA TRES": _answer(amount=45000, confidence=0.88, merchant="Tienda Tres", reason="El mensaje trae otro monto"),
        "CAFE DOS": _answer(confidence=0.7, merchant="Cafe Dos", category="Transporte", reason="Poco claro"),
        "ALMACEN UNO": _answer(action="duplicate", duplicate_of_id=original.id, confidence=0.8, reason="Mismo cobro ya registrado"),
    }

    async def fake_llm(prompt, content, cycle_id, usage=None):
        data = content["UNTRUSTED_DATA"]
        if "pairs" in data:
            usage.update(calls=1, total_tokens=10)
            return {"answers": [{"i": pair["i"], "same": "80" in pair["a"], "confidence": 0.8, "reason": "Misma droguería"} for pair in data["pairs"]]}
        usage.update(calls=1, total_tokens=40, model="deepseek-v4.1-flash", allowance={"remaining": "0.85", "unit": "USD"})
        return answers[data["item"]["merchant"]]

    created_ids = itertools.count(1)

    async def create_transaction(payload):
        return f"ff-ctr-ai-{next(created_ids)}"

    owner_eligible = ai_review.eligible_ids

    async def only_mine(session, *, auto):
        # Review items left by the other sections must not take part: the run, its ids and its usage stay deterministic.
        return [item_id for item_id in await owner_eligible(session, auto=auto) if item_id in mine]

    own_session = session_of(db)

    go_settings = SimpleNamespace(timezone="America/Bogota", llm_api_key=SecretStr("go-secret"), llm_user_agent="finance-analyzer/3.0")

    async def go_usage(settings):
        return {"rolling": {"percent": 12}, "weekly": {"percent": 30}}

    with env.monkeypatch.context() as patch:
        patch.setattr(ai_budget, "now_utc", lambda: AI_NOW)
        patch.setattr(counterparties, "now_utc", lambda: AI_NOW)
        patch.setattr(ai_budget, "get_settings", lambda: go_settings)
        patch.setattr(ai_budget, "fetch_go_usage", go_usage)
        patch.setattr(ai_review, "eligible_ids", only_mine)
        patch.setattr(inbox_router, "get_db_session", own_session)
        patch.setattr(inbox_router, "get_firefly", lambda: firefly)
        patch.setattr(inbox_router, "get_llm", lambda: llm)
        patch.setattr(inbox_router, "_suggest_task", None)
        patch.setattr(llm.complete_json, "side_effect", fake_llm)
        patch.setattr(firefly.create_transaction, "side_effect", create_transaction)
        created_before = firefly.create_transaction.await_count

        # -- AI suggestions of the review queue --------------------------------------------------------------------------
        queued = await rec.call("request_ai_suggestions", "POST", "/inbox/ai-suggest", reviewer, status=202)
        assert queued == {"queued": 5}
        await inbox_router._suggest_task
        assert (await rec.call(None, "POST", "/inbox/ai-suggest", reviewer, status=202)) == {"queued": 0}  # every item has a current suggestion

        listing = await rec.call("list_ai_suggestions", "GET", "/inbox/ai-suggestions", reviewer, params={"status": "pending", "limit": 100, "offset": 0})
        entries = [entry for entry in listing["items"] if entry["item"]["id"] in mine]
        assert [entry["item"]["id"] for entry in entries] == [void.id, sure.id, amount.id, duplicate.id, unsure.id]  # most confident first
        suggestion = {entry["item"]["id"]: entry["id"] for entry in entries}
        by_item = {entry["item"]["id"]: entry for entry in entries}
        assert all(entry["status"] == "pending" and entry["error"] is None and entry["model"] == "deepseek-v4.1-flash" for entry in entries)
        assert by_item[sure.id]["confidence"] == 0.95 and by_item[sure.id]["proposal"] == {
            "action": "approve", "firefly_account": "Nequi", "category": "Alimentación", "merchant": "TIENDA UNO",
            "transaction_type": "withdrawal", "amount": None, "duplicate_of_id": None,
        }
        assert by_item[void.id]["proposal"]["action"] == "ignore" and by_item[void.id]["proposal"]["category"] is None
        assert by_item[amount.id]["proposal"]["amount"] == "45000.00"
        assert by_item[duplicate.id]["proposal"]["action"] == "duplicate" and by_item[duplicate.id]["proposal"]["duplicate_of_id"] == original.id
        assert by_item[sure.id]["item"]["status"] == "needs_review"

        discarded = await rec.call("discard_ai_suggestion", "POST", f"/inbox/ai-suggestions/{suggestion[unsure.id]}/discard", reviewer)
        assert discarded["id"] == suggestion[unsure.id] and discarded["status"] == "discarded" and discarded["item"]["id"] == unsure.id
        await rec.error("error_409_suggestion_not_pending", "POST", f"/inbox/ai-suggestions/{suggestion[unsure.id]}/discard", reviewer, 409, "suggestion_not_pending")
        await rec.error("error_404_suggestion_not_found", "POST", "/inbox/ai-suggestions/999999/discard", reviewer, 404, "suggestion_not_found")

        # By id the owner applies the annulment, the amount change and (missing) ids; all_above adds the sure approval.
        applied = await rec.call(
            "apply_ai_suggestions", "POST", "/inbox/ai-suggestions/apply", reviewer,
            json={"ids": [suggestion[void.id], suggestion[amount.id], 999999], "all_above": 0.9},
        )
        assert applied["applied"] == [suggestion[void.id], suggestion[amount.id], suggestion[sure.id]]
        assert applied["failed"] == [{"id": 999999, "detail": "La sugerencia no existe."}]
        states = {
            name: await db.get(InboxItem, row.id, populate_existing=True)
            for name, row in (("sure", sure), ("void", void), ("amount", amount), ("unsure", unsure), ("duplicate", duplicate))
        }
        assert [states[name].status for name in ("sure", "void", "amount", "unsure", "duplicate")] == ["created", "rejected", "created", "needs_review", "needs_review"]
        assert states["amount"].amount == Decimal("45000.00") and states["sure"].category == "Alimentación"
        assert firefly.create_transaction.await_count - created_before == 2

        # -- Counterparties: suggestions, merge and undo (a stateful Firefly with expense and revenue accounts) ------------
        ledger_firefly = AsyncMock(spec=FireflyClient)
        ledger = FakeLedger(ledger_firefly)
        patch.setattr(env.service, "firefly", ledger_firefly)

        groups = await rec.call("list_merge_suggestions", "GET", "/counterparties/merge-suggestions", reviewer)
        assert [group["confidence"] for group in groups] == [0.98, 0.9, 0.85, 0.8]
        by_target = {group["target"]["id"]: group for group in groups}
        assert set(by_target) == {"10", "30", "20", "50"}
        assert by_target["10"]["target"] == {"id": "10", "name": "Éxito"}
        assert by_target["10"]["sources"] == [{"id": "11", "name": "EXITO", "transactions": 2}, {"id": "12", "name": "Exito S.A.S", "transactions": 1}]
        assert by_target["50"]["reason"] == "Misma droguería" and len(groups[0]["id"]) == 12

        await rec.error(
            "error_422_target_in_sources", "POST", "/counterparties/merge", reviewer, 422, "target_in_sources", json={"target_id": "10", "source_ids": ["10", "11"]},
        )
        merged = await rec.call("merge_counterparties", "POST", "/counterparties/merge", reviewer, json={"target_id": "10", "source_ids": ["11", "12"]})
        assert merged["status"] == "applied" and merged["account_type"] == "expense" and merged["target"] == {"id": "10", "name": "Éxito"}
        assert merged["sources"] == [
            {"id": "11", "name": "EXITO", "moved": 2, "planned": 2, "deleted": True, "error": None},
            {"id": "12", "name": "Exito S.A.S", "moved": 1, "planned": 1, "deleted": True, "error": None},
        ]
        assert merged["undone_at"] is None and all(ledger.destination(journal) == "10" for journal in ("j1", "j2", "j3", "j4", "j5", "j6"))

        undone = await rec.call("undo_merge", "POST", f"/counterparties/merge/{merged['id']}/undo", reviewer)
        assert undone["status"] == "undone" and undone["failed"] == [] and undone["undone_at"] is not None and undone["id"] == merged["id"]
        assert {account["name"] for account in ledger.accounts if account["id"] not in {"1", "10", "11", "12"} and account["type"] == "expense"} >= {"EXITO", "Exito S.A.S"}
        await rec.error("error_409_merge_already_undone", "POST", f"/counterparties/merge/{merged['id']}/undo", reviewer, 409, "merge_already_undone")
        await rec.error("error_404_merge_not_found", "POST", "/counterparties/merge/999999/undo", reviewer, 404, "merge_not_found")

        # -- AI usage: what the runs above counted, plus the automatic extraction of the day and yesterday's calls ------
        await ai_budget.record(db, "extract", 6, {"total_tokens": 1200})
        db.add(AiUsage(day=date(2026, 11, 7), kind="extract", calls=10, auto_calls=10, tokens=0))
        await db.commit()
        usage = await rec.call("get_ai_usage", "GET", "/ai/usage", reviewer)
        assert usage["today"]["day"] == "2026-11-08" and usage["month"]["month"] == "2026-11"
        assert usage["today"]["by_kind"] == {
            "ai_suggest": {"calls": 5, "auto_calls": 0, "tokens": 200},
            "counterparty_merge": {"calls": 1, "auto_calls": 0, "tokens": 10},
            "extract": {"calls": 6, "auto_calls": 6, "tokens": 1200},
        }
        assert (usage["today"]["calls"], usage["today"]["auto_calls"], usage["today"]["tokens"]) == (12, 6, 1410)
        assert (usage["month"]["calls"], usage["month"]["auto_calls"]) == (22, 16)
        assert usage["daily_limit"] == 400 and usage["throttled"] is False and usage["throttle_reason"] is None
        assert usage["go_usage"]["data"] == {"rolling": {"percent": 12}, "weekly": {"percent": 30}} and usage["go_usage"]["error"] is None
        assert usage["allowance"] == {"remaining": "0.85", "unit": "USD"}

    # The stored provider document is of a made-up day: drop it so no later section sees a throttled or stale guard.
    await db.execute(delete(RuntimeFlag).where(RuntimeFlag.key == ai_budget.GO_USAGE_KEY))
    await db.commit()
