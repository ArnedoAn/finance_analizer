"""
Tests for transaction creation flow.

Tests the complete flow from analysis to Firefly III transaction creation
without using the AI, using mock data instead.
"""

import pytest
import pytest_asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import (
    TransactionAnalysis,
    TransactionCreate,
    TransactionSplit,
    TransactionType,
    AccountType,
)
from app.services.transaction_service import TransactionService
from app.services.sync_service import SyncService
from app.core.exceptions import (
    FireflyDuplicateError,
    FireflyValidationError,
    TransactionCreationError,
)


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def mock_firefly_client() -> AsyncMock:
    """Create mock Firefly client."""
    client = AsyncMock()
    
    # Mock account operations
    client.get_accounts.return_value = [
        {"id": "1", "name": "Cuenta Principal", "type": "asset", "currency_code": "COP"},
        {"id": "2", "name": "Gastos Generales", "type": "expense", "currency_code": "COP"},
    ]
    
    client.get_account_by_name.return_value = None  # Account not found by default
    
    client.create_account.return_value = {
        "id": "10",
        "name": "New Account",
        "type": "expense",
    }
    
    client.get_or_create_account.return_value = {
        "id": "1",
        "name": "Cuenta Principal",
        "type": "asset",
    }
    
    # Mock category operations
    client.get_categories.return_value = [
        {"id": "1", "name": "Compras"},
        {"id": "2", "name": "Servicios"},
    ]
    
    client.get_or_create_category.return_value = {
        "id": "1",
        "name": "Compras",
    }
    
    # Mock transaction operations
    client.create_transaction.return_value = {
        "id": "100",
        "transaction_journal_id": "100",
        "type": "withdrawal",
        "date": "2024-01-15",
        "amount": "45990",
        "description": "Test transaction",
        "source_name": "Cuenta Principal",
        "destination_name": "Amazon",
    }
    
    return client


@pytest.fixture
def sample_withdrawal_analysis() -> TransactionAnalysis:
    """Sample withdrawal (expense) analysis."""
    return TransactionAnalysis(
        amount=Decimal("45990"),
        currency="COP",
        date=datetime(2024, 1, 15),
        description="Compra en Amazon Colombia",
        merchant="Amazon",
        suggested_category="Compras",
        suggested_account_name="Cuenta Principal",
        transaction_type=TransactionType.WITHDRAWAL,
        confidence_score=0.95,
    )


@pytest.fixture
def sample_deposit_analysis() -> TransactionAnalysis:
    """Sample deposit (income) analysis."""
    return TransactionAnalysis(
        amount=Decimal("5000000"),
        currency="COP",
        date=datetime(2024, 1, 15),
        description="Pago de nómina",
        merchant="Empresa XYZ",
        suggested_category="Salario",
        suggested_account_name="Cuenta Principal",
        transaction_type=TransactionType.DEPOSIT,
        confidence_score=0.90,
    )


@pytest.fixture
def sample_transfer_analysis() -> TransactionAnalysis:
    """Sample transfer analysis."""
    return TransactionAnalysis(
        amount=Decimal("100000"),
        currency="COP",
        date=datetime(2024, 1, 15),
        description="Transferencia entre cuentas",
        merchant=None,
        suggested_category=None,
        suggested_account_name="Cuenta Ahorros",
        transaction_type=TransactionType.TRANSFER,
        confidence_score=0.85,
    )


# =============================================================================
# TransactionSplit Model Tests
# =============================================================================

class TestTransactionSplit:
    """Tests for TransactionSplit model."""
    
    def test_valid_withdrawal_split(self):
        """Test creating a valid withdrawal split."""
        split = TransactionSplit(
            type=TransactionType.WITHDRAWAL,
            date="2024-01-15",
            amount="45990",
            description="Compra en tienda",
            source_name="Cuenta Principal",
            destination_name="Tienda XYZ",
            currency_code="COP",
        )
        
        assert split.type == TransactionType.WITHDRAWAL
        assert split.amount == "45990"
        assert split.currency_code == "COP"
    
    def test_valid_deposit_split(self):
        """Test creating a valid deposit split."""
        split = TransactionSplit(
            type=TransactionType.DEPOSIT,
            date="2024-01-15",
            amount="5000000",
            description="Ingreso de salario",
            source_name="Empresa",
            destination_name="Cuenta Principal",
            currency_code="COP",
        )
        
        assert split.type == TransactionType.DEPOSIT
        assert split.source_name == "Empresa"
    
    def test_split_with_category(self):
        """Test split with category."""
        split = TransactionSplit(
            type=TransactionType.WITHDRAWAL,
            date="2024-01-15",
            amount="50000",
            description="Mercado",
            source_name="Cuenta",
            destination_name="Supermercado",
            category_name="Alimentación",
            currency_code="COP",
        )
        
        assert split.category_name == "Alimentación"
    
    def test_split_with_external_id(self):
        """Test split with external reference ID."""
        split = TransactionSplit(
            type=TransactionType.WITHDRAWAL,
            date="2024-01-15",
            amount="25000",
            description="Test",
            external_id="email-123-abc",
            currency_code="COP",
        )
        
        assert split.external_id == "email-123-abc"


# =============================================================================
# TransactionCreate Model Tests
# =============================================================================

class TestTransactionCreate:
    """Tests for TransactionCreate model."""
    
    def test_valid_transaction_create(self):
        """Test creating a valid transaction request."""
        tx = TransactionCreate(
            error_if_duplicate_hash=True,
            apply_rules=True,
            fire_webhooks=True,
            transactions=[
                TransactionSplit(
                    type=TransactionType.WITHDRAWAL,
                    date="2024-01-15",
                    amount="100000",
                    description="Test transaction",
                    source_name="Cuenta",
                    destination_name="Tienda",
                    currency_code="COP",
                )
            ],
        )
        
        assert tx.error_if_duplicate_hash is True
        assert len(tx.transactions) == 1
    
    def test_transaction_must_have_splits(self):
        """Test that transaction must have at least one split."""
        with pytest.raises(ValueError):
            TransactionCreate(
                transactions=[],  # Empty list should fail
            )


# =============================================================================
# SyncService Tests
# =============================================================================

class TestSyncService:
    """Tests for SyncService account/category resolution."""
    
    @pytest_asyncio.fixture
    async def sync_service(self, db_session: AsyncSession, mock_firefly_client: AsyncMock) -> SyncService:
        """Create sync service with mocks."""
        return SyncService(db_session, mock_firefly_client)
    
    @pytest.mark.asyncio
    async def test_resolve_source_account_withdrawal(
        self,
        sync_service: SyncService,
        sample_withdrawal_analysis: TransactionAnalysis,
        mock_firefly_client: AsyncMock,
    ):
        """Test source account resolution for withdrawal."""
        mock_firefly_client.get_or_create_account.return_value = {
            "id": "1",
            "name": "Cuenta Principal",
            "type": "asset",
        }
        
        account = await sync_service.resolve_source_account(sample_withdrawal_analysis)
        
        assert account["name"] == "Cuenta Principal"
        assert account["type"] == "asset"
    
    @pytest.mark.asyncio
    async def test_resolve_destination_account_withdrawal(
        self,
        sync_service: SyncService,
        sample_withdrawal_analysis: TransactionAnalysis,
        mock_firefly_client: AsyncMock,
    ):
        """Test destination account resolution for withdrawal (expense)."""
        mock_firefly_client.get_or_create_account.return_value = {
            "id": "10",
            "name": "Amazon",
            "type": "expense",
        }
        
        account = await sync_service.resolve_destination_account(sample_withdrawal_analysis)
        
        # For withdrawal, destination should be expense account (merchant)
        assert account["name"] == "Amazon"
    
    @pytest.mark.asyncio
    async def test_resolve_category(
        self,
        sync_service: SyncService,
        mock_firefly_client: AsyncMock,
    ):
        """Test category resolution."""
        mock_firefly_client.get_or_create_category.return_value = {
            "id": "1",
            "name": "Compras",
        }
        
        category = await sync_service.resolve_category("Compras")
        
        assert category["name"] == "Compras"
    
    @pytest.mark.asyncio
    async def test_resolve_empty_category_uses_default(
        self,
        sync_service: SyncService,
        mock_firefly_client: AsyncMock,
    ):
        """Test that empty category resolves to default."""
        mock_firefly_client.get_or_create_category.return_value = {
            "id": "99",
            "name": "Sin Categoría",
        }
        
        category = await sync_service.resolve_category("")
        
        mock_firefly_client.get_or_create_category.assert_called_with("Sin Categoría")


# =============================================================================
# TransactionService Tests
# =============================================================================

class TestTransactionService:
    """Tests for TransactionService."""
    
    @pytest_asyncio.fixture
    async def transaction_service(
        self,
        db_session: AsyncSession,
        mock_firefly_client: AsyncMock,
    ) -> TransactionService:
        """Create transaction service with mocks."""
        sync_service = SyncService(db_session, mock_firefly_client)
        return TransactionService(db_session, mock_firefly_client, sync_service)
    
    @pytest.mark.asyncio
    async def test_create_withdrawal_dry_run(
        self,
        transaction_service: TransactionService,
        sample_withdrawal_analysis: TransactionAnalysis,
        mock_firefly_client: AsyncMock,
    ):
        """Test creating a withdrawal in dry run mode."""
        mock_firefly_client.get_or_create_account.side_effect = [
            {"id": "1", "name": "Cuenta Principal", "type": "asset"},
            {"id": "10", "name": "Amazon", "type": "expense"},
        ]
        mock_firefly_client.get_or_create_category.return_value = {
            "id": "1", "name": "Compras"
        }
        
        result = await transaction_service.create_from_analysis(
            sample_withdrawal_analysis,
            external_id="test-123",
            dry_run=True,
        )
        
        assert result["dry_run"] is True
        assert result["type"] == "withdrawal"
        assert result["amount"] == "45990"
        assert result["source_name"] == "Cuenta Principal"
        assert result["destination_name"] == "Amazon"
        
        # Should NOT call create_transaction in dry run
        mock_firefly_client.create_transaction.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_create_withdrawal_real(
        self,
        transaction_service: TransactionService,
        sample_withdrawal_analysis: TransactionAnalysis,
        mock_firefly_client: AsyncMock,
    ):
        """Test creating a real withdrawal transaction."""
        mock_firefly_client.get_or_create_account.side_effect = [
            {"id": "1", "name": "Cuenta Principal", "type": "asset"},
            {"id": "10", "name": "Amazon", "type": "expense"},
        ]
        mock_firefly_client.get_or_create_category.return_value = {
            "id": "1", "name": "Compras"
        }
        mock_firefly_client.create_transaction.return_value = {
            "id": "100",
            "transaction_journal_id": "100",
            "type": "withdrawal",
            "date": "2024-01-15",
            "amount": "45990",
            "description": "Compra en Amazon Colombia",
        }
        
        # Patch settings to disable global dry_run
        with patch.object(transaction_service.settings, 'dry_run', False):
            result = await transaction_service.create_from_analysis(
                sample_withdrawal_analysis,
                external_id="test-123",
                dry_run=False,
            )
        
        assert result["id"] == "100"
        mock_firefly_client.create_transaction.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_create_deposit(
        self,
        transaction_service: TransactionService,
        sample_deposit_analysis: TransactionAnalysis,
        mock_firefly_client: AsyncMock,
    ):
        """Test creating a deposit transaction."""
        mock_firefly_client.get_or_create_account.side_effect = [
            {"id": "5", "name": "Empresa XYZ", "type": "revenue"},
            {"id": "1", "name": "Cuenta Principal", "type": "asset"},
        ]
        mock_firefly_client.get_or_create_category.return_value = {
            "id": "10", "name": "Salario"
        }
        
        result = await transaction_service.create_from_analysis(
            sample_deposit_analysis,
            dry_run=True,
        )
        
        assert result["type"] == "deposit"
        assert result["amount"] == "5000000"
    
    @pytest.mark.asyncio
    async def test_duplicate_transaction_raises_error(
        self,
        transaction_service: TransactionService,
        sample_withdrawal_analysis: TransactionAnalysis,
        mock_firefly_client: AsyncMock,
    ):
        """Test that duplicate transaction raises FireflyDuplicateError."""
        mock_firefly_client.get_or_create_account.side_effect = [
            {"id": "1", "name": "Cuenta Principal", "type": "asset"},
            {"id": "10", "name": "Amazon", "type": "expense"},
        ]
        mock_firefly_client.get_or_create_category.return_value = {
            "id": "1", "name": "Compras"
        }
        mock_firefly_client.create_transaction.side_effect = FireflyDuplicateError(
            "Duplicate transaction detected"
        )
        
        with patch.object(transaction_service.settings, 'dry_run', False):
            with pytest.raises(FireflyDuplicateError):
                await transaction_service.create_from_analysis(
                    sample_withdrawal_analysis,
                    dry_run=False,
                )
    
    @pytest.mark.asyncio
    async def test_validation_error_raises_transaction_creation_error(
        self,
        transaction_service: TransactionService,
        sample_withdrawal_analysis: TransactionAnalysis,
        mock_firefly_client: AsyncMock,
    ):
        """Test that validation errors are properly wrapped."""
        mock_firefly_client.get_or_create_account.side_effect = [
            {"id": "1", "name": "Cuenta Principal", "type": "asset"},
            {"id": "10", "name": "Amazon", "type": "expense"},
        ]
        mock_firefly_client.get_or_create_category.return_value = {
            "id": "1", "name": "Compras"
        }
        mock_firefly_client.create_transaction.side_effect = FireflyValidationError(
            "Validation error: /accounts",
            details={"errors": [{"field": "accounts", "message": "Invalid"}]},
        )
        
        with patch.object(transaction_service.settings, 'dry_run', False):
            with pytest.raises(TransactionCreationError) as exc_info:
                await transaction_service.create_from_analysis(
                    sample_withdrawal_analysis,
                    dry_run=False,
                )
        
        assert "Failed to create transaction" in str(exc_info.value)


# =============================================================================
# Integration-style Tests (with mock Firefly)
# =============================================================================

class TestTransactionFlowIntegration:
    """Integration tests for the full transaction flow."""
    
    @pytest.mark.asyncio
    async def test_full_withdrawal_flow(
        self,
        db_session: AsyncSession,
        mock_firefly_client: AsyncMock,
    ):
        """Test complete withdrawal flow from analysis to transaction."""
        # Setup mocks
        mock_firefly_client.get_or_create_account.side_effect = [
            {"id": "1", "name": "Nequi", "type": "asset"},
            {"id": "20", "name": "Rappi", "type": "expense"},
        ]
        mock_firefly_client.get_or_create_category.return_value = {
            "id": "5", "name": "Delivery"
        }
        
        # Create services
        sync_service = SyncService(db_session, mock_firefly_client)
        tx_service = TransactionService(db_session, mock_firefly_client, sync_service)
        
        # Create analysis (as if from DeepSeek)
        analysis = TransactionAnalysis(
            amount=Decimal("35000"),
            currency="COP",
            date=datetime(2024, 1, 20),
            description="Pedido Rappi - Hamburguesas",
            merchant="Rappi",
            suggested_category="Delivery",
            suggested_account_name="Nequi",
            transaction_type=TransactionType.WITHDRAWAL,
            confidence_score=0.92,
        )
        
        # Create transaction (dry run)
        result = await tx_service.create_from_analysis(
            analysis,
            external_id="email-rappi-123",
            dry_run=True,
        )
        
        assert result["dry_run"] is True
        assert result["amount"] == "35000"
        assert result["description"] == "Pedido Rappi - Hamburguesas"
        assert result["source_name"] == "Nequi"
        assert result["destination_name"] == "Rappi"
        assert result["category_name"] == "Delivery"
    
    @pytest.mark.asyncio
    async def test_colombian_bank_transaction(
        self,
        db_session: AsyncSession,
        mock_firefly_client: AsyncMock,
    ):
        """Test transaction from Colombian bank email."""
        mock_firefly_client.get_or_create_account.side_effect = [
            {"id": "1", "name": "Bancolombia Ahorros", "type": "asset"},
            {"id": "30", "name": "Netflix", "type": "expense"},
        ]
        mock_firefly_client.get_or_create_category.return_value = {
            "id": "8", "name": "Suscripciones"
        }
        
        sync_service = SyncService(db_session, mock_firefly_client)
        tx_service = TransactionService(db_session, mock_firefly_client, sync_service)
        
        # Simulate Bancolombia transaction analysis
        analysis = TransactionAnalysis(
            amount=Decimal("39900"),
            currency="COP",
            date=datetime(2024, 1, 18),
            description="Pago Netflix suscripción mensual",
            merchant="Netflix",
            suggested_category="Suscripciones",
            suggested_account_name="Bancolombia Ahorros",
            transaction_type=TransactionType.WITHDRAWAL,
            confidence_score=0.98,
        )
        
        result = await tx_service.create_from_analysis(analysis, dry_run=True)
        
        assert result["dry_run"] is True
        assert result["source_name"] == "Bancolombia Ahorros"
        assert result["destination_name"] == "Netflix"
        assert result["amount"] == "39900"
    
    @pytest.mark.asyncio
    async def test_nequi_incoming_transfer(
        self,
        db_session: AsyncSession,
        mock_firefly_client: AsyncMock,
    ):
        """Test incoming Nequi transfer (deposit)."""
        mock_firefly_client.get_or_create_account.side_effect = [
            {"id": "50", "name": "Juan Pérez", "type": "revenue"},
            {"id": "2", "name": "Nequi", "type": "asset"},
        ]
        mock_firefly_client.get_or_create_category.return_value = {
            "id": "15", "name": "Transferencias"
        }
        
        sync_service = SyncService(db_session, mock_firefly_client)
        tx_service = TransactionService(db_session, mock_firefly_client, sync_service)
        
        analysis = TransactionAnalysis(
            amount=Decimal("150000"),
            currency="COP",
            date=datetime(2024, 1, 19),
            description="Transferencia recibida de Juan Pérez",
            merchant="Juan Pérez",
            suggested_category="Transferencias",
            suggested_account_name="Nequi",
            transaction_type=TransactionType.DEPOSIT,
            confidence_score=0.88,
        )
        
        result = await tx_service.create_from_analysis(analysis, dry_run=True)
        
        assert result["type"] == "deposit"
        assert result["amount"] == "150000"


# =============================================================================
# Edge Cases Tests
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases and error handling."""
    
    def test_analysis_with_zero_amount(self):
        """Test that zero amount is rejected."""
        with pytest.raises(ValueError):
            TransactionAnalysis(
                amount=Decimal("0"),
                currency="COP",
                date=datetime.now(),
                description="Invalid transaction",
                transaction_type=TransactionType.WITHDRAWAL,
                confidence_score=0.5,
            )
    
    def test_analysis_with_negative_amount(self):
        """Test that negative amount is rejected."""
        with pytest.raises(ValueError):
            TransactionAnalysis(
                amount=Decimal("-50000"),
                currency="COP",
                date=datetime.now(),
                description="Invalid transaction",
                transaction_type=TransactionType.WITHDRAWAL,
                confidence_score=0.5,
            )
    
    def test_analysis_with_low_confidence(self):
        """Test analysis with very low confidence score."""
        # Should still be valid, but flagged
        analysis = TransactionAnalysis(
            amount=Decimal("10000"),
            currency="COP",
            date=datetime.now(),
            description="Uncertain transaction",
            transaction_type=TransactionType.WITHDRAWAL,
            confidence_score=0.3,
        )
        
        assert analysis.confidence_score == 0.3
    
    def test_analysis_currency_normalization(self):
        """Test that currency is normalized to uppercase."""
        analysis = TransactionAnalysis(
            amount=Decimal("50000"),
            currency="cop",  # lowercase
            date=datetime.now(),
            description="Test",
            transaction_type=TransactionType.WITHDRAWAL,
            confidence_score=0.8,
        )
        
        assert analysis.currency == "COP"
    
    def test_very_large_amount(self):
        """Test handling of very large amounts (Colombian pesos)."""
        analysis = TransactionAnalysis(
            amount=Decimal("999999999.99"),
            currency="COP",
            date=datetime.now(),
            description="Large purchase",
            transaction_type=TransactionType.WITHDRAWAL,
            confidence_score=0.9,
        )
        
        assert analysis.amount == Decimal("999999999.99")
    
    def test_description_with_special_characters(self):
        """Test description with special characters."""
        analysis = TransactionAnalysis(
            amount=Decimal("25000"),
            currency="COP",
            date=datetime.now(),
            description="Compra en TIENDA #123 - Café & más",
            merchant="TIENDA #123",
            transaction_type=TransactionType.WITHDRAWAL,
            confidence_score=0.85,
        )
        
        assert "#" in analysis.description
        assert "&" in analysis.description
