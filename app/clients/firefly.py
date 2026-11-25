"""Single-owner Firefly JSON:API client; retries belong to the durable inbox."""

from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from app.core import error_log
from app.core.config import get_settings
from app.core.exceptions import FireflyError, PermanentError

# Widest period Firefly accepts: it rejects an end date on or after 2038-01-17 (422).
ALL_TIME = (date(2000, 1, 1), date(2037, 12, 31))


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

    async def create_category(self, name: str) -> str:
        """Create a category and return its name as Firefly stored it."""
        result = await self._request("POST", "categories", json={"name": name})
        return result["data"]["attributes"]["name"]

    async def _category_row(self, name: str) -> dict:
        wanted = name.strip().casefold()
        for row in await self._list("categories"):
            if row["attributes"]["name"].casefold() == wanted:
                return row
        raise PermanentError("Firefly 404: category not found")

    async def rename_category(self, name: str, new_name: str) -> str:
        """Rename a category and return its name as Firefly stored it."""
        row = await self._category_row(name)
        result = await self._request("PUT", f"categories/{row['id']}", json={"name": new_name})
        return result["data"]["attributes"]["name"]

    async def delete_category(self, name: str) -> None:
        """Delete a category; Firefly leaves its transactions without category."""
        row = await self._category_row(name)
        await self._request("DELETE", f"categories/{row['id']}")

    async def create_account(self, name: str, type: str) -> dict:
        if type not in {"expense", "revenue"}:
            raise PermanentError("Only merchant expense/revenue accounts may be created")
        result = await self._request(
            "POST", "accounts", json={"name": name, "type": type, "currency_code": "COP"}
        )
        return self._account(result["data"])

    async def create_liability(
        self, name: str, liability_type: str, rate_ea: Decimal, opening_balance: Decimal, opening_date: date,
    ) -> dict:
        """Create a liability account the owner owes (``POST accounts`` with ``type`` ``liability``) and return it like ``accounts()``.

        This is the only explicit way the analyzer creates a non-merchant account (``create_account`` stays restricted to
        expense/revenue merchants); the debts service is its only caller. Fields (Firefly III API v1, ``AccountStore``):
        ``liability_type`` is ``loan`` or ``debt`` (also ``mortgage``), ``liability_direction`` ``debit`` is «I owe this debt»
        (``credit`` would mean somebody owes the owner), ``interest`` is the percentage and ``interest_period`` ``yearly``
        (the rate is an EA), and ``opening_balance`` goes with ``opening_balance_date`` (both or none). The opening balance
        of a liability the owner owes is sent negative, as in the API's own example; the service reads the balance back
        from Firefly and uses its absolute value, so the sign Firefly stores never changes what the app shows.
        """
        if liability_type not in {"loan", "debt"}:
            raise PermanentError("Only loan or debt liabilities may be created")
        result = await self._request("POST", "accounts", json={
            "name": name, "type": "liability", "liability_type": liability_type, "liability_direction": "debit",
            "interest": f"{rate_ea:.2f}", "interest_period": "yearly",
            "opening_balance": f"{-abs(opening_balance):.2f}", "opening_balance_date": opening_date.isoformat(),
            "currency_code": "COP", "active": True, "include_net_worth": True,
        })
        return self._account(result["data"])

    async def update_account(self, account_id: str, fields: dict) -> None:
        """Partial ``PUT accounts/{id}``; the debts service only sends ``interest`` and ``interest_period`` (a rate change)."""
        await self._request("PUT", f"accounts/{account_id}", json=fields)

    async def account_balance(self, account_id: str) -> dict | None:
        """``{type, balance}`` of one account (``balance`` is Firefly's signed ``current_balance``) or None when it does not exist."""
        try:
            result = await self._request("GET", f"accounts/{account_id}")
        except PermanentError as exc:
            if str(exc).startswith("Firefly 404"):
                return None
            raise
        attrs = result["data"]["attributes"]
        value = attrs.get("current_balance")
        try:
            balance = Decimal(str(value)) if value not in (None, "") else Decimal(0)
        except InvalidOperation:
            balance = Decimal(0)
        return {"type": attrs["type"], "balance": balance}

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

    async def bills_status(self, start, end) -> list[dict]:
        """Active bills with what Firefly expects and has matched between two dates (inclusive), read only.

        ``[{id, name, amount_min, amount_max, currency, repeat_freq, pay_dates: [YYYY-MM-DD], paid_dates: [YYYY-MM-DD]}]``;
        ``pay_dates`` are the expected charges in the period and ``paid_dates`` the dates of the transactions already
        matched to the bill in it (Firefly fills both when ``start`` and ``end`` are sent).
        """
        rows = []
        for row in await self._list("bills", start=start.isoformat(), end=end.isoformat()):
            attrs = row.get("attributes", {})
            if attrs.get("active") is False:
                continue
            paid = [str(entry.get("date", ""))[:10] for entry in attrs.get("paid_dates") or [] if isinstance(entry, dict)]
            rows.append({
                "id": str(row["id"]), "name": attrs.get("name", str(row["id"])),
                "amount_min": self._decimal(attrs.get("amount_min")), "amount_max": self._decimal(attrs.get("amount_max")),
                "currency": attrs.get("currency_code") or "COP", "repeat_freq": attrs.get("repeat_freq"),
                "pay_dates": [str(day)[:10] for day in attrs.get("pay_dates") or []],
                "paid_dates": [day for day in paid if day],
            })
        return rows

    async def create_budget(self, name: str) -> dict:
        """Create an active budget; returns ``{id, name}``."""
        result = await self._request("POST", "budgets", json={"name": name, "active": True})
        return {"id": str(result["data"]["id"]), "name": result["data"]["attributes"]["name"]}

    async def rename_budget(self, budget_id: str, name: str) -> dict:
        """Rename a budget (limits and transactions keep pointing at it); returns ``{id, name}`` as Firefly stores it."""
        result = await self._request("PUT", f"budgets/{budget_id}", json={"name": name})
        return {"id": str(result["data"]["id"]), "name": result["data"]["attributes"]["name"]}

    async def delete_budget(self, budget_id: str) -> None:
        """Delete a budget with its limits; Firefly leaves its transactions without budget."""
        await self._request("DELETE", f"budgets/{budget_id}")

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

    async def transaction_groups_page(self, page: int, start, end) -> tuple[list[dict], int]:
        """One page (100) of transaction groups between two dates (JSON:API ``data``) and the total number of pages."""
        result = await self._request(
            "GET", "transactions", params={"start": start.isoformat(), "end": end.isoformat(), "type": "all", "page": page, "limit": 100},
        )
        pagination = result.get("meta", {}).get("pagination", {})
        return result["data"], int(pagination.get("total_pages", 1))

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
