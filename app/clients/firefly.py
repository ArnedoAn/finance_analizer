"""Single-owner Firefly JSON:API client; retries belong to the durable inbox."""

from decimal import Decimal, InvalidOperation

import httpx

from app.core import error_log
from app.core.config import get_settings
from app.core.exceptions import FireflyError, PermanentError


class FireflyClient:
    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()
        base = self.settings.firefly_base_url.rstrip("/")
        if not base.endswith("/api/v1"):
            base += "/api/v1"
        self._client = httpx.AsyncClient(
            base_url=base + "/", timeout=30,
            headers={
                "Authorization": f"Bearer {self.settings.firefly_api_token.get_secret_value()}",
                "Accept": "application/vnd.api+json",
            },
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        operation = f"{method} {path.split('?')[0]}"
        token = self.settings.firefly_api_token.get_secret_value()
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            error_log.record("firefly", operation, "Firefly network request failed", exc=exc, scrub=(token,))
            raise FireflyError("Firefly network request failed") from exc
        if 400 <= response.status_code < 500 and response.status_code != 429:
            try:
                body = response.json()
                message = str(body.get("message", "Validation failed"))
                errors = body.get("errors", {})
                message = f"{message}: {errors}" if errors else message
            except (ValueError, AttributeError):
                message = "Request rejected"
            if token:
                message = message.replace(token, "[redacted]")
            if response.status_code != 404:  # a missing id is an expected answer (``get_transaction``), not a failure
                error_log.record("firefly", operation, f"Firefly {response.status_code}", exc_type="PermanentError", status_code=response.status_code, body=response.text, scrub=(token,))
            raise PermanentError(f"Firefly {response.status_code}: {message[:1500]}")
        if response.is_error:
            error_log.record("firefly", operation, f"Firefly temporarily unavailable ({response.status_code})", exc_type="FireflyError", status_code=response.status_code, body=response.text, scrub=(token,))
            raise FireflyError(f"Firefly temporarily unavailable ({response.status_code})")
        return response.json() if response.content else {}

    async def _list(self, path: str, **params) -> list[dict]:
        rows = []
        page = 1
        while True:
            result = await self._request("GET", path, params={**params, "page": page, "limit": 100})
            data = result["data"]
            rows.extend(data)
            pagination = result.get("meta", {}).get("pagination", {})
            if not data or page >= int(pagination.get("total_pages", 1)):
                return rows
            page += 1

    @staticmethod
    def _account(data: dict) -> dict:
        attrs = data["attributes"]
        return {"id": str(data["id"]), "name": attrs["name"], "type": attrs["type"]}

    async def accounts(self) -> list[dict]:
        return [self._account(row) for row in await self._list("accounts")]

    async def categories(self) -> list[str]:
        return [row["attributes"]["name"] for row in await self._list("categories")]

    async def create_account(self, name: str, type: str) -> dict:
        if type not in {"expense", "revenue"}:
            raise PermanentError("Only merchant expense/revenue accounts may be created")
        result = await self._request(
            "POST", "accounts", json={"name": name, "type": type, "currency_code": "COP"}
        )
        return self._account(result["data"])

    async def create_transaction(self, payload: dict) -> str:
        result = await self._request(
            "POST", "transactions", json={**payload, "apply_rules": True}
        )
        return str(result["data"]["id"])

    async def find_by_external_id(self, external_id: str) -> str | None:
        escaped = external_id.replace("\\", "\\\\").replace('"', '\\"')
        rows = await self._list("search/transactions", query=f'external_id_is:"{escaped}"')
        for row in rows:
            splits = row.get("attributes", {}).get("transactions", [])
            if any(split.get("external_id") == external_id for split in splits):
                return str(row["id"])
        return None

    async def update_transaction(self, transaction_id: str, payload: dict) -> str:
        result = await self._request("PUT", f"transactions/{transaction_id}", json={**payload, "apply_rules": True})
        return str(result["data"]["id"])

    async def delete_transaction(self, transaction_id: str) -> None:
        await self._request("DELETE", f"transactions/{transaction_id}")

    async def get_transaction(self, transaction_id: str) -> dict | None:
        """The transaction group (JSON:API ``data``: ``id`` and ``attributes.transactions``) or None when Firefly has no such id."""
        try:
            result = await self._request("GET", f"transactions/{transaction_id}")
        except PermanentError as exc:
            if str(exc).startswith("Firefly 404"):
                return None
            raise
        return result.get("data")

    async def budgets(self) -> list[dict]:
        """Every budget as ``{id, name}``; the ledger editor resolves budgets by name."""
        return [{"id": str(row["id"]), "name": row["attributes"]["name"]} for row in await self._list("budgets")]

    async def bills(self) -> list[dict]:
        """Every bill (recurring charge) as ``{id, name}``; the ledger editor resolves bills by name."""
        return [{"id": str(row["id"]), "name": row["attributes"]["name"]} for row in await self._list("bills")]

    async def create_budget(self, name: str) -> dict:
        """Create an active budget; returns ``{id, name}``."""
        result = await self._request("POST", "budgets", json={"name": name, "active": True})
        return {"id": str(result["data"]["id"]), "name": result["data"]["attributes"]["name"]}

    async def budget_limits(self, budget_id: str, start, end) -> list[dict]:
        """The limits of a budget that overlap the period, as ``{id, amount, start, end}`` (``amount`` Decimal, dates ``YYYY-MM-DD``)."""
        result = await self._request("GET", f"budgets/{budget_id}/limits", params={"start": start.isoformat(), "end": end.isoformat()})
        rows = []
        for row in result.get("data", []):
            attrs = row.get("attributes", {})
            if attrs.get("currency_code") in (None, "", "COP"):
                rows.append({"id": str(row["id"]), "amount": self._decimal(attrs.get("amount")), "start": str(attrs.get("start", ""))[:10], "end": str(attrs.get("end", ""))[:10]})
        return rows

    async def create_budget_limit(self, budget_id: str, start, end, amount: Decimal) -> str:
        result = await self._request(
            "POST", f"budgets/{budget_id}/limits",
            json={"start": start.isoformat(), "end": end.isoformat(), "amount": f"{amount:.2f}", "currency_code": "COP"},
        )
        return str(result["data"]["id"])

    async def update_budget_limit(self, budget_id: str, limit_id: str, start, end, amount: Decimal) -> None:
        await self._request(
            "PUT", f"budgets/{budget_id}/limits/{limit_id}",
            json={"start": start.isoformat(), "end": end.isoformat(), "amount": f"{amount:.2f}", "currency_code": "COP"},
        )

    async def insight(self, start, end) -> dict:
        return await self._request(
            "GET", "insight/expense/category",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )

    @staticmethod
    def _decimal(value) -> Decimal:
        """Firefly sends money as strings, numbers or ``[{"sum": "-12.00"}]`` lists; spending is negative."""
        if isinstance(value, list):
            return sum((FireflyClient._decimal(row.get("sum") if isinstance(row, dict) else row) for row in value), Decimal(0))
        try:
            return abs(Decimal(str(value))) if value not in (None, "") else Decimal(0)
        except InvalidOperation:
            return Decimal(0)

    async def budget_progress(self, start, end) -> list[dict]:
        """Active budgets with a limit in the period: ``[{name, spent, limit}]`` (COP, two decimals)."""
        params = {"start": start.isoformat(), "end": end.isoformat()}
        insight = None
        rows = []
        for budget in await self._list("budgets"):
            attrs = budget.get("attributes", {})
            if attrs.get("active") is False:
                continue
            limits = (await self._request("GET", f"budgets/{budget['id']}/limits", params=params)).get("data", [])
            limits = [row.get("attributes", {}) for row in limits]
            limits = [row for row in limits if row.get("currency_code") in (None, "", "COP")]
            limit = sum((self._decimal(row.get("amount")) for row in limits), Decimal(0))
            if limit <= 0:
                continue
            if any("spent" in row for row in limits):
                spent = sum((self._decimal(row.get("spent")) for row in limits), Decimal(0))
            else:
                if insight is None:
                    result = await self._request("GET", "insight/expense/budget", params=params)
                    insight = result if isinstance(result, list) else result.get("data", [])
                spent = sum((self._decimal(row.get("difference")) for row in insight
                             if str(row.get("id")) == str(budget["id"]) and row.get("currency_code") in (None, "", "COP")), Decimal(0))
            rows.append({"name": attrs.get("name", str(budget["id"])), "spent": spent.quantize(Decimal("0.01")), "limit": limit.quantize(Decimal("0.01"))})
        return rows

    async def period_splits(self, start, end) -> list[dict]:
        """Every split of every transaction group between two dates (inclusive), as Firefly sends the split fields."""
        groups = await self._list("transactions", start=start.isoformat(), end=end.isoformat(), type="all")
        return [split for group in groups for split in group.get("attributes", {}).get("transactions", [])]

    async def insight_total(self, kind: str, start, end) -> Decimal:
        """COP total of ``expense`` or ``income`` between two dates (absolute value)."""
        result = await self._request("GET", f"insight/{kind}/total", params={"start": start.isoformat(), "end": end.isoformat()})
        rows = result if isinstance(result, list) else result.get("data", [])
        return sum((self._decimal(row.get("difference")) for row in rows if row.get("currency_code") in (None, "", "COP")), Decimal(0))

    async def list_auto_transactions(self) -> list[dict]:
        return await self._list("search/transactions", query='tag_is:"auto"')

    async def account_transactions(self, account_id: str, start, end) -> list[dict]:
        """Every journal of one account between two dates (inclusive), flattened.

        Each row keeps the group id as ``firefly_id`` plus the journal's ``type``, ``date``, ``description``, ``amount``,
        ``source_id`` and ``destination_id`` exactly as Firefly sends them; the caller interprets them.
        """
        groups = await self._list(
            f"accounts/{account_id}/transactions", start=start.isoformat(), end=end.isoformat(), type="all",
        )
        return [
            {"firefly_id": str(group["id"]), **{key: split.get(key) for key in ("transaction_journal_id", "type", "date", "description", "amount", "source_id", "destination_id")}}
            for group in groups for split in group["attributes"]["transactions"]
        ]

    async def update_split_account(self, group_id: str, journal_id: str, field: str, account_id: str) -> None:
        """Point one split of a group at another account (``field`` is ``source_id`` or ``destination_id``).

        Rules are NOT applied: merging counterparties must not recategorize anything.
        """
        if field not in {"source_id", "destination_id"}:
            raise PermanentError("Only the source or destination account of a split may be moved")
        await self._request(
            "PUT", f"transactions/{group_id}",
            json={"apply_rules": False, "fire_webhooks": False, "transactions": [{"transaction_journal_id": str(journal_id), field: str(account_id)}]},
        )

    async def delete_account(self, account_id: str) -> None:
        """Delete one account. Firefly deletes its transactions too: callers must have checked it is empty."""
        await self._request("DELETE", f"accounts/{account_id}")

    async def check_connection(self) -> bool:
        if not self.settings.firefly_base_url or not self.settings.firefly_api_token.get_secret_value():
            return False
        try:
            await self._request("GET", "about")
            return True
        except (FireflyError, httpx.HTTPError, ValueError, KeyError):
            return False

    async def close(self) -> None:
        await self._client.aclose()
