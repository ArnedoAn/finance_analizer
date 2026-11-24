"""The client-visible failure of the assistant services (batches, proposals and their actions)."""


class AssistantError(Exception):
    """A client-visible failure; the router maps it to ``{"detail", "code"}`` with ``status``."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail
