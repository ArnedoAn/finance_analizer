"""``POST /assistant/batches/{id}/apply`` applies in the background: 202 with a run, progress in ``GET``, idempotent by key.

Firefly is slow and the reverse proxy cuts a long request, so the request only validates and records a run; a task with
its own session does the work. These tests wait for that task (``assistant_runs.wait_idle``) or hold it with an event.
"""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.timezone import now_utc
from app.db.models_assistant import AssistantRun, Audit, Proposal
from app.services import assistant, assistant_runs, ledger
from tests.test_assistant_actions_v4 import World, act, batch_state, by_id
from tests.test_assistant_v4 import ADMIN, API, apply, open_batch, start_apply


@pytest.fixture
def world(clients):
    return World(clients[0])


class SlowFirefly:
    """Holds the ``hold``-th ``update_transaction`` from now on until ``release()`` (the others go straight through)."""

    def __init__(self, world, hold=1):
        self.world, self.hold, self.calls = world, hold, 0
        self.entered, self._gate = asyncio.Event(), asyncio.Event()
        self._original = world.ledger.update_transaction
        world.firefly.update_transaction.side_effect = self._update

    async def _update(self, transaction_id, payload):
        self.calls += 1
        if self.calls == self.hold:
            self.entered.set()
            await self._gate.wait()
        return await self._original(transaction_id, payload)

    async def inside(self):
        await asyncio.wait_for(self.entered.wait(), 5)

    def release(self):
        self._gate.set()


async def edits(client, world, count, category="Ocio"):
    refs = [world.movement(f"DiDi {number}", hours=2 + number % 20) for number in range(count)]
    batch_id = (await open_batch(client, hours=72))["id"]
    ids = [(await act(client, batch_id, "ledger_edit", ref, {"category": category}))["id"] for ref in refs]
    return batch_id, ids, refs


async def finish(client, batch_id):
    await assistant_runs.wait_idle()
    return await batch_state(client, batch_id)


async def test_apply_answers_202_at_once_and_everything_ends_applied(client, world):
    batch_id, ids, refs = await edits(client, world, 3)
    response = await start_apply(client, batch_id, ids)
    assert response.status_code == 202, response.text
    body = response.json()
    run = body["run"]
    assert set(run) == {"id", "status", "approved_ids", "applied", "failed", "pending", "started_at", "finished_at"}
    assert (run["status"], run["approved_ids"], run["applied"], run["failed"], run["pending"], run["finished_at"]) == ("running", ids, [], [], ids, None)
    assert body["batch"]["run"] == run and body["batch"]["status"] == "open"

    state = await finish(client, batch_id)
    assert state["run"]["id"] == run["id"] and state["run"]["status"] == "done"
    assert (state["run"]["applied"], state["run"]["failed"], state["run"]["pending"]) == (ids, [], [])
    assert state["run"]["finished_at"] is not None and state["status"] == "applied"
    assert {proposal["status"] for proposal in state["proposals"]} == {"applied"}
    assert all(world.category_of(ref) == "Ocio" for ref in refs)


async def test_the_same_key_answers_the_same_run_without_applying_again(client, world, db):
    batch_id, ids, _ = await edits(client, world, 2)
    headers = {**ADMIN, "Idempotency-Key": "apply-1"}
    first = await client.post(f"{API}/{batch_id}/apply", headers=headers, json={"approved_ids": ids})
    assert first.status_code == 202
    await assistant_runs.wait_idle()
    writes = len(world.ledger.writes("update"))
    assert writes == 2

    again = await client.post(f"{API}/{batch_id}/apply", headers=headers, json={"approved_ids": ids})
    assert again.status_code == 200, again.text
    assert again.json()["run"]["id"] == first.json()["run"]["id"] and again.json()["run"]["status"] == "done"
    assert again.json()["run"]["applied"] == ids
    await assistant_runs.wait_idle()
    assert len(world.ledger.writes("update")) == writes
    assert len((await db.scalars(select(AssistantRun))).all()) == 1

    # Also while the run is still working: the retry after a cut connection finds its own run.
    other_batch, other_ids, _ = await edits(client, world, 2)
    slow = SlowFirefly(world)
    other_headers = {**ADMIN, "Idempotency-Key": "apply-2"}
    started = await client.post(f"{API}/{other_batch}/apply", headers=other_headers, json={"approved_ids": other_ids})
    await slow.inside()
    retry = await client.post(f"{API}/{other_batch}/apply", headers=other_headers, json={"approved_ids": other_ids})
    assert retry.status_code == 200 and retry.json()["run"]["id"] == started.json()["run"]["id"]
    assert retry.json()["run"]["status"] == "running"
    slow.release()
    await assistant_runs.wait_idle()
    assert len(world.ledger.writes("update")) == writes + 2


async def test_another_key_while_a_run_is_active_is_a_409_with_that_run(client, world, db):
    batch_id, ids, _ = await edits(client, world, 2)
    slow = SlowFirefly(world)
    first = await client.post(f"{API}/{batch_id}/apply", headers={**ADMIN, "Idempotency-Key": "one"}, json={"approved_ids": ids})
    assert first.status_code == 202
    await slow.inside()

    second = await client.post(f"{API}/{batch_id}/apply", headers={**ADMIN, "Idempotency-Key": "two"}, json={"approved_ids": ids})
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["code"] == "apply_running" and isinstance(body["detail"], str)
    assert body["run"]["id"] == first.json()["run"]["id"] and body["run"]["status"] == "running"
    keyless = await start_apply(client, batch_id, ids)
    assert keyless.status_code == 409 and keyless.json()["code"] == "apply_running"

    # Reverting something that is being applied is refused too.
    revert = await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)
    assert revert.status_code == 409 and revert.json()["code"] == "apply_running"

    slow.release()
    state = await finish(client, batch_id)
    assert state["run"]["status"] == "done" and state["run"]["applied"] == ids
    assert len((await db.scalars(select(AssistantRun))).all()) == 1  # the refused requests created nothing

    # Once it ended a new key is a new run (nothing left to write).
    third = await start_apply(client, batch_id, ids, headers={**ADMIN, "Idempotency-Key": "three"})
    assert third.status_code == 202 and third.json()["run"]["id"] != first.json()["run"]["id"]
    await assistant_runs.wait_idle()


async def test_progress_is_visible_in_the_get_while_firefly_is_slow(client, world):
    batch_id, ids, _ = await edits(client, world, 3)
    slow = SlowFirefly(world, hold=2)
    response = await start_apply(client, batch_id, ids)
    assert response.status_code == 202
    await slow.inside()

    mid = await batch_state(client, batch_id)
    run = mid["run"]
    assert run["status"] == "running" and run["finished_at"] is None
    assert (run["applied"], run["failed"], run["pending"]) == (ids[:1], [], ids[1:])
    assert [by_id(mid, pid)["status"] for pid in ids] == ["applied", "applying", "proposed"]
    assert mid["status"] == "open"  # the batch closes with the run, not before

    slow.release()
    done = await finish(client, batch_id)
    assert done["run"]["status"] == "done" and done["run"]["applied"] == ids and done["run"]["pending"] == []
    assert done["status"] == "applied"


async def test_a_failed_proposal_whose_effect_reached_firefly_ends_applied_on_retry(client, world, monkeypatch):
    ref = world.movement()
    batch_id = (await open_batch(client))["id"]
    proposal = await act(client, batch_id, "ledger_edit", ref, {"category": "Ocio"})
    real = ledger.edit

    async def edit_then_lose_the_connection(*args, **kwargs):
        await real(*args, **kwargs)  # Firefly and the local ledger changed...
        raise RuntimeError("connection lost")  # ...but the answer never made it back

    monkeypatch.setattr(ledger, "edit", edit_then_lose_the_connection)
    first = (await apply(client, batch_id, [proposal["id"]])).json()
    assert [entry["id"] for entry in first["failed"]] == [proposal["id"]] and first["applied"] == []
    assert by_id(first["batch"], proposal["id"])["status"] == "failed"
    assert world.category_of(ref) == "Ocio" and len(world.ledger.writes("update")) == 1

    monkeypatch.setattr(ledger, "edit", real)
    retry = (await apply(client, batch_id, [proposal["id"]])).json()
    assert retry["failed"] == [] and retry["applied"] == [proposal["id"]], retry["failed"]
    stored = by_id(retry["batch"], proposal["id"])
    assert stored["status"] == "applied" and stored["error"] is None and "stale" not in str(retry)
    assert retry["batch"]["status"] == "applied" and len(world.ledger.writes("update")) == 1


async def test_a_cancelled_run_leaves_the_proposal_resumable_not_applying(client, world, db):
    batch_id, ids, refs = await edits(client, world, 2)
    slow = SlowFirefly(world)
    assert (await start_apply(client, batch_id, ids, headers={**ADMIN, "Idempotency-Key": "cut"})).status_code == 202
    await slow.inside()
    assert (await batch_state(client, batch_id))["proposals"][0]["status"] == "applying"

    tasks = list(assistant_runs._tasks)
    assert len(tasks) == 1
    tasks[0].cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert tasks[0].cancelled()

    state = await batch_state(client, batch_id)
    assert [by_id(state, pid)["status"] for pid in ids] == ["failed", "proposed"]
    assert by_id(state, ids[0])["error"] == assistant.INTERRUPTED and by_id(state, ids[0])["applied_at"] is None
    assert state["run"]["status"] == "failed" and state["run"]["finished_at"] is not None and state["run"]["pending"] == []
    assert {entry["id"] for entry in state["run"]["failed"]} == set(ids)
    assert world.category_of(refs[0]) == "Alimentación" and not world.ledger.writes("update")
    entry = (await db.scalars(select(Audit).where(Audit.action == "assistant.apply"))).one()
    assert entry.detail["status"] == "failed" and entry.detail["idempotency_key"] == "cut"

    # Nothing is locked: approving again right away (no 10-minute wait) finishes the job.
    retry = (await apply(client, batch_id, ids)).json()
    assert retry["failed"] == [] and retry["applied"] == ids and retry["batch"]["status"] == "applied"
    assert all(world.category_of(ref) == "Ocio" for ref in refs)


async def test_a_run_orphaned_by_a_restart_becomes_failed_and_can_be_applied_again(client, world, db):
    batch_id, ids, refs = await edits(client, world, 3)
    # The previous process died with the first proposal claimed and the run still "running".
    claimed = await db.get(Proposal, ids[0])
    claimed.status, claimed.applied_at = "applying", now_utc()
    db.add(AssistantRun(batch_id=batch_id, status="running", idempotency_key="before-restart", approved_ids=ids[:2], applied=[], failed=[], discarded=[], started_at=now_utc() - timedelta(minutes=1)))
    await db.commit()
    assert (await batch_state(client, batch_id))["run"]["status"] == "running"

    assert await assistant_runs.recover_orphans(db) == 1
    assert await assistant_runs.recover_orphans(db) == 0

    state = await batch_state(client, batch_id)
    run = state["run"]
    assert run["status"] == "failed" and run["finished_at"] is not None and run["pending"] == []
    assert run["failed"] == [{"id": pid, "detail": assistant_runs.RESTARTED} for pid in ids[:2]]
    assert (by_id(state, ids[0])["status"], by_id(state, ids[0])["error"]) == ("failed", assistant_runs.RESTARTED)
    assert by_id(state, ids[1])["status"] == "proposed" and by_id(state, ids[2])["status"] == "proposed"
    entry = (await db.scalars(select(Audit).where(Audit.action == "assistant.apply"))).one()
    assert entry.detail["status"] == "failed" and entry.detail["idempotency_key"] == "before-restart"

    # The batch is no longer blocked and every proposal can be approved again, with a new key or none.
    retry = (await apply(client, batch_id, ids, headers={**ADMIN, "Idempotency-Key": "after-restart"})).json()
    assert retry["failed"] == [] and retry["applied"] == ids and retry["batch"]["status"] == "applied"
    assert all(world.category_of(ref) == "Ocio" for ref in refs)


async def test_fifty_ledger_edits_end_applied_through_a_run(client, world):
    batch_id, ids, refs = await edits(client, world, 50)
    response = await start_apply(client, batch_id, ids, headers={**ADMIN, "Idempotency-Key": "fifty"})
    assert response.status_code == 202 and response.json()["run"]["pending"] == ids
    state = await finish(client, batch_id)
    assert state["run"]["status"] == "done" and state["run"]["applied"] == ids and state["run"]["failed"] == [] and state["run"]["pending"] == []
    assert state["status"] == "applied" and {proposal["status"] for proposal in state["proposals"]} == {"applied"}
    assert all(world.category_of(ref) == "Ocio" for ref in refs) and len(world.ledger.writes("update")) == 50


async def test_validation_still_answers_before_any_run_and_discarding_is_immediate(client, world, db):
    batch_id, ids, _ = await edits(client, world, 2)
    risky = await act(client, batch_id, "ledger_delete", world.movement("Viejo", hours=5))
    assert (await start_apply(client, batch_id, [])).status_code == 422
    assert (await start_apply(client, batch_id, [999])).json()["code"] == "validation_error"
    together = await start_apply(client, batch_id, [ids[0], risky["id"]])
    assert together.status_code == 422 and together.json()["code"] == "risky_requires_single"
    assert (await start_apply(client, 999, ids)).status_code == 404
    assert (await batch_state(client, batch_id))["run"] is None
    assert (await db.scalars(select(AssistantRun))).all() == []

    # Discarding needs no waiting for the run: the rest is discarded when the request answers.
    response = await start_apply(client, batch_id, [ids[0]], discard_others=True)
    assert response.status_code == 202
    discarded = {proposal["id"]: proposal["status"] for proposal in response.json()["batch"]["proposals"]}
    assert discarded[ids[1]] == "discarded" and discarded[risky["id"]] == "discarded"
    await assistant_runs.wait_idle()
    state = await batch_state(client, batch_id)
    assert state["status"] == "applied" and state["run"]["applied"] == ids[:1]
    audit = (await db.scalars(select(Audit).where(Audit.action == "assistant.apply"))).one()
    assert audit.detail["discarded"] == [ids[1], risky["id"]] and audit.detail["applied"] == ids[:1]
