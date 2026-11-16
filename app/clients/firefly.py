"""Single-owner Firefly JSON:API client; retries belong to the durable inbox."""

import httpx

from app.core.config import get_settings


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
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def _list(self, path: str) -> list[dict]:
        rows = []
        page = 1
        while True:
            result = await self._request("GET", path, params={"page": page, "limit": 100})
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
            raise ValueError("Only merchant expense/revenue accounts may be created")
        result = await self._request(
            "POST", "accounts", json={"name": name, "type": type, "currency_code": "COP"}
        )
        return self._account(result["data"])

    async def create_transaction(self, payload: dict) -> str:
        result = await self._request(
            "POST", "transactions", json={**payload, "apply_rules": True}
        )
        return str(result["data"]["id"])

    async def check_connection(self) -> bool:
        if not self.settings.firefly_base_url or not self.settings.firefly_api_token.get_secret_value():
            return False
        try:
            await self._request("GET", "about")
            return True
        except (httpx.HTTPError, ValueError, KeyError):
            return False

    async def close(self) -> None:
        await self._client.aclose()
