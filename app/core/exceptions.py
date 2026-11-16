"""Domain failures safe to handle at the API boundary."""


class FinanceAnalyzerError(Exception):
    pass


class FireflyError(FinanceAnalyzerError):
    pass


class PermanentError(FireflyError, ValueError):
    """Non-retryable validation failure, safe to route to human review."""


class LLMError(FinanceAnalyzerError):
    pass


class IMAPError(FinanceAnalyzerError):
    pass
