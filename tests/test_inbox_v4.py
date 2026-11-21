"""Phase 4: inbox filters, unparsed queue, classify, reject/suspicious, description/tags and audit."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy import select

from app.api.dependencies import hash_secret
from app.core.timezone import now_utc
from app.db.models_devices import Device
from app.db.models_rules import Rule
from app.services import flags

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}


async def make(service, **changes):
    values = dict(
        source="sms", source_ref=str(uuid4()), sender="85954", subject_or_title="Compra", raw_text=None,
        occurred_at=now_utc() - timedelta(hours=1), status="created", method="parser", transaction_type="withdrawal",
        amount=Decimal("12000.00"), currency="COP", merchant="SUPERMERCADO CENTRAL", asset_account="Nequi",
        firefly_transaction_id=str(uuid4()),
    )
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


async def pending_review(service, **changes):
    """An item that was never written to Firefly and awaits a human decision."""
    values = dict(status="needs_review", firefly_transaction_id=None, review_reason="merchant is not clean", merchant="TIENDA UNO")
    values.update(changes)
    return await make(service, **values)


async def device(db, name, scopes):
    key = f"{name}-key-" + "0123456789" * 3
    row = Device(name=name, key_hash=hash_secret(key), scopes=scopes)
    db.add(row)
    await db.commit()
    return {"X-API-Key": key}, row.id


async def audit_entries(client, action):
    response = await client.get("/api/v1/audit", headers=ADMIN, params={"action": action})
    assert response.status_code == 200
    return response.json()


# --- filters ---------------------------------------------------------------------------------------


async def ids_for(client, **params):
    response = await client.get("/api/v1/inbox", headers=ADMIN, params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(body["items"])
    return {item["id"] for item in body["items"]}


async def test_inbox_filters(client, service):
    now = now_utc()
    old = await make(service, merchant="TIENDA UNO", occurred_at=now - timedelta(days=3), tags=["viaje"], description="regalo para mamá", review_reason="possible duplicate manual/automatic transaction")
    mid = await make(service, merchant="CAFE DOS", occurred_at=now - timedelta(days=1), status="needs_review", review_reason="merchant is not clean")
    new = await make(service, merchant="OTRO", occurred_at=now - timedelta(minutes=5))
    old_id, mid_id, new_id = old.id, mid.id, new.id
    cutoff = (now - timedelta(days=2)).isoformat()

    assert await ids_for(client) == {old_id, mid_id, new_id}
    assert await ids_for(client, since=cutoff) == {mid_id, new_id}
    assert await ids_for(client, until=cutoff) == {old_id}
    assert await ids_for(client, since=(now - timedelta(days=4)).isoformat(), until=cutoff) == {old_id}
    assert await ids_for(client, review_reason="DUPLICATE") == {old_id}
    assert await ids_for(client, q="tienda") == {old_id}
    assert await ids_for(client, q="REGALO") == {old_id}
    assert await ids_for(client, q="%") == set()
    assert await ids_for(client, tag="viaje") == {old_id}
    assert await ids_for(client, tag="via") == set()
    assert await ids_for(client, status="needs_review", q="cafe") == {mid_id}

    items = (await client.get("/api/v1/inbox", headers=ADMIN, params={"tag": "viaje"})).json()["items"]
    assert items[0]["description"] == "regalo para mamá" and items[0]["tags"] == ["viaje"]
    assert (await client.get("/api/v1/inbox", headers=ADMIN, params={"status": "created"})).json()["items"][0]["tags"] == []


async def test_inbox_filters_require_timezone(client):
    response = await client.get("/api/v1/inbox", headers=ADMIN, params={"since": "2026-01-01T00:00:00"})
    assert response.status_code == 422 and response.json()["code"] == "validation_error"


# --- unparsed queue --------------------------------------------------------------------------------


async def test_unparsed_queue_lists_unreliable_items_with_redacted_text(client, service):
    no_amount = await pending_review(service, amount=None, raw_text="Nequi tarjeta 1234567890123 compra")
    failed = await make(service, status="failed", merchant=None, firefly_transaction_id=None)
    blank = await pending_review(service, merchant="")
    complete = await pending_review(service)
    created = await make(service, merchant=None)
    expected = {no_amount.id, failed.id, blank.id}
    complete_id, created_id = complete.id, created.id

    response = await client.get("/api/v1/inbox/unparsed", headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3 and {item["id"] for item in body["items"]} == expected
    assert complete_id not in expected and created_id not in expected
    first = next(item for item in body["items"] if item["id"] == no_amount.id)
    assert "1234567890123" not in first["raw_text_redacted"] and "compra" in first["raw_text_redacted"]
    assert all("raw_text" not in item for item in body["items"])

    page = (await client.get("/api/v1/inbox/unparsed", headers=ADMIN, params={"limit": 1})).json()
    assert page["total"] == 3 and len(page["items"]) == 1


# --- classify --------------------------------------------------------------------------------------


async def test_classify_approves_with_corrections_and_learns_a_rule(client, service, clients, db):
    item = await pending_review(service, category=None)
    item_id = item.id
    response = await client.post(f"/api/v1/inbox/{item_id}/classify", headers=ADMIN, json={"category": "alimentación", "create_rule": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["item"]["status"] == "created" and body["item"]["category"] == "Alimentación"
    assert body["rule"]["match"] == "merchant_exact" and body["rule"]["pattern"] == "TIENDA UNO"
    assert body["rule"]["category"] == "Alimentación" and body["rule"]["source"] == "learned"
    clients[0].create_transaction.assert_awaited_once()
    assert (await db.scalar(select(Rule))).pattern == "TIENDA UNO"
    assert (await audit_entries(client, "rule.create"))["total"] == 1
    approvals = (await audit_entries(client, "inbox.approve"))["items"]
    assert approvals[0]["detail"]["changes"]["category"] == {"old": None, "new": "Alimentación"}


async def test_classify_without_rule_returns_null_rule(client, service):
    item = await pending_review(service, category=None)
    response = await client.post(f"/api/v1/inbox/{item.id}/classify", headers=ADMIN, json={"category": "Transporte"})
    assert response.status_code == 200
    assert response.json()["rule"] is None and response.json()["item"]["category"] == "Transporte"


async def test_classify_rule_needs_a_category_and_changes_nothing_otherwise(client, service, clients):
    item = await pending_review(service, category=None)
    item_id = item.id
    response = await client.post(f"/api/v1/inbox/{item_id}/classify", headers=ADMIN, json={"create_rule": True})
    assert response.status_code == 422
    clients[0].create_transaction.assert_not_awaited()
    assert (await service.inbox.get(item_id)).status == "needs_review"


async def test_classify_delegates_rule_creation_to_the_rules_catalog(client, service, monkeypatch):
    from types import SimpleNamespace

    from app.services import rules

    calls = []

    async def create_rule(session, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=7, **kwargs)

    monkeypatch.setattr(rules, "create_rule", create_rule)
    monkeypatch.setattr(rules, "rule_data", lambda rule: {"id": rule.id, "pattern": rule.pattern})
    item = await pending_review(service, category=None)
    response = await client.post(f"/api/v1/inbox/{item.id}/classify", headers=ADMIN, json={"category": "Transporte", "create_rule": True})
    assert response.status_code == 200
    assert calls == [{"match": "merchant_exact", "pattern": "TIENDA UNO", "category": "Transporte", "source": "learned"}]
    assert response.json()["rule"] == {"id": 7, "pattern": "TIENDA UNO"}


# --- reject / suspicious ---------------------------------------------------------------------------


async def test_reject_suspicious_marks_it_with_the_actor(client, service, monkeypatch):
    calls = []

    async def mark_suspicious(session, item, principal=None):
        calls.append((item.id, principal.name))

    monkeypatch.setattr(flags, "mark_suspicious", mark_suspicious, raising=False)
    plain = await pending_review(service)
    suspect = await pending_review(service)
    plain_id, suspect_id = plain.id, suspect.id

    response = await client.post(f"/api/v1/inbox/{plain_id}/reject", headers=ADMIN)
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert calls == []
    response = await client.post(f"/api/v1/inbox/{suspect_id}/reject", headers=ADMIN, json={"suspicious": True})
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert calls == [(suspect_id, "admin")]


async def test_reject_audits_status_change(client, service):
    item = await pending_review(service)
    await client.post(f"/api/v1/inbox/{item.id}/reject", headers=ADMIN)
    entry = (await audit_entries(client, "inbox.reject"))["items"][0]
    assert entry["detail"] == {"changes": {"status": {"old": "needs_review", "new": "rejected"}}}
    assert entry["target_type"] == "inbox_item" and entry["actor"] == "admin"


async def test_void_audits_and_deletes_in_firefly(client, service, clients):
    item = await make(service, firefly_transaction_id="tx-gone")
    response = await client.post(f"/api/v1/inbox/{item.id}/void", headers=ADMIN)
    assert response.status_code == 200 and response.json()["status"] == "voided"
    clients[0].delete_transaction.assert_awaited_once_with("tx-gone")
    entry = (await audit_entries(client, "inbox.void"))["items"][0]
    assert entry["detail"]["changes"]["status"] == {"old": "created", "new": "voided"}


# --- description, tags and audit -------------------------------------------------------------------


async def test_patch_description_and_tags_reach_firefly_and_audit(client, service, clients):
    item = await make(service, raw_text="Compra 1234567890 SUPERMERCADO", category="Alimentación")
    item_id = item.id
    response = await client.patch(f"/api/v1/inbox/{item_id}", headers=ADMIN, json={"description": "  Mercado de la semana ", "tags": ["casa", "casa", " viaje "], "category": "Transporte"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["description"] == "Mercado de la semana" and body["tags"] == ["casa", "viaje"]
    split = clients[0].update_transaction.call_args.args[1]["transactions"][0]
    assert split["description"] == "Mercado de la semana"
    assert split["tags"] == ["auto", "sms", "casa", "viaje"] and split["category_name"] == "Transporte"

    entry = (await audit_entries(client, "inbox.edit"))["items"][0]
    assert entry["detail"]["changes"] == {
        "category": {"old": "Alimentación", "new": "Transporte"},
        "description": {"old": None, "new": "Mercado de la semana"},
        "tags": {"old": [], "new": ["casa", "viaje"]},
    }
    assert entry["target_id"] == str(item_id) and entry["device_id"] is None
    assert "1234567890" not in json.dumps(entry)

    # Clearing the description falls back to the original subject.
    assert (await client.patch(f"/api/v1/inbox/{item_id}", headers=ADMIN, json={"description": None})).json()["description"] is None
    assert clients[0].update_transaction.call_args.args[1]["transactions"][0]["description"] == "Compra"
    assert (await audit_entries(client, "inbox.edit"))["items"][0]["detail"]["changes"] == {"description": {"old": "Mercado de la semana", "new": None}}


async def test_corrections_limits(client, service):
    item = await make(service)
    url = f"/api/v1/inbox/{item.id}"
    for body in ({"tags": [str(n) for n in range(11)]}, {"tags": [""]}, {"tags": ["x" * 41]}, {"description": "x" * 501}):
        response = await client.patch(url, headers=ADMIN, json=body)
        assert response.status_code == 422 and response.json()["code"] == "validation_error"


async def test_every_approve_is_audited_with_or_without_changes(client, service):
    unchanged = await pending_review(service)
    changed = await pending_review(service)
    unchanged_id, changed_id = unchanged.id, changed.id
    assert (await client.post(f"/api/v1/inbox/{unchanged_id}/approve", headers=ADMIN, json={})).json()["status"] == "created"
    entries = (await audit_entries(client, "inbox.approve"))["items"]
    assert len(entries) == 1 and entries[0]["target_id"] == str(unchanged_id) and entries[0]["detail"] == {"changes": {}}
    response = await client.post(f"/api/v1/inbox/{changed_id}/approve", headers=ADMIN, json={"merchant": "OTRA TIENDA", "tags": ["viaje"]})
    assert response.status_code == 200 and response.json()["status"] == "created"
    entries = (await audit_entries(client, "inbox.approve"))["items"]
    assert len(entries) == 2 and entries[0]["target_id"] == str(changed_id)
    assert entries[0]["detail"]["changes"]["merchant"] == {"old": "TIENDA UNO", "new": "OTRA TIENDA"}
    assert entries[0]["detail"]["changes"]["tags"] == {"old": [], "new": ["viaje"]}


async def test_review_routes_audit_the_device_and_enforce_scope(client, service, db):
    reviewer, reviewer_id = await device(db, "reviewer", ["read", "review"])
    reader, _ = await device(db, "reader", ["read"])
    item = await make(service)
    item_id = item.id
    assert (await client.get("/api/v1/inbox", headers=reader)).status_code == 200
    assert (await client.get("/api/v1/inbox/unparsed", headers=reader)).status_code == 200
    for method, path, body in (
        ("PATCH", f"/api/v1/inbox/{item_id}", {"description": "x"}),
        ("POST", f"/api/v1/inbox/{item_id}/approve", {}),
        ("POST", f"/api/v1/inbox/{item_id}/reject", {"suspicious": True}),
        ("POST", f"/api/v1/inbox/{item_id}/classify", {"category": "Transporte"}),
        ("POST", f"/api/v1/inbox/{item_id}/void", None),
    ):
        response = await client.request(method, path, headers=reader, json=body)
        assert response.status_code == 403 and response.json()["code"] == "insufficient_scope", path
    assert (await client.patch(f"/api/v1/inbox/{item_id}", headers=reviewer, json={"description": "x"})).status_code == 200
    entry = (await audit_entries(client, "inbox.edit"))["items"][0]
    assert entry["actor"] == "reviewer" and entry["device_id"] == reviewer_id


async def test_inbox_filters_by_exact_source_ref(client, service):
    target = await make(service, source_ref="sms:abc-1")
    await make(service, source_ref="sms:abc-10")
    await make(service, source_ref="other")
    assert await ids_for(client, source_ref="sms:abc-1") == {target.id}
    assert await ids_for(client, source_ref="missing") == set()


# --- suspicious reports on auto-approved charges ---------------------------------------------------


async def test_reject_suspicious_on_a_created_item_voids_it_and_pauses_auto_approval(client, service, clients, db):
    item = await make(service, firefly_transaction_id="tx-auto")
    item_id = item.id
    plain = await client.post(f"/api/v1/inbox/{item_id}/reject", headers=ADMIN)
    assert plain.status_code == 422
    clients[0].delete_transaction.assert_not_awaited()
    assert (await flags.status(db))["auto_approve_paused"] is False

    response = await client.post(f"/api/v1/inbox/{item_id}/reject", headers=ADMIN, json={"suspicious": True})
    assert response.status_code == 200 and response.json()["status"] == "voided"
    clients[0].delete_transaction.assert_awaited_once_with("tx-auto")
    assert (await flags.status(db))["auto_approve_paused"] is True
    assert (await audit_entries(client, "inbox.void"))["total"] == 1
    assert (await audit_entries(client, "flags.suspicious"))["items"][0]["target_id"] == str(item_id)


async def test_reject_suspicious_failure_is_reported_and_retry_is_safe(client, service, clients, db, monkeypatch):
    item = await make(service, firefly_transaction_id="tx-auto")
    item_id = item.id
    real, calls = flags.mark_suspicious, []

    async def flaky(session, target, principal=None):
        calls.append(target.id)
        if len(calls) == 1:
            raise RuntimeError("boom")
        await real(session, target, principal)

    monkeypatch.setattr(flags, "mark_suspicious", flaky)
    first = await client.post(f"/api/v1/inbox/{item_id}/reject", headers=ADMIN, json={"suspicious": True})
    assert first.status_code == 503 and first.json()["code"] == "suspicious_not_marked" and "retry" in first.json()["detail"]
    assert (await service.inbox.get(item_id)).status == "voided"
    assert (await flags.status(db))["auto_approve_paused"] is False

    second = await client.post(f"/api/v1/inbox/{item_id}/reject", headers=ADMIN, json={"suspicious": True})
    assert second.status_code == 200 and second.json()["status"] == "voided"
    clients[0].delete_transaction.assert_awaited_once_with("tx-auto")
    assert calls == [item_id, item_id] and (await flags.status(db))["auto_approve_paused"] is True


# --- audit of reprocess, retry-failed and raw reads ------------------------------------------------


async def test_reprocess_and_retry_failed_are_audited(client, service, monkeypatch):
    monkeypatch.setattr(service, "process", AsyncMock())
    first = await make(service, status="failed", firefly_transaction_id=None)
    second = await make(service, status="failed", firefly_transaction_id=None)
    first_id = first.id
    assert (await client.post(f"/api/v1/inbox/{first_id}/reprocess", headers=ADMIN)).status_code == 200
    entry = (await audit_entries(client, "inbox.reprocess"))["items"][0]
    assert entry["target_id"] == str(first_id) and entry["actor"] == "admin" and entry["detail"] == {"previous_status": "failed"}

    assert (await client.post("/api/v1/inbox/retry-failed", headers=ADMIN)).json() == {"requeued": 1}
    entry = (await audit_entries(client, "inbox.retry_failed"))["items"][0]
    assert entry["actor"] == "admin" and entry["detail"] == {"requeued": 1}
    assert second.id and (await audit_entries(client, "inbox.reprocess"))["total"] == 1


async def test_reading_the_raw_text_is_audited_without_the_content(client, service, db):
    reviewer, reviewer_id = await device(db, "reviewer", ["read", "review"])
    reader, _ = await device(db, "reader", ["read"])
    item = await make(service, raw_text="Compra con tarjeta 1234567890123 token-ultra-secreto")
    item_id = item.id
    assert (await client.get(f"/api/v1/inbox/{item_id}", headers=ADMIN)).status_code == 200
    assert (await audit_entries(client, "inbox.raw_view"))["total"] == 0
    assert (await client.get(f"/api/v1/inbox/{item_id}", headers=reader, params={"include_raw": "true"})).status_code == 403
    assert (await audit_entries(client, "inbox.raw_view"))["total"] == 0

    response = await client.get(f"/api/v1/inbox/{item_id}", headers=reviewer, params={"include_raw": "true"})
    assert response.status_code == 200 and "token-ultra-secreto" in response.json()["raw_text"]
    listing = await audit_entries(client, "inbox.raw_view")
    assert listing["total"] == 1
    entry = listing["items"][0]
    assert entry["target_type"] == "inbox_item" and entry["target_id"] == str(item_id)
    assert entry["actor"] == "reviewer" and entry["device_id"] == reviewer_id
    assert "token-ultra-secreto" not in json.dumps(entry) and "1234567890123" not in json.dumps(entry)
