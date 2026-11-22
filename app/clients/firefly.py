"""Single-owner Firefly JSON:API client; retries belong to the durable inbox."""

from decimal import Decimal, InvalidOperation

import httpx

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
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise FireflyError("Firefly network request failed") from exc
        if 400 <= response.status_code < 500 and response.status_code != 429:
            try:
                body = response.json()
                message = str(body.get("message", "Validation failed"))
                errors = body.get("errors", {})
                message = f"{message}: {errors}" if errors else message
            except (ValueError, AttributeError):
                message = "Request rejected"
            token = self.settings.firefly_api_token.get_secret_value()
            if token:
                message = message.replace(token, "[redacted]")
            raise PermanentError(f"Firefly {response.status_code}: {message[:1500]}")
        if response.is_error:
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

    async def list_auto_transactions(self) -> list[dict]:
        return await self._list("search/transactions", query='tag_is:"auto"')

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
