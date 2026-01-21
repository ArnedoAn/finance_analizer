"""
Tests for Pydantic schemas.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.models.schemas import (
    EmailMessage,
    TransactionAnalysis,
    TransactionType,
    ProcessingStatus,
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
