"""Manual transfer validation stays ahead of persistence and ledger writes."""
import pytest

from app.models.schemas import ManualTransaction


async def test_manual_transfer_requires_distinct_destination(service, clients):
    for destination in (None, "Nequi", "Missing account"):
        payload = ManualTransaction(client_ref=f"invalid:{destination}", amount="25000", description="Pago tarjeta", type="transfer", destination_account=destination)
        with pytest.raises(ValueError):
            await service.manual(payload)
    assert await service.inbox.count() == 0
    clients[0].create_transaction.assert_not_awaited()


async def test_manual_transfer_accepts_firefly_liabilities(service, clients):
    clients[0].accounts.return_value.append({"id": "9", "name": "Visa", "type": "liabilities"})
    item = await service.manual(ManualTransaction(client_ref="card-payment", amount="25000", description="Pago tarjeta", type="transfer", destination_account="Visa"))
    assert item.status == "created"
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert (split["type"], split["source_id"], split["destination_id"]) == ("transfer", "1", "9")
    assert split["tags"] == ["manual"]
