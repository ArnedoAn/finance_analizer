"""
Tests for notification webhook processing.

Covers:
- NotificationPayload schema validation and hashing
- ProcessedNotificationRepository idempotency
- TransactionFingerprintRepository cross-channel dedup
- NotificationProcessorService flow (with mocked DeepSeek/Firefly)
"""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import (
    ProcessedNotificationRepository,
    TransactionFingerprintRepository,
)
from app.models.schemas import (
    NotificationPayload,
    NotificationProcessingResult,
    NotificationWebhookResponse,
    ProcessingStatus,
    TransactionAnalysis,
    TransactionType,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def sample_notification_data() -> dict:
    """Sample notification payload from Nequi."""
    return {
        "id": "Nequi-2024-01-15",
        "type": "notification",
        "app": "com.nequi.MobileApp",
        "sender": "Nequi",
        "message": "Compra aprobada por $45.000 en Rappi. 15/01/2024 14:30",
        "title": "Nequi",
        "text": "Compra aprobada por $45.000 en Rappi",
        "timestamp": "2024-01-15 14:30:00",
        "date": "2024-01-15",
        "category": "finance",
        "device": "Samsung Galaxy S24",
    }


@pytest.fixture
def sample_uala_notification_data() -> dict:
    """Sample notification payload from Ualá."""
    return {
        "id": "Uala-2024-02-10",
        "type": "notification",
        "app": "ar.com.bancar.uala",
        "sender": "Ualá",
        "message": "Compraste en Mercado Libre por $15.500 ARS",
        "title": "Ualá",
        "text": "Compraste en Mercado Libre por $15.500 ARS",
        "timestamp": "2024-02-10 18:00:00",
        "date": "2024-02-10",
        "category": "finance",
        "device": "iPhone 15",
    }


@pytest.fixture
def sample_non_financial_notification() -> dict:
    """A non-financial notification (WhatsApp)."""
    return {
        "id": "WhatsApp-2024-01-15",
        "type": "notification",
        "app": "com.whatsapp",
        "sender": "John",
        "message": "Hola, cómo estás?",
        "title": "WhatsApp",
        "text": "Hola, cómo estás?",
        "timestamp": "2024-01-15 10:00:00",
        "date": "2024-01-15",
        "category": "messaging",
        "device": "Samsung Galaxy S24",
    }


# =============================================================================
# NotificationPayload Schema Tests
# =============================================================================

class TestNotificationPayload:
    """Tests for NotificationPayload schema."""
    
    def test_create_payload(self, sample_notification_data: dict) -> None:
        payload = NotificationPayload(**sample_notification_data)
        assert payload.app == "com.nequi.MobileApp"
        assert payload.sender == "Nequi"
        assert payload.title == "Nequi"
        assert "Rappi" in payload.message
    
    def test_notification_hash_deterministic(self, sample_notification_data: dict) -> None:
        p1 = NotificationPayload(**sample_notification_data)
        p2 = NotificationPayload(**sample_notification_data)
        assert p1.notification_hash == p2.notification_hash
    
    def test_notification_hash_unique(self, sample_notification_data: dict) -> None:
        p1 = NotificationPayload(**sample_notification_data)
        data2 = {**sample_notification_data, "message": "Different message"}
        p2 = NotificationPayload(**data2)
        assert p1.notification_hash != p2.notification_hash
    
    def test_content_property_prefers_message(self, sample_notification_data: dict) -> None:
        payload = NotificationPayload(**sample_notification_data)
        assert payload.content == payload.message
    
    def test_content_property_falls_back_to_text(self) -> None:
        payload = NotificationPayload(
            id="test", app="test.app", message="", text="fallback text"
        )
        assert payload.content == "fallback text"
    
    def test_frozen_model(self, sample_notification_data: dict) -> None:
        payload = NotificationPayload(**sample_notification_data)
        with pytest.raises(Exception):
            payload.message = "changed"
    
    def test_user_id_and_aliases(self, sample_notification_data: dict) -> None:
        data = {**sample_notification_data, "user_id": " 12345 "}
        p = NotificationPayload(**data)
        assert p.user_id == "12345"
        p2 = NotificationPayload(**{**sample_notification_data, "userId": "999"})
        assert p2.user_id == "999"
    
    def test_source_channel_alias(self, sample_notification_data: dict) -> None:
        p = NotificationPayload(
            **{**sample_notification_data, "sourceChannel": "sms"}
        )
        assert p.source_channel == "sms"


# =============================================================================
# ProcessedNotificationRepository Tests
# =============================================================================

class TestProcessedNotificationRepository:
    """Tests for notification idempotency."""
    
    async def test_mark_processed(self, db_session: AsyncSession) -> None:
        repo = ProcessedNotificationRepository(db_session)
        record = await repo.mark_processed(
            notification_hash="abc123hash",
            source_app="com.nequi.MobileApp",
            sender="Nequi",
            title="Nequi",
            notification_date=datetime(2024, 1, 15, 14, 30),
        )
        assert record.id is not None
        assert record.notification_hash == "abc123hash"
    
    async def test_exists(self, db_session: AsyncSession) -> None:
        repo = ProcessedNotificationRepository(db_session)
        assert await repo.exists("nonexistent") is False
        
        await repo.mark_processed(
            notification_hash="exists_test",
            source_app="com.nequi.MobileApp",
            sender="Nequi",
            title="Test",
            notification_date=datetime(2024, 1, 15),
        )
        assert await repo.exists("exists_test") is True
    
    async def test_get_recent(self, db_session: AsyncSession) -> None:
        repo = ProcessedNotificationRepository(db_session)
        
        for i in range(3):
            await repo.mark_processed(
                notification_hash=f"hash_{i}",
                source_app="com.nequi.MobileApp",
                sender="Nequi",
                title=f"Test {i}",
                notification_date=datetime(2024, 1, 15),
            )
        
        records = await repo.get_recent(limit=2)
        assert len(records) == 2
    
    async def test_get_count(self, db_session: AsyncSession) -> None:
        repo = ProcessedNotificationRepository(db_session)
        
        await repo.mark_processed(
            notification_hash="count_1",
            source_app="com.nequi.MobileApp",
            sender="Nequi",
            title="Test",
            notification_date=datetime(2024, 1, 15),
        )
        await repo.mark_processed(
            notification_hash="count_2",
            source_app="ar.com.bancar.uala",
            sender="Ualá",
            title="Test",
            notification_date=datetime(2024, 1, 15),
        )
        
        total = await repo.get_count()
        assert total == 2
        
        nequi_count = await repo.get_count(source_app="com.nequi.MobileApp")
        assert nequi_count == 1
    
    async def test_notification_isolation_by_session(self, db_session: AsyncSession) -> None:
        repo_a = ProcessedNotificationRepository(db_session, session_id="notifA11")
        repo_b = ProcessedNotificationRepository(db_session, session_id="notifB11")
        
        await repo_a.mark_processed(
            notification_hash="hash-shared",
            source_app="com.nequi.MobileApp",
            sender="Nequi",
            title="Title",
            notification_date=datetime(2024, 1, 15),
        )
        
        assert await repo_a.exists("hash-shared") is True
        assert await repo_b.exists("hash-shared") is False


# =============================================================================
# TransactionFingerprintRepository Tests
# =============================================================================

class TestTransactionFingerprintRepository:
    """Tests for cross-channel deduplication."""
    
    def test_compute_hash_deterministic(self) -> None:
        h1 = TransactionFingerprintRepository.compute_hash(
            amount="45000", transaction_date=datetime(2024, 1, 15), account_name="Nequi"
        )
        h2 = TransactionFingerprintRepository.compute_hash(
            amount="45000", transaction_date=datetime(2024, 1, 15), account_name="Nequi"
        )
        assert h1 == h2
    
    def test_compute_hash_case_insensitive_account(self) -> None:
        h1 = TransactionFingerprintRepository.compute_hash(
            amount="45000", transaction_date=datetime(2024, 1, 15), account_name="Nequi"
        )
        h2 = TransactionFingerprintRepository.compute_hash(
            amount="45000", transaction_date=datetime(2024, 1, 15), account_name="nequi"
        )
        assert h1 == h2
    
    def test_compute_hash_different_amounts(self) -> None:
        h1 = TransactionFingerprintRepository.compute_hash(
            amount="45000", transaction_date=datetime(2024, 1, 15), account_name="Nequi"
        )
        h2 = TransactionFingerprintRepository.compute_hash(
            amount="50000", transaction_date=datetime(2024, 1, 15), account_name="Nequi"
        )
        assert h1 != h2
    
    def test_compute_hash_ignores_time(self) -> None:
        """Same day, different times should produce same hash."""
        h1 = TransactionFingerprintRepository.compute_hash(
            amount="45000", transaction_date=datetime(2024, 1, 15, 10, 0), account_name="Nequi"
        )
        h2 = TransactionFingerprintRepository.compute_hash(
            amount="45000", transaction_date=datetime(2024, 1, 15, 18, 30), account_name="Nequi"
        )
        assert h1 == h2
    
    async def test_create_fingerprint(self, db_session: AsyncSession) -> None:
        repo = TransactionFingerprintRepository(db_session)
        record = await repo.create(
            fingerprint_hash="fp_hash_1",
            amount="45000",
            transaction_date=datetime(2024, 1, 15, 14, 30),
            source_channel="email",
            source_id="email:123",
            description="Compra en Rappi",
            firefly_transaction_id="tx_456",
        )
        assert record.id is not None
        assert record.source_channel == "email"
    
    async def test_find_duplicate_within_window(self, db_session: AsyncSession) -> None:
        repo = TransactionFingerprintRepository(db_session)
        
        # Create fingerprint from email
        await repo.create(
            fingerprint_hash="fp_dup_test",
            amount="45000",
            transaction_date=datetime(2024, 1, 15, 14, 30),
            source_channel="email",
            source_id="email:123",
        )
        
        # Check from notification (1 hour later)
        dup = await repo.find_duplicate(
            fingerprint_hash="fp_dup_test",
            transaction_date=datetime(2024, 1, 15, 15, 30),
            window_hours=2,
        )
        assert dup is not None
        assert dup.source_channel == "email"
    
    async def test_no_duplicate_outside_window(self, db_session: AsyncSession) -> None:
        repo = TransactionFingerprintRepository(db_session)
        
        await repo.create(
            fingerprint_hash="fp_window_test",
            amount="45000",
            transaction_date=datetime(2024, 1, 15, 10, 0),
            source_channel="email",
            source_id="email:456",
        )
        
        # Check 5 hours later (outside 2h window)
        dup = await repo.find_duplicate(
            fingerprint_hash="fp_window_test",
            transaction_date=datetime(2024, 1, 15, 15, 0),
            window_hours=2,
        )
        assert dup is None
    
    async def test_no_duplicate_different_hash(self, db_session: AsyncSession) -> None:
        repo = TransactionFingerprintRepository(db_session)
        
        await repo.create(
            fingerprint_hash="fp_hash_A",
            amount="45000",
            transaction_date=datetime(2024, 1, 15, 14, 0),
            source_channel="email",
            source_id="email:789",
        )
        
        dup = await repo.find_duplicate(
            fingerprint_hash="fp_hash_B",
            transaction_date=datetime(2024, 1, 15, 14, 30),
            window_hours=2,
        )
        assert dup is None
    
    async def test_fingerprint_isolation_by_session(self, db_session: AsyncSession) -> None:
        repo_a = TransactionFingerprintRepository(db_session, session_id="fpSessA1")
        repo_b = TransactionFingerprintRepository(db_session, session_id="fpSessB1")
        
        await repo_a.create(
            fingerprint_hash="fp_iso_hash",
            amount="10000",
            transaction_date=datetime(2024, 1, 15, 10, 0),
            source_channel="email",
            source_id="email:iso",
        )
        
        found_a = await repo_a.find_duplicate(
            fingerprint_hash="fp_iso_hash",
            transaction_date=datetime(2024, 1, 15, 10, 10),
            window_hours=1,
        )
        found_b = await repo_b.find_duplicate(
            fingerprint_hash="fp_iso_hash",
            transaction_date=datetime(2024, 1, 15, 10, 10),
            window_hours=1,
        )
        assert found_a is not None
        assert found_b is None


# =============================================================================
# NotificationWebhookResponse / NotificationProcessingResult Tests
# =============================================================================

class TestNotificationResponseModels:
    
    def test_webhook_response(self) -> None:
        resp = NotificationWebhookResponse(
            accepted=True,
            notification_hash="abc123",
            message="OK",
        )
        assert resp.accepted is True
    
    def test_processing_result_success(self) -> None:
        result = NotificationProcessingResult(
            notification_hash="abc123",
            source_app="com.nequi.MobileApp",
            status=ProcessingStatus.CREATED,
            transaction_id="tx_1",
        )
        assert result.is_success is True
    
    def test_processing_result_skipped(self) -> None:
        result = NotificationProcessingResult(
            notification_hash="abc123",
            source_app="com.nequi.MobileApp",
            status=ProcessingStatus.SKIPPED,
            error_message="Duplicate",
        )
        assert result.is_success is False
