"""
Tests for Pydantic schemas.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.models.schemas import (
    EmailMessage,
    HealthCheck,
    TransactionAnalysis,
    TransactionType,
    ProcessingStatus,
)
from app.core.session import (
    build_user_session_id,
    build_telegram_session_id,
    create_oauth_state,
    normalize_telegram_id,
    parse_oauth_state,
    resolve_or_create_session_id,
    resolve_webhook_session_id,
)


class TestEmailMessage:
    """Tests for EmailMessage schema."""
    
    def test_create_email_message(self, sample_email_data):
        """Test creating an email message."""
        email = EmailMessage(
            message_id=sample_email_data["message_id"],
            internal_id=sample_email_data["internal_id"],
            thread_id=sample_email_data["thread_id"],
            subject=sample_email_data["subject"],
            sender=sample_email_data["sender"],
            date=datetime.fromisoformat(sample_email_data["date"].replace("Z", "+00:00")),
            body_text=sample_email_data["body_text"],
        )
        
        assert email.message_id == sample_email_data["message_id"]
        assert email.internal_id == sample_email_data["internal_id"]
        assert email.subject == sample_email_data["subject"]
    
    def test_idempotency_key(self, sample_email_data):
        """Test idempotency key generation."""
        email = EmailMessage(
            message_id=sample_email_data["message_id"],
            internal_id=sample_email_data["internal_id"],
            thread_id=sample_email_data["thread_id"],
            sender=sample_email_data["sender"],
            date=datetime.now(),
        )
        
        expected_key = f"{sample_email_data['message_id']}:{sample_email_data['internal_id']}"
        assert email.idempotency_key == expected_key
    
    def test_body_property(self):
        """Test body property fallback."""
        # With text body
        email = EmailMessage(
            message_id="<test@test>",
            internal_id="123",
            thread_id="thread",
            sender="test@test.com",
            date=datetime.now(),
            body_text="Plain text",
            body_html="<p>HTML</p>",
        )
        assert email.body == "Plain text"
        
        # Without text body
        email2 = EmailMessage(
            message_id="<test@test>",
            internal_id="123",
            thread_id="thread",
            sender="test@test.com",
            date=datetime.now(),
            body_text="",
            body_html="<p>HTML</p>",
        )
        assert email2.body == "<p>HTML</p>"


class TestTransactionAnalysis:
    """Tests for TransactionAnalysis schema."""
    
    def test_create_analysis(self, sample_analysis_data):
        """Test creating transaction analysis."""
        analysis = TransactionAnalysis(
            amount=sample_analysis_data["amount"],
            currency=sample_analysis_data["currency"],
            date=datetime.fromisoformat(sample_analysis_data["date"]),
            description=sample_analysis_data["description"],
            merchant=sample_analysis_data["merchant"],
            suggested_category=sample_analysis_data["suggested_category"],
            transaction_type=sample_analysis_data["transaction_type"],
            confidence_score=sample_analysis_data["confidence_score"],
        )
        
        assert analysis.amount == Decimal("45.99")
        assert analysis.currency == "USD"
        assert analysis.merchant == "Amazon"
        assert analysis.transaction_type == TransactionType.WITHDRAWAL
    
    def test_amount_parsing(self):
        """Test amount parsing from various formats."""
        # From string with currency symbol
        analysis = TransactionAnalysis(
            amount="$45.99",
            date=datetime.now(),
            description="Test",
        )
        assert analysis.amount == Decimal("45.99")
        
        # From int
        analysis2 = TransactionAnalysis(
            amount=100,
            date=datetime.now(),
            description="Test",
        )
        assert analysis2.amount == Decimal("100")
    
    def test_currency_normalization(self):
        """Test currency code is normalized to uppercase."""
        analysis = TransactionAnalysis(
            amount=100,
            currency="usd",
            date=datetime.now(),
            description="Test",
        )
        assert analysis.currency == "USD"
    
    def test_default_values(self):
        """Test default values are applied."""
        analysis = TransactionAnalysis(
            amount=100,
            date=datetime.now(),
            description="Test",
        )
        
        assert analysis.currency == "USD"
        assert analysis.transaction_type == TransactionType.WITHDRAWAL
        assert analysis.suggested_category == "Sin Categoría"
        assert analysis.confidence_score == 0.0


class TestProcessingStatus:
    """Tests for ProcessingStatus enum."""
    
    def test_status_values(self):
        """Test all status values exist."""
        assert ProcessingStatus.PENDING.value == "pending"
        assert ProcessingStatus.PROCESSING.value == "processing"
        assert ProcessingStatus.ANALYZED.value == "analyzed"
        assert ProcessingStatus.CREATED.value == "created"
        assert ProcessingStatus.SKIPPED.value == "skipped"
        assert ProcessingStatus.FAILED.value == "failed"
        assert ProcessingStatus.DRY_RUN.value == "dry_run"


class TestSessionUtilities:
    """Tests for session helper utilities."""
    
    def test_resolve_session_from_header(self):
        session_id, is_new = resolve_or_create_session_id(
            header_session_id="abc12345",
            cookie_session_id=None,
        )
        assert session_id == "abc12345"
        assert is_new is False
    
    def test_resolve_session_from_cookie(self):
        session_id, is_new = resolve_or_create_session_id(
            header_session_id=None,
            cookie_session_id="cookie1234",
        )
        assert session_id == "cookie1234"
        assert is_new is False
    
    def test_resolve_session_generates_when_missing(self):
        session_id, is_new = resolve_or_create_session_id(
            header_session_id=None,
            cookie_session_id=None,
        )
        assert is_new is True
        assert isinstance(session_id, str)
        assert len(session_id) > 20

    def test_resolve_session_from_telegram_explicit_header(self):
        session_id, is_new = resolve_or_create_session_id(
            header_session_id=None,
            cookie_session_id=None,
            telegram_session_id="telegram123",
        )
        assert session_id == "telegram123"
        assert is_new is False

    def test_resolve_session_from_telegram_ids(self):
        expected = build_telegram_session_id("123456789", "-100123456")
        session_id, is_new = resolve_or_create_session_id(
            header_session_id="header1234",
            cookie_session_id="cookie1234",
            telegram_user_id="123456789",
            telegram_chat_id="-100123456",
        )
        assert session_id == expected
        assert is_new is False

    def test_resolve_session_from_user_id_header(self):
        expected = build_user_session_id("123456789")
        session_id, is_new = resolve_or_create_session_id(
            header_session_id="header1234",
            cookie_session_id="cookie1234",
            user_id="123456789",
        )
        assert session_id == expected
        assert is_new is False

    def test_resolve_session_prioritizes_telegram_over_user_id(self):
        expected = build_telegram_session_id("123456789", "-100123456")
        session_id, _ = resolve_or_create_session_id(
            header_session_id="header1234",
            cookie_session_id="cookie1234",
            user_id="frontend-user",
            telegram_user_id="123456789",
            telegram_chat_id="-100123456",
        )
        assert session_id == expected

    def test_build_user_session_id(self):
        sid = build_user_session_id("abc-user")
        assert sid is not None
        assert sid.startswith("usr_")
        assert len(sid) == 44

    def test_resolve_webhook_session_id(self):
        assert resolve_webhook_session_id("abc-user", "ignored") == build_user_session_id(
            "abc-user"
        )
        assert resolve_webhook_session_id(None, "my-session-id-12") == "my-session-id-12"
        assert resolve_webhook_session_id("   ", "fallback1234") == "fallback1234"

    def test_build_telegram_session_id_user_only(self):
        sid = build_telegram_session_id("123456789")
        assert sid is not None
        assert sid.startswith("tg_")
        assert len(sid) == 43

    def test_build_telegram_session_id_invalid_user(self):
        assert build_telegram_session_id("invalid-user") is None

    def test_normalize_telegram_id(self):
        assert normalize_telegram_id("123456") == "123456"
        assert normalize_telegram_id("-100123456") == "-100123456"
        assert normalize_telegram_id("abc123") is None
    
    def test_oauth_state_roundtrip(self):
        state = create_oauth_state("session123")
        recovered = parse_oauth_state(state)
        assert recovered == "session123"
    
    def test_oauth_state_tampered_fails(self):
        state = create_oauth_state("session123")
        tampered = state[:-1] + ("A" if state[-1] != "A" else "B")
        
        with pytest.raises(ValueError):
            parse_oauth_state(tampered)


class TestHealthCheckSchema:
    """Tests for health check schema extensions."""
    
    def test_health_check_accepts_session_id(self):
        data = HealthCheck(
            status="healthy",
            version="1.0.0",
            environment="development",
            session_id="abc12345",
            services={"gmail": True},
        )
        assert data.session_id == "abc12345"
