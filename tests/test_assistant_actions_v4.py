"""Assistant actions beyond inbox edits: categories, registered movements, budgets and rules (Firefly simulated).

Every action goes proposal -> owner approval (``/apply``) -> the owner's own service, and a failure stays on its
proposal (``failed`` with ``error``) without stopping the rest of the batch.
"""
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.exceptions import PermanentError
from app.core.timezone import now_utc
from app.db.models_assistant import Audit
from app.db.models_rules import Merchant, Rule
from app.services import rules
from tests.test_assistant_v4 import ADMIN, API, apply, open_batch
from tests.test_ledger_v4 import FireflyLedger

BOGOTA = ZoneInfo("America/Bogota")


def when(hours: int) -> str:
    return (now_utc() - timedelta(hours=hours)).astimezone(BOGOTA).replace(microsecond=0).isoformat()


class World:
    """Firefly with stateful categories, budgets (with monthly limits) and a ledger."""

    def __init__(self, firefly):
        self.firefly = firefly
        firefly.accounts.return_value = [*firefly.accounts.return_value, {"id": "7", "name": "DiDi", "type": "expense"}]
        self.ledger = FireflyLedger(firefly, firefly.accounts.return_value, budgets={"11": "Mercado", "12": "Ocio"}, bills={})
        self.categories = ["Alimentación", "Ocio"]
        self.limits = {}  # (budget id, start, end) -> {"id", "amount"}
        self._next = 40
        firefly.categories.side_effect = lambda: list(self.categories)
        firefly.create_category.side_effect = self.create_category
        firefly.rename_category.side_effect = self.rename_category
        firefly.delete_category.side_effect = self.delete_category
        firefly.budgets.side_effect = lambda: [{"id": ident, "name": name} for ident, name in self.ledger.budgets.items()]
        firefly.create_budget.side_effect = self.create_budget
        firefly.budget_limits.side_effect = self.budget_limits
        firefly.create_budget_limit.side_effect = self.create_budget_limit
        firefly.update_budget_limit.side_effect = self.update_budget_limit
        firefly.delete_budget.side_effect = self.delete_budget

    async def create_category(self, name):
        self.categories.append(name)
        return name

    async def rename_category(self, name, new_name):
        self.categories[self.categories.index(name)] = new_name
        return new_name

    async def delete_category(self, name):
        self.categories.remove(name)

    async def create_budget(self, name):
        self._next += 1
        self.ledger.budgets[str(self._next)] = name
        return {"id": str(self._next), "name": name}

    async def budget_limits(self, budget_id, start, end):
        return [{"id": row["id"], "amount": row["amount"], "start": s, "end": e} for (ident, s, e), row in self.limits.items() if ident == budget_id]

    async def create_budget_limit(self, budget_id, start, end, amount):
        self._next += 1
        self.limits[(budget_id, start.isoformat(), end.isoformat())] = {"id": str(self._next), "amount": amount}
        return str(self._next)

    async def update_budget_limit(self, budget_id, limit_id, start, end, amount):
        next(row for row in self.limits.values() if row["id"] == limit_id)["amount"] = amount

    async def delete_budget(self, budget_id):
        self.ledger.budgets.pop(budget_id)
        self.limits = {key: row for key, row in self.limits.items() if key[0] != budget_id}

    def limit(self, name, month):
        ident = next(ident for ident, label in self.ledger.budgets.items() if label == name)
        return self.limits[(ident, f"{month}-01", f"{month}-30")]["amount"]

    def movement(self, description="DiDi viaje", hours=2, destination="7", **extra):
        return self.ledger.seed(description=description, destination=destination, category_name="Alimentación", date=when(hours), **extra)

    def category_of(self, ref):
        return self.ledger.journals(ref)[0]["category_name"]


@pytest.fixture
def world(clients):
    return World(clients[0])


async def act(client, batch_id, field, target=None, value=None, status=201):
    response = await client.post(f"{API}/{batch_id}/proposals", headers=ADMIN, json={"field": field, "target_ref": target, "new_value": value, "reason": "porque sí"})
    assert response.status_code == status, response.text
    return response.json()


async def batch_state(client, batch_id):
    return (await client.get(f"{API}/{batch_id}", headers=ADMIN)).json()


def by_id(batch, proposal_id):
    return next(proposal for proposal in batch["proposals"] if proposal["id"] == proposal_id)


async def audit_actions(db):
    return {row.action for row in (await db.scalars(select(Audit))).all()}


# --- the case the owner reported -----------------------------------------------------------------------------------------


async def test_recategorize_registered_movements_creating_the_category(client, world, db):
    first, second, other = world.movement(), world.movement(hours=3), world.movement("Mercado", hours=4, destination="2")
    batch_id = (await open_batch(client))["id"]
    edits = [await act(client, batch_id, "ledger_edit", ref, {"category": "Transporte"}) for ref in (first, second)]
    create = await act(client, batch_id, "create_category", None, "Transporte")
    assert [edit["risky"] for edit in edits] == [False, False] and create["target_id"] is None
    assert create["subject"] == "Transporte" and edits[0]["subject"].startswith("DiDi viaje · 12000.00")

    # The owner approves the assignments listed before the creation: the creation still runs first.
    response = await apply(client, batch_id, [edit["id"] for edit in edits] + [create["id"]])
    body = response.json()
    assert body["failed"] == [] and body["applied"] == [create["id"], edits[0]["id"], edits[1]["id"]]
    assert world.categories == ["Alimentación", "Ocio", "Transporte"]
    assert world.category_of(first) == world.category_of(second) == "Transporte"
    assert world.category_of(other) == "Alimentación"
    assert body["batch"]["status"] == "applied"
    assert {"category.create", "ledger.update"} <= await audit_actions(db)

    # "Deshacer lote" restores the movements; a created category stays (it has no automatic undo).
    reverted = (await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)).json()
    assert sorted(reverted["reverted"]) == sorted(edit["id"] for edit in edits) and reverted["failed"] == []
    assert world.category_of(first) == world.category_of(second) == "Alimentación"
    assert "Transporte" in world.categories and reverted["batch"]["status"] == "reverted"


async def test_unknown_category_without_creation_fails_visibly_and_the_rest_applies(client, world):
    first, second = world.movement(), world.movement(hours=3)
    batch_id = (await open_batch(client))["id"]
    wrong = await act(client, batch_id, "ledger_edit", first, {"category": "Transporte"})
    fine = await act(client, batch_id, "ledger_edit", second, {"description": "DiDi corregido"})
    body = (await apply(client, batch_id, [wrong["id"], fine["id"]])).json()
    assert body["applied"] == [fine["id"]]
    assert body["failed"] == [{"id": wrong["id"], "detail": "Unknown category: Transporte"}]
    state = await batch_state(client, batch_id)
    assert (by_id(state, wrong["id"])["status"], by_id(state, wrong["id"])["error"]) == ("failed", "Unknown category: Transporte")
    assert by_id(state, fine["id"])["status"] == "applied" and state["status"] == "partially_applied"
    assert world.category_of(first) == "Alimentación" and "Transporte" not in world.categories
    assert world.ledger.journals(second)[0]["description"] == "DiDi corregido"


async def test_inbox_proposal_with_an_unknown_category_fails_naming_it_and_a_creation_in_the_batch_fixes_it(client, service, clients):
    from tests.test_assistant_v4 import make, propose

    firefly = clients[0]
    stored = ["Alimentación"]
    firefly.categories.side_effect = lambda: list(stored)

    async def create(name):
        stored.append(name)
        return name

    firefly.create_category.side_effect = create
    item = await make(service, category="Alimentación")
    batch_id = (await open_batch(client))["id"]
    assign = (await propose(client, batch_id, item.id, "category", "Transporte")).json()
    create = await act(client, batch_id, "create_category", None, "Transporte")
    body = (await apply(client, batch_id, [assign["id"]])).json()
    assert body["failed"] == [{"id": assign["id"], "detail": "Unknown category: Transporte"}]
    body = (await apply(client, batch_id, [assign["id"], create["id"]])).json()
    assert body["failed"] == [] and body["applied"] == [create["id"], assign["id"]]
    assert stored[-1] == "Transporte"


# --- categories --------------------------------------------------------------------------------------------------------


async def test_create_category_validates_and_is_not_proposed_twice(client, world):
    batch_id = (await open_batch(client))["id"]
    await act(client, batch_id, "create_category", None, "Transporte")
    await act(client, batch_id, "create_category", None, " transporte ", status=409)
    for bad in ("", "a$b", "x" * 65, 12, None):
        await act(client, batch_id, "create_category", None, bad, status=422)
    existing = await act(client, batch_id, "create_category", None, "alimentacion")
    body = (await apply(client, batch_id, [existing["id"]])).json()
    assert body["applied"] == [existing["id"]] and world.categories == ["Alimentación", "Ocio"]  # exists in other spelling: nothing created


async def test_rename_category_keeps_rules_and_merchants_in_step(client, world, db):
    await rules.create_rule(db, match="merchant_exact", pattern="DIDI", category="Alimentación")
    db.add(Merchant(name="DIDI", default_category="Alimentación", count=1))
    await db.commit()
    batch_id = (await open_batch(client))["id"]
    await act(client, batch_id, "rename_category", "Inexistente", "Comida", status=422)
    await act(client, batch_id, "rename_category", "Alimentación", "ocio", status=409)
    await act(client, batch_id, "rename_category", "Alimentación", "alimentacion", status=422)
    rename = await act(client, batch_id, "rename_category", "alimentación", "Comida")
    assert rename["target_ref"] == "Alimentación" and rename["risky"] is False
    body = (await apply(client, batch_id, [rename["id"]])).json()
    assert body["failed"] == [] and world.categories == ["Comida", "Ocio"]
    assert await db.scalar(select(Rule.category)) == "Comida"
    assert await db.scalar(select(Merchant.default_category)) == "Comida"
    assert "category.rename" in await audit_actions(db)


async def test_delete_category_is_risky_and_refused_while_a_rule_uses_it(client, world, db):
    rule = await rules.create_rule(db, match="merchant_exact", pattern="DIDI", category="Ocio")
    await db.commit()
    rule_id = rule.id
    batch_id = (await open_batch(client))["id"]
    delete = await act(client, batch_id, "delete_category", "ocio")
    drop_rule = await act(client, batch_id, "delete_rule", rule_id)
    assert delete["risky"] and drop_rule["risky"] and delete["target_ref"] == "Ocio"
    together = await apply(client, batch_id, [delete["id"], drop_rule["id"]])
    assert together.status_code == 422 and together.json()["code"] == "risky_requires_single"

    refused = (await apply(client, batch_id, [delete["id"]])).json()
    assert refused["applied"] == [] and "1 active rule(s) still assign «Ocio»" in refused["failed"][0]["detail"]
    assert by_id(refused["batch"], delete["id"])["status"] == "failed" and "Ocio" in world.categories

    assert (await apply(client, batch_id, [drop_rule["id"]])).json()["applied"] == [drop_rule["id"]]
    assert await db.scalar(select(Rule.id)) is None
    final = (await apply(client, batch_id, [delete["id"]])).json()
    assert final["applied"] == [delete["id"]] and world.categories == ["Alimentación"]
    assert {"rule.delete", "category.delete"} <= await audit_actions(db)


async def test_firefly_failure_on_a_category_action_is_visible(client, world):
    world.firefly.create_category.side_effect = PermanentError("Firefly 422: nombre duplicado")
    batch_id = (await open_batch(client))["id"]
    create = await act(client, batch_id, "create_category", None, "Transporte")
    body = (await apply(client, batch_id, [create["id"]])).json()
    assert body["applied"] == [] and body["failed"] == [{"id": create["id"], "detail": "Firefly 422: nombre duplicado"}]
    assert by_id(body["batch"], create["id"])["error"] == "Firefly 422: nombre duplicado"


# --- registered movements ----------------------------------------------------------------------------------------------


async def test_ledger_edit_fields_and_their_risk(client, world):
    ref = world.movement(amount="10000.00")
    batch_id = (await open_batch(client))["id"]
    plain = await act(client, batch_id, "ledger_edit", ref, {"description": "Viaje al aeropuerto", "merchant": "DiDi", "budget": "ocio", "tags_add": ["viaje", " viaje "]})
    assert plain["risky"] is False and plain["old_value"]["merchant"] == "DiDi" and plain["old_value"]["tags"] == []
    assert plain["new_value"] == {"description": "Viaje al aeropuerto", "merchant": "DiDi", "budget": "ocio", "tags_add": ["viaje"]}
    moved = await act(client, batch_id, "ledger_edit", ref, {"description": "Otra"}, status=409)  # one proposal per movement
    assert "already" in moved["detail"]
    assert (await apply(client, batch_id, [plain["id"]])).json()["failed"] == []
    journal = world.ledger.journals(ref)[0]
    assert journal["description"] == "Viaje al aeropuerto" and journal["tags"] == ["viaje"] and journal["budget_name"] == "Ocio"

    new_date = when(6)
    for hours, value, check in (
        (3, {"amount": "15000.50"}, lambda journal: Decimal(journal["amount"]) == Decimal("15000.50")),
        (4, {"date": new_date}, lambda journal: journal["date"].startswith(new_date[:16])),
        (5, {"source_account": "Bancolombia"}, lambda journal: journal["source_name"] == "Bancolombia"),
    ):
        batch_id = (await open_batch(client))["id"]
        other = world.movement("Mercado", hours=hours, amount="10000.00")
        proposal = await act(client, batch_id, "ledger_edit", other, value)
        assert proposal["risky"] is True, value
        body = (await apply(client, batch_id, [proposal["id"]])).json()
        assert body["failed"] == [], (value, body["failed"])
        assert check(world.ledger.journals(other)[0]), value


async def test_ledger_edit_validation(client, world):
    ref = world.movement()
    old = world.movement("Viejo", hours=24 * 10)
    batch_id = (await open_batch(client, hours=48))["id"]
    await act(client, batch_id, "ledger_edit", old, {"description": "x"}, status=422)  # outside the window
    await act(client, batch_id, "ledger_edit", "9999", {"description": "x"}, status=404)
    await act(client, batch_id, "ledger_edit", ref, {}, status=422)
    await act(client, batch_id, "ledger_edit", ref, {"color": "rojo"}, status=422)
    await act(client, batch_id, "ledger_edit", ref, {"amount": -5}, status=422)
    await act(client, batch_id, "ledger_edit", ref, {"description": "DiDi viaje"}, status=422)  # changes nothing
    await act(client, batch_id, "ledger_edit", None, {"description": "x"}, status=422)
    assert (await batch_state(client, batch_id))["proposals"] == []


async def test_ledger_edit_that_went_stale_fails_visibly(client, world):
    ref = world.movement()
    batch_id = (await open_batch(client))["id"]
    proposal = await act(client, batch_id, "ledger_edit", ref, {"description": "DiDi aeropuerto"})
    world.ledger.journals(ref)[0]["description"] = "Cambiado por el dueño"
    body = (await apply(client, batch_id, [proposal["id"]])).json()
    assert body["applied"] == [] and "stale" in body["failed"][0]["detail"]
    assert world.ledger.journals(ref)[0]["description"] == "Cambiado por el dueño"


async def test_ledger_delete_is_risky_applies_and_can_be_undone(client, world):
    ref = world.movement(amount="8000.00")
    batch_id = (await open_batch(client))["id"]
    delete = await act(client, batch_id, "ledger_delete", ref)
    assert delete["risky"] and delete["old_value"]["amount"] == "8000.00" and delete["new_value"] is None
    await act(client, batch_id, "ledger_delete", ref, status=409)
    body = (await apply(client, batch_id, [delete["id"]])).json()
    assert body["applied"] == [delete["id"]] and ref not in world.ledger.groups
    reverted = (await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)).json()
    assert reverted["reverted"] == [delete["id"]]
    assert [group["attributes"]["transactions"][0]["description"] for group in world.ledger.groups.values()] == ["DiDi viaje"]


async def test_ledger_delete_firefly_failure_is_visible(client, world):
    ref = world.movement()
    world.firefly.delete_transaction.side_effect = PermanentError("Firefly 422: nope")
    batch_id = (await open_batch(client))["id"]
    delete = await act(client, batch_id, "ledger_delete", ref)
    body = (await apply(client, batch_id, [delete["id"]])).json()
    assert body["applied"] == [] and "Firefly 422" in body["failed"][0]["detail"] and ref in world.ledger.groups
    assert by_id(body["batch"], delete["id"])["status"] == "failed"


# --- budgets -----------------------------------------------------------------------------------------------------------


async def test_budgets_create_change_and_delete(client, world, db):
    batch_id = (await open_batch(client))["id"]
    month = now_utc().astimezone(BOGOTA).strftime("%Y-%m")
    create = await act(client, batch_id, "create_budget", None, {"name": "Transporte", "amount": 300000, "month": month})
    assert create["new_value"] == {"name": "Transporte", "amount": "300000.00", "month": month} and create["risky"] is False
    await act(client, batch_id, "create_budget", None, {"name": "mercado"}, status=409)
    await act(client, batch_id, "create_budget", None, {"name": "Viajes", "month": month}, status=422)
    assert (await apply(client, batch_id, [create["id"]])).json()["failed"] == []
    assert world.limit("Transporte", month) == Decimal("300000.00")

    batch_id = (await open_batch(client))["id"]
    change = await act(client, batch_id, "set_budget_limit", "mercado", {"amount": "500000", "month": month})
    assert change["old_value"] is None and change["target_ref"] == "Mercado"
    assert (await apply(client, batch_id, [change["id"]])).json()["failed"] == []

    batch_id = (await open_batch(client))["id"]
    again = await act(client, batch_id, "set_budget_limit", "Mercado", {"amount": "600000", "month": month})
    assert again["old_value"] == {"amount": "500000.00", "month": month}
    assert (await apply(client, batch_id, [again["id"]])).json()["failed"] == []
    assert world.limit("Mercado", month) == Decimal("600000.00")

    batch_id = (await open_batch(client))["id"]
    await act(client, batch_id, "set_budget_limit", "Inexistente", {"amount": "1", "month": month}, status=422)
    drop = await act(client, batch_id, "delete_budget", "ocio")
    assert drop["risky"] and drop["target_ref"] == "Ocio"
    assert (await apply(client, batch_id, [drop["id"]])).json()["applied"] == [drop["id"]]
    assert sorted(world.ledger.budgets.values()) == ["Mercado", "Transporte"]
    assert {"budget.create", "budget.limit", "budget.delete"} <= await audit_actions(db)


async def test_budget_limit_for_a_budget_created_in_the_same_batch_applies_after_it(client, world):
    batch_id = (await open_batch(client))["id"]
    month = now_utc().astimezone(BOGOTA).strftime("%Y-%m")
    create = await act(client, batch_id, "create_budget", None, {"name": "Viajes"})
    limit = await act(client, batch_id, "set_budget_limit", "viajes", {"amount": 100000, "month": month})
    body = (await apply(client, batch_id, [limit["id"], create["id"]])).json()
    assert body["failed"] == [] and body["applied"] == [create["id"], limit["id"]]
    assert world.limit("Viajes", month) == Decimal("100000.00")


async def test_budget_failure_is_visible(client, world):
    batch_id = (await open_batch(client))["id"]
    world.firefly.create_budget.side_effect = PermanentError("Firefly 422: presupuesto")
    create = await act(client, batch_id, "create_budget", None, {"name": "Viajes"})
    body = (await apply(client, batch_id, [create["id"]])).json()
    assert body["failed"] == [{"id": create["id"], "detail": "Firefly 422: presupuesto"}]


# --- rules -------------------------------------------------------------------------------------------------------------


async def test_rules_create_and_delete(client, world, db):
    batch_id = (await open_batch(client))["id"]
    create = await act(client, batch_id, "create_rule", None, {"match": "merchant_exact", "pattern": "didi", "category": "Transporte"})
    category = await act(client, batch_id, "create_category", None, "Transporte")
    await act(client, batch_id, "create_rule", None, {"match": "regex", "pattern": "x", "category": "Ocio"}, status=422)
    body = (await apply(client, batch_id, [create["id"], category["id"]])).json()
    assert body["failed"] == [] and body["applied"] == [category["id"], create["id"]]
    rule = (await db.scalars(select(Rule))).one()
    assert (rule.match, rule.pattern, rule.category) == ("merchant_exact", "DIDI", "Transporte")

    batch_id = (await open_batch(client))["id"]
    await act(client, batch_id, "delete_rule", 999, status=404)
    drop = await act(client, batch_id, "delete_rule", rule.id)
    assert drop["risky"] and drop["old_value"]["pattern"] == "DIDI"
    assert (await apply(client, batch_id, [drop["id"]])).json()["applied"] == [drop["id"]]
    assert await db.scalar(select(Rule.id)) is None


async def test_rule_with_an_unknown_category_fails_visibly(client, world):
    batch_id = (await open_batch(client))["id"]
    create = await act(client, batch_id, "create_rule", None, {"match": "merchant_contains", "pattern": "didi", "category": "Transporte"})
    body = (await apply(client, batch_id, [create["id"]])).json()
    assert body["applied"] == [] and body["failed"] == [{"id": create["id"], "detail": "Unknown category"}]


# --- batch behavior ----------------------------------------------------------------------------------------------------


async def test_a_batch_with_only_irreversible_actions_has_nothing_to_revert(client, world):
    batch_id = (await open_batch(client))["id"]
    create = await act(client, batch_id, "create_category", None, "Transporte")
    assert (await apply(client, batch_id, [create["id"]])).json()["applied"] == [create["id"]]
    response = await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)
    assert response.status_code == 409 and response.json()["code"] == "conflict"


async def test_action_proposals_need_an_open_batch_and_count_toward_the_cap(client, world):
    batch_id = (await open_batch(client))["id"]
    for number in range(50):
        await act(client, batch_id, "create_category", None, f"Categoría {number}")
    over = await act(client, batch_id, "create_category", None, "Una más", status=422)
    assert over["code"] == "batch_limit"
    await client.post(f"{API}/{batch_id}/apply", headers=ADMIN, json={"approved_ids": [], "discard_others": True})
    closed = await act(client, batch_id, "create_category", None, "Tarde", status=409)
    assert closed["code"] == "conflict"
