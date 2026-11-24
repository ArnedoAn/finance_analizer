"""Official TRM (Tasa Representativa del Mercado, COP per USD) from datos.gov.co, a SODA (Socrata) open-data API.

Dataset ``32sa-8pi3``, public and key-less. Each row is one rate and the days it governs:
``{"valor": "3750.50", "unidad": "COP", "vigenciadesde": "2026-11-24T00:00:00.000", "vigenciahasta": "2026-11-24T00:00:00.000"}``
(Friday's rate runs through Monday, so ``vigenciahasta`` can be days after ``vigenciadesde``). Queries use SoQL
``$where`` / ``$order`` / ``$limit``. Every call has a timeout and every failure is a ``TrmError``; the cache and the
stale fallback live in ``app.services.fx``.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from app.core.exceptions import TrmError


@dataclass(frozen=True)
class TrmRate:
    valid_from: date
    valid_until: date
    rate: Decimal


def _parse(row: dict) -> TrmRate:
    try:
        rate = Decimal(str(row["valor"]))
        valid_from = date.fromisoformat(str(row["vigenciadesde"])[:10])
        valid_until = date.fromisoformat(str(row["vigenciahasta"])[:10])
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise TrmError("Unexpected TRM row") from exc
    if rate <= 0 or valid_until < valid_from:
        raise TrmError("Unexpected TRM row")
    return TrmRate(valid_from, valid_until, rate)


class TrmClient:
    def __init__(self, url: str, timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport, headers={"Accept": "application/json"})

    async def _rows(self, **params) -> list[TrmRate]:
        try:
            response = await self._client.get(self._url, params=params)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TrmError("TRM service unavailable") from exc
        if not isinstance(data, list):
            raise TrmError("Unexpected TRM response")
        return [_parse(row) for row in data]

    async def for_date(self, day: date) -> TrmRate | None:
        """The rate that governs ``day`` (``vigenciadesde <= day <= vigenciahasta``) or None when the dataset has none."""
        stamp = f"{day.isoformat()}T00:00:00.000"
        rows = await self._rows(**{"$where": f"vigenciadesde <= '{stamp}' AND vigenciahasta >= '{stamp}'", "$order": "vigenciadesde DESC", "$limit": "1"})
        return rows[0] if rows else None

    async def latest(self, count: int = 10) -> list[TrmRate]:
        """The ``count`` most recent rates, newest first."""
        return await self._rows(**{"$order": "vigenciadesde DESC", "$limit": str(count)})

    async def close(self) -> None:
        await self._client.aclose()
