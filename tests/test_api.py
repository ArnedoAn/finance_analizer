"""Offline API boundary checks for the single-owner contract."""

import pytest


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "incorrect"}])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/inbox", None),
        ("GET", "/api/v1/processing/statistics", None),
        ("GET", "/api/v1/mail/accounts", None),
        ("GET", "/api/v1/senders", None),
        ("POST", "/api/v1/notifications/webhook", {"message": "Compra"}),
        ("POST", "/api/v1/transactions/manual", {"amount": 100, "description": "Cafe"}),
    ],
)
async def test_api_requires_owner_key(client, headers, method, path, body):
    response = await client.request(method, path, headers=headers, json=body)
    assert response.status_code == 401


async def test_liveness_is_public(client):
    response = await client.get("/api/v1/health/live")
    assert response.status_code == 200


async def test_inbox_with_key_uses_isolated_database(client):
    response = await client.get("/api/v1/inbox", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_invalid_webhook_is_rejected(client):
    response = await client.post(
        "/api/v1/notifications/webhook", headers={"X-API-Key": "test-key"}, json={}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("amount", [0, -1])
async def test_manual_amount_must_be_positive(client, amount):
    response = await client.post(
        "/api/v1/transactions/manual",
        headers={"X-API-Key": "test-key"},
        json={"amount": amount, "description": "Cafe"},
    )
    assert response.status_code == 422
