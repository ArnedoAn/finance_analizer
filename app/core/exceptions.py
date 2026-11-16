"""Domain failures safe to handle at the API boundary."""


class FinanceAnalyzerError(Exception):
    pass


class FireflyError(FinanceAnalyzerError):
    pass


class LLMError(FinanceAnalyzerError):
    pass


class IMAPError(FinanceAnalyzerError):
    pass
