"""Input contracts independent of external clients."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.timezone import BOGOTA, parse_datetime
from app.models.schemas import ManualTransaction, TransactionAnalysis, WebhookPayload


def test_epoch_ms_preserves_bogota_day():
    local = datetime(2026, 11, 15, 20, 0, tzinfo=BOGOTA)
    parsed = parse_datetime(str(int(local.timestamp() * 1000)))
    assert parsed.astimezone(BOGOTA) == local
    assert parsed.astimezone(UTC).day == 16


def test_naive_phone_date_is_local_aware():
    assert parse_datetime("2026-11-15 20:00") == datetime(2026, 11, 15, 20, 0, tzinfo=BOGOTA)


def test_manual_positive_decimal():
    value = ManualTransaction(amount="25000.50", description="Almuerzo")
    assert value.amount == Decimal("25000.50")
    assert value.type == "withdrawal"
    with pytest.raises(ValidationError):
        ManualTransaction(amount=0, description="Zero")


def test_webhook_ignores_removed_identity_fields():
    payload = WebhookPayload(message="Compra", user_id="not-an-owner")
    assert "user_id" not in payload.model_dump()
    with pytest.raises(ValidationError):
        WebhookPayload(message="")


def test_llm_output_requires_real_positive_amount():
    with pytest.raises(ValidationError):
        TransactionAnalysis(is_transaction=True)
    assert not TransactionAnalysis(is_transaction=False).is_transaction
