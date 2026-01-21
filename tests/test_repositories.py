"""
Tests for database repositories.
"""

from datetime import datetime

import pytest
import pytest_asyncio

from app.db.models import ProcessedEmail, AuditLog
from app.db.repositories import (
    ProcessedEmailRepository,
    AuditLogRepository,
    AccountCacheRepository,
    CategoryCacheRepository,
)
from app.models.schemas import AuditLogCreate, ProcessingStatus


@pytest.mark.asyncio
class TestProcessedEmailRepository:
    """Tests for ProcessedEmailRepository."""
    
    async def test_mark_processed(self, db_session):
        """Test marking an email as processed."""
        repo = ProcessedEmailRepository(db_session)
        
        result = await repo.mark_processed(
            message_id="<test-123@gmail.com>",
            internal_id="abc123",
            email_date=datetime.now(),
        )
        
        assert result.id is not None
        assert result.message_id == "<test-123@gmail.com>"
        assert result.internal_id == "abc123"
    
    async def test_exists(self, db_session):
        """Test checking if email was processed."""
        repo = ProcessedEmailRepository(db_session)
        
        # Should not exist initially
        exists = await repo.exists("<test-456@gmail.com>", "xyz789")
        assert exists is False
        
        # Mark as processed
        await repo.mark_processed(
            message_id="<test-456@gmail.com>",
            internal_id="xyz789",
            email_date=datetime.now(),
        )
        await db_session.commit()
        
        # Should exist now
        exists = await repo.exists("<test-456@gmail.com>", "xyz789")
        assert exists is True
    
    async def test_get_processed_ids(self, db_session):
        """Test getting processed email IDs."""
        repo = ProcessedEmailRepository(db_session)
        
        # Add some processed emails
        await repo.mark_processed("<a@b.com>", "id1", datetime.now())
        await repo.mark_processed("<c@d.com>", "id2", datetime.now())
        await db_session.commit()
        
        ids = await repo.get_processed_ids()
        assert "id1" in ids
        assert "id2" in ids


@pytest.mark.asyncio
class TestAuditLogRepository:
    """Tests for AuditLogRepository."""
    
    async def test_create_audit_log(self, db_session):
        """Test creating audit log entry."""
        repo = AuditLogRepository(db_session)
        
        data = AuditLogCreate(
            email_message_id="<test@test.com>",
            email_internal_id="test123",
            email_subject="Test Subject",
            email_sender="sender@test.com",
            email_date=datetime.now(),
            status=ProcessingStatus.CREATED,
            firefly_transaction_id="tx-123",
            processing_time_ms=500,
        )
        
        result = await repo.create(data)
        
        assert result.id is not None
        assert result.email_message_id == "<test@test.com>"
        assert result.status == "created"
        assert result.firefly_transaction_id == "tx-123"
    
    async def test_get_recent(self, db_session):
        """Test getting recent audit logs."""
        repo = AuditLogRepository(db_session)
        
        # Create some logs
        for i in range(5):
            await repo.create(AuditLogCreate(
                email_message_id=f"<test-{i}@test.com>",
                email_internal_id=f"id{i}",
                email_date=datetime.now(),
                status=ProcessingStatus.CREATED,
            ))
        await db_session.commit()
        
        logs = await repo.get_recent(limit=3)
        assert len(logs) == 3
    
    async def test_get_statistics(self, db_session):
        """Test getting processing statistics."""
        repo = AuditLogRepository(db_session)
        
        # Create logs with different statuses
        await repo.create(AuditLogCreate(
            email_message_id="<a@b.com>",
            email_internal_id="id1",
            email_date=datetime.now(),
            status=ProcessingStatus.CREATED,
        ))
        await repo.create(AuditLogCreate(
            email_message_id="<c@d.com>",
            email_internal_id="id2",
            email_date=datetime.now(),
            status=ProcessingStatus.FAILED,
        ))
        await repo.create(AuditLogCreate(
            email_message_id="<e@f.com>",
            email_internal_id="id3",
            email_date=datetime.now(),
            status=ProcessingStatus.CREATED,
        ))
        await db_session.commit()
        
        stats = await repo.get_statistics()
        assert stats.get("created", 0) == 2
        assert stats.get("failed", 0) == 1


@pytest.mark.asyncio
class TestAccountCacheRepository:
    """Tests for AccountCacheRepository."""
    
    async def test_upsert_account(self, db_session):
        """Test inserting and updating account cache."""
        repo = AccountCacheRepository(db_session)
        
        # Insert
        result = await repo.upsert(
            firefly_id="1",
            name="Test Account",
            account_type="asset",
            currency_code="USD",
        )
        await db_session.commit()
        
        assert result.firefly_id == "1"
        assert result.name == "Test Account"
        
        # Update
        result2 = await repo.upsert(
            firefly_id="1",
            name="Updated Account",
            account_type="asset",
            currency_code="EUR",
        )
        await db_session.commit()
        
        assert result2.name == "Updated Account"
        assert result2.currency_code == "EUR"
    
    async def test_get_by_name(self, db_session):
        """Test getting account by name and type."""
        repo = AccountCacheRepository(db_session)
        
        await repo.upsert("1", "My Bank", "asset", "USD")
        await db_session.commit()
        
        result = await repo.get_by_name("My Bank", "asset")
        assert result is not None
        assert result.name == "My Bank"
        
        # Different type should not match
        result2 = await repo.get_by_name("My Bank", "expense")
        assert result2 is None


@pytest.mark.asyncio
class TestCategoryCacheRepository:
    """Tests for CategoryCacheRepository."""
    
    async def test_upsert_category(self, db_session):
        """Test inserting and updating category cache."""
        repo = CategoryCacheRepository(db_session)
        
        result = await repo.upsert(firefly_id="1", name="Food")
        await db_session.commit()
        
        assert result.firefly_id == "1"
        assert result.name == "Food"
    
    async def test_get_by_name(self, db_session):
        """Test getting category by name."""
        repo = CategoryCacheRepository(db_session)
        
        await repo.upsert("1", "Transportation")
        await db_session.commit()
        
        result = await repo.get_by_name("Transportation")
        assert result is not None
        assert result.name == "Transportation"
        
        result2 = await repo.get_by_name("NonExistent")
        assert result2 is None
