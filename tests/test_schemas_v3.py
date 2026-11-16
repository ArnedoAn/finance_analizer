"""Input contracts independent of external clients."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.schemas import ManualTransaction, WebhookPayload


def test_manual_positive_decimal():
    value = ManualTransaction(client_ref="unique-1", amount="25000.50", description="Almuerzo")
    assert value.amount == Decimal("25000.50")
    assert value.type == "withdrawal"
    with pytest.raises(ValidationError):
        ManualTransaction(client_ref="unique-2", amount=0, description="Zero")


def test_webhook_ignores_removed_identity_fields():
    payload = WebhookPayload(message="Compra", user_id="not-an-owner")
    assert "user_id" not in payload.model_dump()
    with pytest.raises(ValidationError):
        WebhookPayload(message="")


@pytest.mark.parametrize("client_ref", [None, "", "x" * 256])
def test_manual_requires_bounded_client_reference(client_ref):
    with pytest.raises(ValidationError):
        ManualTransaction(client_ref=client_ref, amount=100, description="Cafe")


@pytest.mark.parametrize("value", ["yesterday", "2026-02-30", "2026-13-01T10:00:00Z"])
def test_manual_rejects_invalid_dates(value):
    with pytest.raises(ValidationError):
        ManualTransaction(client_ref="date", amount=100, description="Cafe", date=value)


def test_phone_outbox_identity_fields_are_preserved():
    from uuid import UUID
    event_id = UUID("075f3ecf-bfcb-40cb-9d1c-024327ed9608")
    payload = WebhookPayload(
        event_id=str(event_id), notification_key="0|com.nequi.MobileApp|7",
        post_time=1794841200000, message="Compra",
    )
    assert payload.event_id == event_id
    assert payload.notification_key == "0|com.nequi.MobileApp|7"
    assert payload.post_time == 1794841200000


def test_phone_rejects_non_uuid_identity():
    with pytest.raises(ValidationError):
        WebhookPayload(event_id="not-a-uuid", message="Compra")


def test_retry_horizon_replaces_attempt_limit(settings):
    assert settings.fail_after_hours == 72
    assert "max_attempts" not in type(settings).model_fields
    assert "account_map" not in type(settings).model_fields
