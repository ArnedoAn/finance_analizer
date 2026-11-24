"""POST /categories: explicit creation of a Firefly category (Firefly mocked)."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.dependencies import hash_secret
from app.core.exceptions import FireflyError
from app.core.timezone import now_utc
from app.db.models_assistant import Audit
from app.db.models_devices import Device

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
URL = "/api/v1/categories"


@pytest.fixture
def firefly(clients):
    """The mocked Firefly keeps its categories: creating one makes the next listing include it."""
    mock, _ = clients
    stored = ["Alimentación", "Transporte"]

    async def create(name):
        stored.append(name)
        return name

    mock.categories.side_effect = lambda: list(stored)
    mock.create_category.side_effect = create
    return mock


async def device_headers(db, name, scopes):
    key = f"{name}-key-" + "0123456789" * 3
    db.add(Device(name=name, key_hash=hash_secret(key), scopes=scopes))
    await db.commit()
    return {"X-API-Key": key}


async def audits(db):
    return list((await db.scalars(select(Audit).where(Audit.action == "category.create"))).all())


async def waiting_item(service):
    row = await service.inbox.add(
        source="sms", source_ref=str(uuid4()), sender="85954", subject_or_title="Compra", raw_text="Compraste $12.000 en TIENDA UNO",
        occurred_at=now_utc() - timedelta(hours=1), status="needs_review", method="parser", transaction_type="withdrawal",
        amount=Decimal("12000.00"), currency="COP", merchant="TIENDA UNO", asset_account="Nequi", review_reason="merchant is not clean",
    )
    await service.session.commit()
    return row


async def test_creates_and_answers_201(client, firefly, db):
    response = await client.post(URL, headers=ADMIN, json={"name": "  Mascotas  "})
    assert response.status_code == 201, response.text
    assert response.json() == {"name": "Mascotas", "created": True}
    firefly.create_category.assert_awaited_once_with("Mascotas")
    [entry] = await audits(db)
    assert entry.target_type == "category" and entry.target_id == "Mascotas" and entry.detail == {"name": "Mascotas"}


@pytest.mark.parametrize("spelling", ["alimentacion", "ALIMENTACIÓN", " Alimentación "])
async def test_existing_name_in_other_spelling_creates_nothing(client, firefly, db, spelling):
    response = await client.post(URL, headers=ADMIN, json={"name": spelling})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "Alimentación", "created": False}
    firefly.create_category.assert_not_awaited()
    assert await audits(db) == []


async def test_corrections_see_the_new_category_after_it_is_created(client, firefly, service):
    first = await waiting_item(service)
    rejected = await client.post(f"/api/v1/inbox/{first.id}/approve", headers=ADMIN, json={"category": "Mascotas"})
    assert rejected.status_code == 422 and "Unknown category" in rejected.json()["detail"]
    assert (await client.post(URL, headers=ADMIN, json={"name": "Mascotas"})).status_code == 201
    second = await waiting_item(service)
    approved = await client.post(f"/api/v1/inbox/{second.id}/approve", headers=ADMIN, json={"category": "mascotas"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["category"] == "Mascotas"


@pytest.mark.parametrize("body", [
    {}, {"name": ""}, {"name": "   "}, {"name": "x" * 65}, {"name": "Uno\nDos"}, {"name": "Uno\rDos"}, {"name": "Uno\u2028Dos"},
    {"name": "Pesos $"}, {"name": "Uno\x00Dos"}, {"name": "Uno\tDos"}, {"name": 5}, {"name": "Mascotas", "extra": 1},
])
async def test_invalid_names_are_422(client, firefly, body):
    response = await client.post(URL, headers=ADMIN, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    firefly.create_category.assert_not_awaited()


async def test_name_of_64_characters_is_accepted(client, firefly):
    assert (await client.post(URL, headers=ADMIN, json={"name": "x" * 64})).status_code == 201


async def test_read_scope_gets_403(client, firefly, db):
    headers = await device_headers(db, "viewer", ["read"])
    response = await client.post(URL, headers=headers, json={"name": "Mascotas"})
    assert response.status_code == 403 and response.json()["code"] == "insufficient_scope"
    firefly.create_category.assert_not_awaited()


async def test_review_scope_may_create(client, firefly, db):
    headers = await device_headers(db, "reviewer", ["review"])
    assert (await client.post(URL, headers=headers, json={"name": "Mascotas"})).status_code == 201
    assert (await audits(db))[0].actor == "reviewer"


async def test_firefly_down_is_502(client, firefly, db):
    firefly.create_category.side_effect = FireflyError("Firefly network request failed")
    response = await client.post(URL, headers=ADMIN, json={"name": "Mascotas"})
    assert response.status_code == 502
    assert response.json() == {"detail": "External service unavailable", "code": "upstream_unavailable"}
    assert await audits(db) == []


async def test_firefly_down_while_listing_is_502(client, firefly):
    firefly.categories.side_effect = FireflyError("Firefly network request failed")
    response = await client.post(URL, headers=ADMIN, json={"name": "Mascotas"})
    assert response.status_code == 502 and response.json()["code"] == "upstream_unavailable"


async def test_route_is_scoped_to_review():
    from app.main import app
    from tests.test_route_scopes import api_routes, route_scopes

    [route] = [r for r in api_routes(app) if r.path == URL and "POST" in r.methods]
    assert route_scopes(route) == ["review"]


# --- POST /categories/defaults ------------------------------------------------------------------------------------------

DEFAULTS_URL = "/api/v1/categories/defaults"
EXPECTED = [
    "Mercado", "Comida", "Transporte", "Servicios", "Suscripciones", "Arriendo", "Salud", "Ingresos", "Compras",
    "Entretenimiento", "Educación", "Hogar", "Viajes", "Cuidado personal", "Mascotas", "Comisiones e impuestos",
    "Regalos y donaciones",
]


@pytest.fixture
def empty_firefly(firefly):
    """Firefly without categories; ``stored`` is reachable through the mock's listing."""
    stored = []

    async def create(name):
        stored.append(name)
        return name

    firefly.categories.side_effect = lambda: list(stored)
    firefly.create_category.side_effect = create
    firefly.stored = stored
    return firefly


async def test_defaults_create_all_on_empty_firefly_then_nothing(client, empty_firefly, db):
    first = await client.post(DEFAULTS_URL, headers=ADMIN)
    assert first.status_code == 200, first.text
    assert first.json() == {"created": EXPECTED, "existing": []}
    assert empty_firefly.stored == EXPECTED
    [entry] = (await db.scalars(select(Audit).where(Audit.action == "category.defaults"))).all()
    assert entry.detail == {"created": EXPECTED}
    second = await client.post(DEFAULTS_URL, headers=ADMIN)
    assert second.status_code == 200
    assert second.json() == {"created": [], "existing": EXPECTED}
    assert empty_firefly.create_category.await_count == len(EXPECTED)
    assert len((await db.scalars(select(Audit).where(Audit.action == "category.defaults"))).all()) == 1


async def test_defaults_keep_existing_categories_in_other_spelling(client, empty_firefly):
    empty_firefly.stored.extend(["EDUCACION", "mercado"])
    body = (await client.post(DEFAULTS_URL, headers=ADMIN)).json()
    assert body["existing"] == ["mercado", "EDUCACION"] or body["existing"] == ["mercado", "EDUCACION"][::-1]
    assert sorted(body["existing"]) == ["EDUCACION", "mercado"]
    assert "Mercado" not in body["created"] and "Educación" not in body["created"]
    assert len(body["created"]) == len(EXPECTED) - 2
    assert empty_firefly.stored.count("mercado") == 1 and "Mercado" not in empty_firefly.stored


async def test_defaults_failure_midway_keeps_created_and_retry_finishes(client, empty_firefly, db):
    original = empty_firefly.create_category.side_effect

    async def fail_on_salud(name):
        if name == "Salud":
            raise FireflyError("Firefly network request failed")
        return await original(name)

    empty_firefly.create_category.side_effect = fail_on_salud
    response = await client.post(DEFAULTS_URL, headers=ADMIN)
    assert response.status_code == 502 and response.json()["code"] == "upstream_unavailable"
    assert empty_firefly.stored == EXPECTED[:6]
    [entry] = (await db.scalars(select(Audit).where(Audit.action == "category.defaults"))).all()
    assert entry.detail == {"created": EXPECTED[:6]}
    empty_firefly.create_category.side_effect = original
    retry = (await client.post(DEFAULTS_URL, headers=ADMIN)).json()
    assert retry["created"] == EXPECTED[6:] and retry["existing"] == EXPECTED[:6]


async def test_defaults_need_review_scope(client, empty_firefly, db):
    headers = await device_headers(db, "viewer", ["read"])
    assert (await client.post(DEFAULTS_URL, headers=headers)).status_code == 403
    empty_firefly.create_category.assert_not_awaited()
    from app.main import app
    from tests.test_route_scopes import api_routes, route_scopes

    [route] = [r for r in api_routes(app) if r.path == DEFAULTS_URL and "POST" in r.methods]
    assert route_scopes(route) == ["review"]
