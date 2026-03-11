"""
Custom Exception Classes

Defines application-specific exceptions for proper error handling
and HTTP response mapping.
"""

from typing import Any


class FinanceAnalyzerError(Exception):
    """Base exception for all application errors."""
    
    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        original_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.original_error = original_error
    
    def to_dict(self) -> dict[str, Any]:
        """Convert exception to dictionary for API responses."""
        return {
            "error": self.__class__.__name__,
            "message": self.message,
            "details": self.details,
        }


# =============================================================================
# Gmail Exceptions
# =============================================================================

class GmailError(FinanceAnalyzerError):
    """Base exception for Gmail-related errors."""
    pass


class GmailAuthenticationError(GmailError):
    """Raised when Gmail OAuth authentication fails."""
    pass


class GmailFetchError(GmailError):
    """Raised when fetching emails fails."""
    pass


class GmailParseError(GmailError):
    """Raised when parsing email content fails."""
    pass


# =============================================================================
# DeepSeek Exceptions
# =============================================================================

class DeepSeekError(FinanceAnalyzerError):
    """Base exception for DeepSeek AI errors."""
    pass


class DeepSeekAPIError(DeepSeekError):
    """Raised when DeepSeek API call fails."""
    pass


class DeepSeekParseError(DeepSeekError):
    """Raised when parsing AI response fails."""
    pass


class DeepSeekRateLimitError(DeepSeekError):
    """Raised when rate limited by DeepSeek API."""
    pass


# =============================================================================
# Firefly III Exceptions
# =============================================================================

class FireflyError(FinanceAnalyzerError):
    """Base exception for Firefly III errors."""
    pass


class FireflyAuthenticationError(FireflyError):
    """Raised when Firefly API authentication fails."""
    pass


class FireflyAPIError(FireflyError):
    """Raised when Firefly API call fails."""
    pass


class FireflyValidationError(FireflyError):
    """Raised when Firefly rejects data due to validation."""
    pass


class FireflyDuplicateError(FireflyError):
    """Raised when attempting to create a duplicate entry."""
    pass


class FireflyNotFoundError(FireflyError):
    """Raised when a requested resource is not found."""
    pass


# =============================================================================
# Processing Exceptions
# =============================================================================

class ProcessingError(FinanceAnalyzerError):
    """Base exception for transaction processing errors."""
    pass


class DuplicateEmailError(ProcessingError):
    """Raised when an email has already been processed."""
    pass


class ValidationError(ProcessingError):
    """Raised when data validation fails."""
    pass


class TransactionCreationError(ProcessingError):
    """Raised when transaction creation fails."""
    pass


# =============================================================================
# Notification Exceptions
# =============================================================================

class NotificationError(FinanceAnalyzerError):
    """Base exception for notification/webhook errors."""
    pass


class DuplicateNotificationError(NotificationError):
    """Raised when a notification has already been processed."""
    pass


class NotificationFilteredError(NotificationError):
    """Raised when a notification is filtered out (not financial)."""
    pass


# =============================================================================
# Database Exceptions
# =============================================================================

class DatabaseError(FinanceAnalyzerError):
    """Base exception for database errors."""
    pass


class RecordNotFoundError(DatabaseError):
    """Raised when a database record is not found."""
    pass


class RecordExistsError(DatabaseError):
    """Raised when attempting to create a duplicate record."""
    pass
