"""ServerQuality: second opinion, peak/valley AI job queue, trips and events, forgotten subscriptions and credit cards.
The LLM and Firefly are simulated; the clock of the peak windows is replaced per test."""
import importlib.util
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import SecretStr
from sqlalchemy import select

from app.api.routers import ai_jobs as ai_jobs_router
from app.clients.llm import LLMClient
from app.core.timezone import now_utc
from app.db.models import AiEvent, AiJob, AiNote, AiUsage, OwnAccountRow
from app.db.models_alerts import Alert
from app.db.models_assistant import Audit
from app.services import (
    ai_budget,
    ai_events,
    ai_features,
    ai_forgotten,
    ai_insights,
    ai_jobs,
    cards,
    second_opinion,
)
from app.services import processor as pipeline
from tests.test_ai_insights_v4 import add_item, fake, llm  # noqa: F401  (fixtures)
from tests.test_devices_v4 import API, auth, enroll_key

ADMIN = auth("test-admin-key-01234567890123456789")
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261125_15_quality.py"
PEAK = datetime(2026, 11, 23, 2, 0, tzinfo=UTC)  # Monday 02:00 UTC (21:00 in Bogota): peak until 04:00
VALLEY = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_budget_settings(settings, monkeypatch):
    monkeypatch.setattr(ai_budget, "get_settings", lambda: settings)


def clock(monkeypatch, moment):
    monkeypatch.setattr(ai_budget, "_clock", lambda: moment)


# --- Peak windows ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("moment,peak", [
    (datetime(2026, 11, 23, 0, 59, tzinfo=UTC), False), (datetime(2026, 11, 23, 1, 0, tzinfo=UTC), True),
    (datetime(2026, 11, 23, 3, 59, tzinfo=UTC), True), (datetime(2026, 11, 23, 4, 0, tzinfo=UTC), False),
    (datetime(2026, 11, 23, 5, 59, tzinfo=UTC), False), (datetime(2026, 11, 23, 6, 0, tzinfo=UTC), True),
    (datetime(2026, 11, 23, 9, 59, tzinfo=UTC), True), (datetime(2026, 11, 23, 10, 0, tzinfo=UTC), False),
    (datetime(2026, 11, 27, 7, 0, tzinfo=UTC), True), (datetime(2026, 11, 28, 2, 0, tzinfo=UTC), False),  # Friday / Saturday
    (datetime(2026, 11, 22, 7, 0, tzinfo=UTC), False),  # Sunday
])
def test_is_peak_windows(moment, peak):
    assert ai_budget.is_peak(moment) is peak


def test_defer_until_offpeak_and_peak_info():
    assert ai_budget.defer_until_offpeak(PEAK) == VALLEY
    assert ai_budget.defer_until_offpeak(VALLEY) is None
    assert ai_budget.peak_ends_at(datetime(2026, 11, 23, 7, 30, tzinfo=UTC)) == datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    info = ai_budget.peak_info(PEAK)
    assert info == {"now": True, "ends_at": VALLEY.isoformat(), "next_start": datetime(2026, 11, 23, 6, 0, tzinfo=UTC).isoformat()}
    sunday = datetime(2026, 11, 22, 15, 0, tzinfo=UTC)
    assert ai_budget.peak_info(sunday) == {"now": False, "ends_at": None, "next_start": datetime(2026, 11, 23, 1, 0, tzinfo=UTC).isoformat()}
    friday_night = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
    assert ai_budget.next_peak_start(friday_night) == datetime(2026, 11, 30, 1, 0, tzinfo=UTC)


async def test_usage_report_exposes_peak(client, monkeypatch):
    clock(monkeypatch, PEAK)
    body = (await client.get(f"{API}/ai/usage", headers=ADMIN)).json()
    assert body["peak"] == {"now": True, "ends_at": VALLEY.isoformat(), "next_start": datetime(2026, 11, 23, 6, 0, tzinfo=UTC).isoformat()}
    clock(monkeypatch, VALLEY)
    assert (await client.get(f"{API}/ai/usage", headers=ADMIN)).json()["peak"]["now"] is False


# --- peak_policy -----------------------------------------------------------------------------------------------------------


async def test_peak_policy_defaults_validates_and_is_audited(client, db):
    first = (await client.get(f"{API}/settings/ai-budget", headers=ADMIN)).json()
    assert first == {"daily_calls": 400, "peak_policy": "ask", "rolling_stop_percent": 60.0, "weekly_stop_percent": 70.0}
    response = await client.put(f"{API}/settings/ai-budget", headers=ADMIN, json={"peak_policy": "wait"})
    assert response.status_code == 200 and response.json()["peak_policy"] == "wait" and response.json()["daily_calls"] == 400
    again = await client.put(f"{API}/settings/ai-budget", headers=ADMIN, json={"daily_calls": 10})
    assert again.json()["daily_calls"] == 10 and again.json()["peak_policy"] == "wait"
    for bad in ({}, {"peak_policy": "sometimes"}, {"daily_calls": -1}, {"extra": 1}):
        assert (await client.put(f"{API}/settings/ai-budget", headers=ADMIN, json=bad)).status_code == 422
    entries = list((await db.scalars(select(Audit).where(Audit.action == "settings.ai_budget").order_by(Audit.id))).all())
    assert entries[0].detail == {"daily_calls": 400, "previous": 400, "peak_policy": "wait", "previous_peak_policy": "ask"}
    assert entries[1].detail == {"daily_calls": 10, "previous": 400}


# --- Job queue -------------------------------------------------------------------------------------------------------------


@pytest.fixture
def runner():
    calls = []

    @ai_jobs.register("test_runner")
    async def run(ctx, payload):
        calls.append(payload)
        return {"ran": len(calls), "payload": payload}

    @ai_jobs.register("test_boom")
    async def boom(ctx, payload):
        raise ValueError("secret amount 123456")

    @ai_jobs.register("test_noop")
    async def noop(ctx, payload):
        return {"skipped": "nothing_to_do"}

    return calls


@pytest.fixture
def ctx(db, clients, settings):
    return ai_jobs.JobContext(db, clients[0], clients[1], settings)


@pytest.fixture
def kicked(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_jobs_router, "kick", lambda: calls.append(1))
    return calls


async def jobs(db):
    return list((await db.scalars(select(AiJob).order_by(AiJob.id))).all())


async def test_valley_runs_inline_and_a_noop_leaves_no_row(db, ctx, runner):
    result = await ai_jobs.dispatch(ctx, "k1", "Trabajo", "test_runner", {"a": 1}, estimated_calls=3)
    assert result == {"ran": 1, "payload": {"a": 1}}
    (job,) = await jobs(db)
    assert (job.status, job.decided_by, job.result["ran"], job.finished_at is not None) == ("done", "policy", 1, True)
    assert await ai_jobs.dispatch(ctx, "k2", "Nada", "test_noop") == {"skipped": "nothing_to_do"}
    assert len(await jobs(db)) == 1


async def test_peak_with_ask_parks_the_job_with_estimates_and_one_grouped_alert(db, ctx, runner, monkeypatch):
    clock(monkeypatch, PEAK)
    first = await ai_jobs.dispatch(ctx, "categorize_batch", "Categorizar", "test_runner", estimated_calls=10)
    second = await ai_jobs.dispatch(ctx, "budget_assign", "Presupuestos", "test_runner", estimated_calls=5)
    assert first["deferred"] and first["run_after"] == VALLEY.isoformat() and second["deferred"]
    assert runner == []
    a, b = await jobs(db)
    assert (a.status, a.decided_by, a.run_after) == ("waiting_offpeak", None, VALLEY)
    assert a.estimated_calls == 10 and a.estimated_allowance_pct == ai_jobs.estimate_pct(10)
    public = ai_jobs.public(a, PEAK)
    assert public["estimated_allowance_pct_now"] == round(a.estimated_allowance_pct * 2, 3) and public["model"] == "deepseek-v4.1-flash"
    assert ai_jobs.public(a, VALLEY)["estimated_allowance_pct_now"] == a.estimated_allowance_pct
    # nothing runs and the alert waits for the grace period so one cycle's jobs are counted together
    await ai_jobs.run_due(ctx)
    assert runner == [] and not (await db.scalars(select(Alert))).all()
    for job in (a, b):
        job.created_at = now_utc() - timedelta(minutes=5)
    await db.commit()
    assert (await ai_jobs.run_due(ctx))["alerted"] is True
    (alert,) = (await db.scalars(select(Alert))).all()
    assert alert.type == "ai_jobs_waiting" and alert.options == ["Ejecutar ahora"]
    assert alert.title.startswith("2 trabajos de IA esperando la hora valle (ahorro ~") and "%" in alert.title
    assert (await ai_jobs.run_due(ctx))["alerted"] is False
    assert len((await db.scalars(select(Alert))).all()) == 1


async def test_peak_wait_policy_parks_silently_and_run_policy_runs_now(db, ctx, runner, monkeypatch):
    clock(monkeypatch, PEAK)
    await ai_budget.set_policy(db, peak_policy="wait")
    await db.commit()
    assert (await ai_jobs.dispatch(ctx, "k1", "Uno", "test_runner"))["deferred"]
    (job,) = await jobs(db)
    assert (job.status, job.decided_by) == ("waiting_offpeak", "policy")
    job.created_at = now_utc() - timedelta(minutes=5)
    await db.commit()
    assert (await ai_jobs.run_due(ctx))["alerted"] is False and not (await db.scalars(select(Alert))).all()
    await ai_budget.set_policy(db, peak_policy="run")
    await db.commit()
    assert (await ai_jobs.dispatch(ctx, "k2", "Dos", "test_runner"))["ran"] == 1
    # «run» also releases what was already waiting on the next tick (the owner did not choose to wait)
    assert (await ai_jobs.run_due(ctx))["ran"] == [job.id]
    await db.refresh(job)
    assert job.status == "done"


async def test_waiting_jobs_run_by_themselves_when_the_valley_comes_one_per_kind(db, ctx, runner, monkeypatch):
    clock(monkeypatch, PEAK)
    for number in range(2):
        await ai_jobs.enqueue(db, "same", f"Trabajo {number}", 1, "test_runner", {"n": number})
    await ai_jobs.enqueue(db, "other", "Otro", 1, "test_runner", {"n": 9})
    await db.commit()
    assert (await ai_jobs.run_due(ctx))["ran"] == []
    clock(monkeypatch, VALLEY)
    first = await ai_jobs.run_due(ctx)
    assert len(first["ran"]) == 2  # one of each kind
    assert (await ai_jobs.run_due(ctx))["ran"] and len(runner) == 3


async def test_enqueue_dedupes_active_jobs_and_a_surcharge_free_model_never_waits(db, monkeypatch):
    clock(monkeypatch, PEAK)
    one = await ai_jobs.enqueue(db, "k", "Uno", 1, "test_runner", dedupe_key="same")
    two = await ai_jobs.enqueue(db, "k", "Dos", 1, "test_runner", dedupe_key="same")
    assert one.id == two.id
    cheap = await ai_jobs.enqueue(db, "k", "Barato", 1, "test_runner", model="mimo-v2.6-flash")
    assert cheap.status == "queued"


async def test_failed_runner_is_recorded_without_leaking_its_message(db, ctx, runner):
    assert await ai_jobs.dispatch(ctx, "k", "Falla", "test_boom") == {"error": "ValueError"}
    (job,) = await jobs(db)
    assert job.status == "failed" and job.error == "ValueError" and "123456" not in json.dumps(ai_jobs.public(job), default=str)
    missing = await ai_jobs.enqueue(db, "k", "Sin runner", 1, "no_such_runner")
    await db.commit()
    await ai_jobs.run_job(ctx, missing)
    await db.refresh(missing)
    assert missing.status == "failed" and missing.error == "LookupError"


async def test_reap_marks_interrupted_runs_failed(db):
    job = AiJob(kind="k", title="t", runner_key="x", payload={}, status="running", started_at=now_utc() - timedelta(hours=2))
    db.add(job)
    await db.commit()
    assert await ai_jobs.reap(db) == 1
    await db.refresh(job)
    assert (job.status, job.error) == ("failed", "interrupted")


async def test_jobs_api_list_run_now_wait_cancel_and_audit(client, db, ctx, runner, kicked, monkeypatch):
    clock(monkeypatch, PEAK)
    ids = [(await ai_jobs.dispatch(ctx, f"k{n}", f"Trabajo {n}", "test_runner", {"n": n}, estimated_calls=2))["deferred"] for n in range(4)]
    listing = (await client.get(f"{API}/ai/jobs", headers=ADMIN)).json()
    assert listing["total"] == 4 and listing["peak"] == {"now": True, "ends_at": VALLEY.isoformat(), "next_start": datetime(2026, 11, 23, 6, 0, tzinfo=UTC).isoformat()}
    item = listing["items"][-1]
    assert set(item) == {
        "id", "kind", "title", "status", "created_at", "updated_at", "estimated_calls", "estimated_allowance_pct",
        "estimated_allowance_pct_now", "model", "run_after", "decided_by", "started_at", "finished_at", "result", "error",
    }
    assert item["status"] == "waiting_offpeak" and item["run_after"].startswith("2026-11-23T04:00:00")
    assert (await client.get(f"{API}/ai/jobs?status=done", headers=ADMIN)).json()["items"] == []
    assert (await client.get(f"{API}/ai/jobs?status=bogus", headers=ADMIN)).status_code == 422

    # run now: the job is released as the owner's decision and the background tick (kicked) runs it paying the peak price
    response = await client.post(f"{API}/ai/jobs/{ids[0]}/run-now", headers=ADMIN)
    assert response.status_code == 200 and response.json()["status"] == "queued" and response.json()["decided_by"] == "owner" and kicked == [1]
    await ai_jobs.run_due(ctx)
    assert (await db.get(AiJob, ids[0], populate_existing=True)).status == "done" and runner == [{"n": 0}]

    # wait: goes back to the valley as the owner's decision (and «run» policy no longer releases it)
    await client.post(f"{API}/ai/jobs/{ids[1]}/run-now", headers=ADMIN)
    waited = (await client.post(f"{API}/ai/jobs/{ids[1]}/wait", headers=ADMIN)).json()
    assert waited["status"] == "waiting_offpeak" and waited["decided_by"] == "owner"
    await ai_budget.set_policy(db, peak_policy="run")
    await db.commit()
    assert set((await ai_jobs.run_due(ctx))["ran"]) == {ids[2], ids[3]}
    assert (await db.get(AiJob, ids[1], populate_existing=True)).status == "waiting_offpeak"

    # cancel
    cancelled = (await client.post(f"{API}/ai/jobs/{ids[1]}/cancel", headers=ADMIN)).json()
    assert cancelled["status"] == "cancelled" and cancelled["finished_at"]
    again = await client.post(f"{API}/ai/jobs/{ids[1]}/cancel", headers=ADMIN)
    assert again.status_code == 409 and again.json()["code"] == "job_not_actionable"
    assert (await client.post(f"{API}/ai/jobs/{ids[0]}/run-now", headers=ADMIN)).json()["code"] == "job_not_actionable"
    missing = await client.post(f"{API}/ai/jobs/9999/cancel", headers=ADMIN)
    assert missing.status_code == 404 and missing.json()["code"] == "job_not_found"
    actions = {entry.action for entry in (await db.scalars(select(Audit).where(Audit.action.startswith("ai_job.")))).all()}
    assert actions == {"ai_job.run_now", "ai_job.wait", "ai_job.cancel"}


async def test_run_all_now_wait_outside_peak_and_scopes(client, db, ctx, runner, kicked, monkeypatch):
    clock(monkeypatch, PEAK)
    for number in range(3):
        await ai_jobs.dispatch(ctx, f"k{number}", f"Trabajo {number}", "test_runner")
    assert (await client.post(f"{API}/ai/jobs/run-all-now", headers=ADMIN)).json() == {"queued": 3}
    assert {job.decided_by for job in await jobs(db)} == {"owner"} and kicked == [1]
    assert (await client.post(f"{API}/ai/jobs/run-all-now", headers=ADMIN)).json() == {"queued": 0}
    assert len(kicked) == 1
    await ai_jobs.run_due(ctx)
    assert {job.status for job in await jobs(db)} == {"done"}
    clock(monkeypatch, VALLEY)
    queued = await ai_jobs.enqueue(db, "k", "Valle", 1, "test_runner")
    await db.commit()
    response = await client.post(f"{API}/ai/jobs/{queued.id}/wait", headers=ADMIN)
    assert response.status_code == 409 and response.json()["code"] == "not_peak"
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    assert (await client.get(f"{API}/ai/jobs", headers=reader)).status_code == 200
    for path in (f"/ai/jobs/{queued.id}/cancel", f"/ai/jobs/{queued.id}/run-now", f"/ai/jobs/{queued.id}/wait", "/ai/jobs/run-all-now"):
        assert (await client.post(API + path, headers=reader)).status_code == 403
        assert (await client.post(API + path)).status_code == 401


async def test_cancelled_jobs_never_run(db, ctx, runner, monkeypatch):
    clock(monkeypatch, PEAK)
    job = await ai_jobs.enqueue(db, "k", "Cancelable", 1, "test_runner")
    await ai_jobs.cancel(db, job.id)
    await db.commit()
    clock(monkeypatch, VALLEY)
    assert (await ai_jobs.run_due(ctx))["ran"] == [] and runner == []
    with pytest.raises(ai_jobs.JobError):
        await ai_jobs.cancel(db, job.id)


async def test_post_cycle_steps_wait_in_the_peak_and_run_in_the_valley(db, clients, settings, fake, llm, monkeypatch):  # noqa: F811
    await add_item(db, fake, "t1", merchant="SUPERMERCADO CENTRAL")
    llm.complete_json.return_value = {"items": []}
    clock(monkeypatch, PEAK)
    results = await ai_insights.after_cycle(db, clients[0], llm, settings)
    assert results["categorize"]["deferred"] and llm.complete_json.await_count == 0
    waiting = {job.kind for job in await jobs(db) if job.status == "waiting_offpeak"}
    assert {"categorize_batch", "budget_assign"} <= waiting
    again = await ai_insights.after_cycle(db, clients[0], llm, settings)  # the same job is not parked twice
    assert again["categorize"]["deferred"] == results["categorize"]["deferred"]
    clock(monkeypatch, VALLEY)
    await ai_jobs.run_due(ai_jobs.JobContext(db, clients[0], llm, settings))
    assert llm.complete_json.await_count >= 1
    assert (await db.get(AiJob, results["categorize"]["deferred"], populate_existing=True)).status == "done"


# --- Second opinion --------------------------------------------------------------------------------------------------------


async def sms_item(service, **changes):
    values = dict(
        source="sms", source_ref=str(uuid4()), sender="85954", raw_text="Pagaste $3.000.000 en SUPERMERCADO CENTRAL",
        subject_or_title="Compra", occurred_at=datetime.now(UTC), status="pending",
    )
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


def opinions(clients, extraction, **second):
    """The first extraction is the parser's (patched ``parse``); the second model answers with ``second`` changes."""
    def answer(text, cycle_id=None, usage=None, model=None):
        if usage is not None:
            usage["calls"] = usage.get("calls", 0) + 1
        return replace(extraction, template_id=f"llm:{model}", **second)
    clients[1].analyze.side_effect = answer


async def enable_opinion(db, **fields):
    await ai_features.update(db, second_opinion={"enabled": True, **fields})
    await db.commit()


async def test_second_opinion_agreement_verifies_and_raises_the_cap(service, db, clients, extraction, monkeypatch):
    big = replace(extraction, amount=Decimal("3000000.00"))
    monkeypatch.setattr(pipeline, "parse", lambda *a, **k: big)
    await enable_opinion(db)
    opinions(clients, big)
    item = await sms_item(service)
    done = await service.process(item)
    assert done.status == "created" and done.review_reason is None  # 3M is over the 2M cap but verified (cap 4M)
    assert done.second_opinion == {"status": "verified", "model": "mimo-v2.6-flash", "fields": [], "reason": None}
    assert clients[1].analyze.await_args.kwargs["model"] == "mimo-v2.6-flash"
    usage = (await db.scalars(select(AiUsage).where(AiUsage.kind == "second_opinion"))).one()
    assert (usage.calls, usage.auto_calls) == (1, 1)


async def test_verified_amount_above_the_raised_cap_still_goes_to_review(service, db, clients, extraction, monkeypatch):
    huge = replace(extraction, amount=Decimal("4500000.00"))
    monkeypatch.setattr(pipeline, "parse", lambda *a, **k: huge)
    await enable_opinion(db)
    opinions(clients, huge)
    done = await service.process(await sms_item(service))
    assert done.status == "needs_review" and "amount over auto-approval limit" in done.review_reason
    assert done.second_opinion["status"] == "verified"
    await enable_opinion(db, verified_max_amount_cop="5000000")
    other = await service.process(await sms_item(service))
    assert other.status == "created"


@pytest.mark.parametrize("field,change,label", [
    ("amount", {"amount": Decimal("2500000.00")}, "monto"), ("kind", {"kind": "transfer", "counterparty_hint": "Maria"}, "tipo"),
    ("merchant", {"merchant": "OTRO COMERCIO"}, "comercio"), ("account", {"account_hint": "9999"}, "cuenta"),
])
async def test_second_opinion_disagreement_sends_the_item_to_review(service, db, clients, extraction, add_own_account, field, change, label, monkeypatch):
    big = replace(extraction, amount=Decimal("3000000.00"))
    monkeypatch.setattr(pipeline, "parse", lambda *a, **k: big)
    await add_own_account("Bancolombia", last4=["9999"], sources=["sms:85954"])
    await enable_opinion(db)
    opinions(clients, big, **change)
    done = await service.process(await sms_item(service, sender="85954"))
    assert done.status == "needs_review"
    assert done.second_opinion["status"] == "disagree" and label in done.second_opinion["fields"]
    assert "segunda opinión en desacuerdo: " in done.review_reason and label in done.review_reason


async def test_second_opinion_only_for_llm_or_parser_above_the_minimum(service, db, clients, extraction):
    await enable_opinion(db)  # default minimum 1.000.000: the 12.000 parser extraction is not checked
    opinions(clients, extraction)
    done = await service.process(await sms_item(service, raw_text="Pagaste $12.000 en SUPERMERCADO CENTRAL"))
    assert done.status == "created" and done.second_opinion is None and clients[1].analyze.await_count == 0
    await enable_opinion(db, min_amount_cop="0")
    again = await service.process(await sms_item(service, raw_text="Pagaste $12.000 en SUPERMERCADO CENTRAL"))
    assert again.second_opinion["status"] == "verified" and clients[1].analyze.await_count == 1


async def test_second_opinion_after_an_llm_extraction_even_for_small_amounts(service, db, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **k: None)
    await enable_opinion(db)
    opinions(clients, extraction)
    first = replace(extraction, template_id="llm:test")
    calls = []

    def answer(text, cycle_id=None, usage=None, model=None):
        calls.append(model)
        return replace(first, template_id=f"llm:{model}")
    clients[1].analyze.side_effect = answer
    done = await service.process(await sms_item(service, raw_text="Pagaste $12.000 en SUPERMERCADO CENTRAL"))
    assert calls == [None, "mimo-v2.6-flash"] and done.second_opinion["status"] == "verified"


async def test_second_opinion_braked_or_failing_keeps_the_normal_cap(service, db, clients, extraction, monkeypatch):
    big = replace(extraction, amount=Decimal("3000000.00"))
    monkeypatch.setattr(pipeline, "parse", lambda *a, **k: big)
    await enable_opinion(db)
    opinions(clients, big)
    await ai_budget.set_policy(db, daily_calls=0)
    await db.commit()
    braked = await service.process(await sms_item(service))
    assert braked.status == "needs_review" and braked.second_opinion is None and "amount over auto-approval limit" in braked.review_reason
    assert "segunda opinión" not in braked.review_reason and clients[1].analyze.await_count == 0
    await ai_budget.set_policy(db, daily_calls=400)
    await db.commit()
    clients[1].analyze.side_effect = RuntimeError("down")
    failed = await service.process(await sms_item(service))
    assert failed.status == "needs_review" and failed.second_opinion is None and "segunda opinión" not in failed.review_reason


async def test_second_opinion_disabled_never_calls_and_reevaluation_keeps_the_verdict(service, db, clients, extraction, monkeypatch):
    big = replace(extraction, amount=Decimal("3000000.00"))
    monkeypatch.setattr(pipeline, "parse", lambda *a, **k: big)
    opinions(clients, big)
    off = await service.process(await sms_item(service))
    assert off.second_opinion is None and clients[1].analyze.await_count == 0
    await enable_opinion(db)
    opinions(clients, big, amount=Decimal("2500000.00"))
    item = await service.process(await sms_item(service))
    assert item.status == "needs_review" and clients[1].analyze.await_count == 1
    await service.process(item, reevaluate=True)
    assert clients[1].analyze.await_count == 1  # the verdict taken is kept, the provider is not asked again


def test_second_opinion_helpers():
    assert second_opinion.verified_cap({"verified_max_amount_cop": None}, Decimal("2000000")) == Decimal("4000000")
    assert second_opinion.verified_cap({"verified_max_amount_cop": "3000000.00"}, Decimal("2000000")) == Decimal("3000000.00")
    assert second_opinion.verified_cap({"verified_max_amount_cop": "100.00"}, Decimal("2000000")) == Decimal("2000000")


async def test_second_opinion_settings_round_trip_validation_and_audit(client, db):
    got = (await client.get(f"{API}/settings/ai-features", headers=ADMIN)).json()["second_opinion"]
    assert got == {"enabled": False, "model": "mimo-v2.6-flash", "min_amount_cop": "1000000.00", "verified_max_amount_cop": None}  # tests start with it off
    saved = await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"second_opinion": {
        "enabled": True, "model": "other-model-1", "min_amount_cop": "500000", "verified_max_amount_cop": "6000000",
    }})
    assert saved.status_code == 200, saved.text
    assert saved.json()["second_opinion"] == {"enabled": True, "model": "other-model-1", "min_amount_cop": "500000.00", "verified_max_amount_cop": "6000000.00"}
    reset = await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"second_opinion": {"verified_max_amount_cop": None}})
    assert reset.json()["second_opinion"]["verified_max_amount_cop"] is None and reset.json()["second_opinion"]["model"] == "other-model-1"
    for bad in ({"model": "qwen-contributor"}, {"model": "big-pickle-free"}, {"model": "bad model!"}):
        response = await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"second_opinion": bad})
        assert response.status_code == 422 and response.json()["code"] == "invalid_second_opinion"
    for bad in ({"min_amount_cop": "-1"}, {"verified_max_amount_cop": "0"}, {"min_amount_cop": "1.001"}, {"extra": 1}, {"enabled": "maybe"}):
        assert (await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"second_opinion": bad})).status_code == 422
    entry = (await db.scalars(select(Audit).where(Audit.action == "settings.ai_features").order_by(Audit.id))).first()
    assert entry.detail["second_opinion"]["model"] == "other-model-1"


async def test_llm_client_model_per_call_uses_exactly_that_model_and_collects_usage():
    from types import SimpleNamespace

    config = SimpleNamespace(
        llm_base_url="https://llm.test/v1", llm_api_key=SecretStr("k"), llm_models="a,b", llm_user_agent="x",
        llm_fallback_base_url="https://fb.test/v1", llm_fallback_api_key=SecretStr("f"), llm_fallback_model="fb",
    )
    client = LLMClient(config)
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload["model"])
        body = {"kind": "withdrawal", "amount": 5000, "merchant": "TIENDA"}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(body)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        usage: dict = {}
        result = await client.analyze("Pagaste $5.000 en TIENDA", "c", usage=usage, model="mimo-v2.6-flash")
        assert seen == ["mimo-v2.6-flash"] and result.template_id == "llm:mimo-v2.6-flash"
        assert usage["model"] == "mimo-v2.6-flash" and usage["total_tokens"] == 15 and usage["calls"] == 1
        await client.complete_json("p", {"UNTRUSTED_DATA": "x"}, "c", model="other")
        assert seen[-1] == "other"
        await client.analyze("Pagaste $5.000 en TIENDA", "c")
        assert seen[-1] == "a"
    finally:
        await client.close()


# --- Trips and events ------------------------------------------------------------------------------------------------------


async def seed_trip(db, fake, city="MEDELLIN", start_age=10):  # noqa: F811
    return [await add_item(db, fake, f"trip-{city}-{n}", merchant=f"{place} {city}", age_days=start_age - n)
            for n, place in enumerate(("HOTEL POBLADO", "RESTAURANTE EL CIELO", "TAXI"))]


async def test_trip_detection_label_apply_undo_and_never_proposed_twice(client, db, clients, settings, fake, llm):  # noqa: F811
    trip = await seed_trip(db, fake)
    await add_item(db, fake, "home-1", merchant="TIENDA LOCAL", age_days=20)
    llm.complete_json.return_value = {"items": []}
    assert await ai_events.run(db, llm, settings) == {"proposed": 1}
    (row,) = (await db.scalars(select(AiEvent))).all()
    start = min(item.occurred_at.astimezone(ZoneInfo(settings.timezone)).date() for item in trip)
    assert (row.kind, row.city, row.status, row.name_source) == ("trip", "medellin", "pending", "rule")
    assert row.label == f"viaje-medellin-{start:%Y-%m}" and sorted(row.item_ids) == sorted(item.id for item in trip)
    assert row.total_amount == Decimal("36000.00")
    sent = llm.complete_json.await_args.args[1]["UNTRUSTED_DATA"][0]
    assert sent["city"] == "medellin" and sent["movements"] == 3 and "amount" not in sent

    listing = (await client.get(f"{API}/ai/events", headers=ADMIN)).json()
    assert listing["total"] == 1
    body = listing["items"][0]
    assert set(body) == {
        "id", "status", "kind", "label", "city", "start_date", "end_date", "item_ids", "count", "total_amount", "currency", "name_source",
        "reason", "group_id", "undone", "created_at", "applied_at",
    }
    assert (body["count"], body["total_amount"], body["currency"], body["status"]) == (3, "36000.00", "COP", "pending")

    assert await ai_events.run(db, llm, settings) == {"skipped": "nothing_to_do"}  # already proposed
    applied = await client.post(f"{API}/ai/events/{row.id}/apply", headers=ADMIN, json={"label": "Viaje Medellín Familia"})
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied" and applied.json()["label"] == "viaje-medellin-familia" and applied.json()["group_id"]
    for item in trip:
        assert "viaje-medellin-familia" in fake.journals(item.firefly_transaction_id)[0]["tags"]
    again = await client.post(f"{API}/ai/events/{row.id}/apply", headers=ADMIN)
    assert again.status_code == 409 and again.json()["code"] == "event_not_pending"
    undo = await client.post(f"{API}/ledger/groups/{applied.json()['group_id']}/undo", headers=ADMIN)
    assert undo.status_code == 200, undo.text
    assert (await client.get(f"{API}/ai/events?status=applied", headers=ADMIN)).json()["items"][0]["undone"] is True
    assert await ai_events.run(db, llm, settings) == {"skipped": "nothing_to_do"}  # an undo is respected
    entry = await db.scalar(select(Audit).where(Audit.action == "ai_event.apply"))
    assert entry.detail["label"] == "viaje-medellin-familia" and entry.detail["items"] == 3


async def test_event_naming_by_the_model_is_validated_and_discard(client, db, settings, fake, llm):  # noqa: F811
    await seed_trip(db, fake, city="CARTAGENA")
    llm.complete_json.return_value = {"items": [{"id": 1, "label": "Viaje Cartagena Mayo"}]}
    await ai_events.run(db, llm, settings)
    row = await db.scalar(select(AiEvent))
    assert row.label == "viaje-cartagena-mayo" and row.name_source == "llm"
    bad = await client.post(f"{API}/ai/events/{row.id}/apply", headers=ADMIN, json={"label": "x"})
    assert bad.status_code == 422
    discarded = await client.post(f"{API}/ai/events/{row.id}/discard", headers=ADMIN)
    assert discarded.json()["status"] == "discarded"
    assert (await client.post(f"{API}/ai/events/{row.id}/discard", headers=ADMIN)).json()["code"] == "event_not_pending"
    assert (await client.post(f"{API}/ai/events/9999/apply", headers=ADMIN)).json()["code"] == "event_not_found"


async def test_invalid_model_label_and_budget_stop_keep_rule_label(db, settings, fake, llm):  # noqa: F811
    await seed_trip(db, fake, city="CALI")
    llm.complete_json.return_value = {"items": [{"id": 1, "label": "!!"}]}
    await ai_events.run(db, llm, settings)
    row = await db.scalar(select(AiEvent))
    assert row.name_source == "rule" and row.label.startswith("viaje-cali-")
    await db.delete(row)
    await db.commit()
    await ai_budget.set_policy(db, daily_calls=0)
    await db.commit()
    assert await ai_events.run(db, llm, settings) == {"skipped": "budget"}


async def test_home_city_and_loose_spending_are_not_trips_and_tags_switch(db, settings, fake, llm):  # noqa: F811
    for n in range(12):
        await add_item(db, fake, f"home-{n}", merchant="EXITO BOGOTA", age_days=200 + n)
    for n in range(3):
        await add_item(db, fake, f"away-{n}", merchant="TIENDA BOGOTA", age_days=5 + n)  # home city: no trip
    await add_item(db, fake, "one", merchant="HOTEL SALENTO", age_days=7)  # one movement: no trip
    assert await ai_events.detect(db, settings) == []
    await ai_features.update(db, features={"tags": False})
    await db.commit()
    assert await ai_events.run(db, llm, settings) == {"skipped": "disabled"}


async def test_event_of_new_merchants_without_city(db, settings, fake, llm):  # noqa: F811
    await add_item(db, fake, "old", merchant="EXITO", age_days=100)
    for n, merchant in enumerate(("LICORES UNO", "SALON DE FIESTAS", "PASTELERIA DOS", "DECORACION TRES")):
        await add_item(db, fake, f"party-{n}", merchant=merchant, age_days=6, firefly=True)
    found = await ai_events.detect(db, settings)
    assert len(found) == 1 and found[0]["kind"] == "event" and found[0]["city"] is None and len(found[0]["items"]) == 4
    assert found[0]["label"].startswith("evento-")


async def test_stale_proposal_when_movements_leave(client, db, settings, fake, llm):  # noqa: F811
    trip = await seed_trip(db, fake, city="PEREIRA")
    llm.complete_json.return_value = {"items": []}
    await ai_events.run(db, llm, settings)
    for item in trip:
        item.status = "voided"
    await db.commit()
    assert (await client.get(f"{API}/ai/events", headers=ADMIN)).json()["total"] == 0
    assert (await client.get(f"{API}/ai/events?status=stale", headers=ADMIN)).json()["total"] == 1


async def test_events_scopes(client, db):
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    assert (await client.get(f"{API}/ai/events", headers=reader)).status_code == 200
    assert (await client.post(f"{API}/ai/events/1/apply", headers=reader)).status_code == 403
    assert (await client.post(f"{API}/ai/events/1/discard", headers=reader)).status_code == 403
    assert (await client.get(f"{API}/ai/events")).status_code == 401


# --- Forgotten subscriptions -----------------------------------------------------------------------------------------------


async def subscription(db, fake, merchant="NETFLIX", amount="30000.00", category="Ocio", verdict=True):  # noqa: F811
    for age in (95, 65, 35, 5):
        await add_item(db, fake, f"{merchant}-{age}", merchant=merchant, category=category, amount=amount, age_days=age)
    db.add(AiNote(kind="subscription", key=merchant.casefold(), text="", data={"verdict": "subscription" if verdict else "other", "is_subscription": verdict}))
    await db.commit()


async def forgotten_alerts(db):
    return list((await db.scalars(select(Alert).where(Alert.type == "forgotten_subscription"))).all())


async def test_forgotten_subscription_alert_with_monthly_and_yearly_totals_once(db, settings, fake, llm):  # noqa: F811
    await subscription(db, fake)
    assert await ai_forgotten.run(db, llm, settings) == {"checked": 1, "alerts": 1}
    (alert,) = await forgotten_alerts(db)
    assert alert.amount == Decimal("30000.00") and alert.currency == "COP"
    assert "$30.000 COP al mes" in alert.body and "$360.000 COP al año" in alert.body and "NETFLIX" in alert.title
    assert llm.complete_json.await_count == 0  # no doubtful relation: no model call
    assert (await ai_forgotten.run(db, llm, settings))["alerts"] == 0 and len(await forgotten_alerts(db)) == 1


async def test_related_activity_of_the_same_merchant_silences_the_alert(db, settings, fake, llm):  # noqa: F811
    await subscription(db, fake)
    await add_item(db, fake, "extra", merchant="NETFLIX", category="Ocio", amount="12000.00", age_days=10)
    assert (await ai_forgotten.run(db, llm, settings))["alerts"] == 0 and llm.complete_json.await_count == 0


async def test_doubtful_relation_is_judged_by_the_cheap_model(db, settings, fake, llm):  # noqa: F811
    await subscription(db, fake)
    await add_item(db, fake, "cine", merchant="CINE COLOMBIA", category="Ocio", amount="40000.00", age_days=10)
    llm.complete_json.return_value = {"related": True}
    assert (await ai_forgotten.run(db, llm, settings))["alerts"] == 0
    kwargs = llm.complete_json.await_args.kwargs
    assert kwargs["model"] == "mimo-v2.6-flash"
    evidence = llm.complete_json.await_args.args[1]["UNTRUSTED_DATA"]
    assert evidence["subscription"] == "NETFLIX" and evidence["other_merchants"] == ["CINE COLOMBIA"]
    usage = (await db.scalars(select(AiUsage).where(AiUsage.kind == "forgotten_subscriptions"))).one()
    assert (usage.calls, usage.auto_calls) == (1, 1)
    llm.complete_json.return_value = {"related": False}
    assert (await ai_forgotten.run(db, llm, settings))["alerts"] == 1


async def test_unavailable_or_braked_judge_means_no_alert(db, settings, fake, llm):  # noqa: F811
    await subscription(db, fake)
    await add_item(db, fake, "cine", merchant="CINE COLOMBIA", category="Ocio", amount="40000.00", age_days=10)
    llm.complete_json.side_effect = RuntimeError("down")
    assert (await ai_forgotten.run(db, llm, settings))["alerts"] == 0
    llm.complete_json.side_effect = None
    llm.complete_json.return_value = {"related": False}
    await ai_budget.set_policy(db, daily_calls=0)
    await db.commit()
    before = llm.complete_json.await_count
    assert (await ai_forgotten.run(db, llm, settings))["alerts"] == 0 and llm.complete_json.await_count == before


async def test_only_confirmed_subscriptions_and_feature_switch(db, settings, fake, llm):  # noqa: F811
    await subscription(db, fake, verdict=False)
    assert await ai_forgotten.run(db, llm, settings) == {"checked": 0, "alerts": 0}
    await ai_features.update(db, features={"subscriptions": False})
    await db.commit()
    assert await ai_forgotten.run(db, llm, settings) == {"skipped": "disabled"}


async def test_forgotten_job_goes_through_the_queue_even_in_the_peak(db, clients, settings, fake, llm, monkeypatch):  # noqa: F811
    await subscription(db, fake)
    clock(monkeypatch, PEAK)  # the judge model has no surcharge: the job never waits
    result = await ai_forgotten.run_monthly(db, llm, settings)
    assert result == {"checked": 1, "alerts": 1}
    (job,) = await jobs(db)
    assert (job.kind, job.status) == ("forgotten_subscriptions", "done")


def test_monthly_amount_scales_other_cadences():
    assert ai_forgotten.monthly_amount({"amount": "30000.00", "period_days": 30}) == Decimal("30000.00")
    assert ai_forgotten.monthly_amount({"amount": "120000.00", "period_days": 365}) == Decimal("10006.85")
    assert ai_forgotten.monthly_amount({"amount": "10000.00", "period_days": 7}) == Decimal("43482.14")


# --- Credit cards ----------------------------------------------------------------------------------------------------------


def test_card_date_helpers_clamp_to_the_month():
    assert cards.at_day(2026, 2, 31) == date(2026, 2, 28)
    assert cards.next_on_or_after(date(2026, 11, 24), 24) == date(2026, 11, 24)
    assert cards.next_on_or_after(date(2026, 11, 25), 24) == date(2026, 12, 24)
    assert cards.next_on_or_after(date(2026, 1, 30), 31) == date(2026, 1, 31)
    assert cards.next_on_or_after(date(2026, 2, 1), 31) == date(2026, 2, 28)
    assert cards.next_on_or_after(date(2026, 12, 20), 5) == date(2027, 1, 5)
    assert cards.previous_before(date(2026, 3, 31), 31) == date(2026, 2, 28)
    assert cards.previous_before(date(2026, 1, 15), 20) == date(2025, 12, 20)
    assert cards.payment_for(date(2026, 12, 15), 28) == date(2026, 12, 28)
    assert cards.payment_for(date(2026, 12, 15), 10) == date(2027, 1, 10)
    assert cards.payment_for(date(2026, 12, 15), 15) == date(2027, 1, 15)  # strictly after the cut


@pytest.fixture
def card_world(clients, add_own_account):
    firefly = clients[0]
    firefly.accounts.return_value = [*firefly.accounts.return_value, {"id": "9", "name": "Tarjeta A", "type": "liabilities"}]
    firefly.account_transactions.return_value = [
        {"source_id": "9", "type": "withdrawal", "amount": "100000.00"}, {"source_id": "9", "type": "withdrawal", "amount": "150000.00"},
        {"source_id": "1", "destination_id": "9", "type": "transfer", "amount": "999.00"},  # a payment: not a charge
    ]
    return firefly


async def own_cards(add_own_account):
    await add_own_account("Tarjeta A", bank="nu", kind="credit", statement_day=15, payment_day=28)
    await add_own_account("Tarjeta B", bank="lulo", kind="credit", statement_day=25, payment_day=10)
    await add_own_account("Tarjeta C", bank="x", kind="credit")


async def test_cards_advice_per_card_and_best_to_use(client, db, card_world, add_own_account):
    await own_cards(add_own_account)
    body = (await client.get(f"{API}/cards/advice?date=2026-11-24", headers=ADMIN)).json()
    assert body["date"] == "2026-11-24" and [card["account"] for card in body["items"]] == ["Tarjeta A", "Tarjeta B", "Tarjeta C"]
    a, b, c = body["items"]
    assert a == {
        "account": "Tarjeta A", "bank": "nu", "statement_day": 15, "payment_day": 28, "next_statement": "2026-12-15", "next_payment": "2026-11-28",
        "interest_free_days": 34, "period_spent": "250000.00", "recommended": True,
    }
    assert (b["next_statement"], b["next_payment"], b["interest_free_days"], b["recommended"]) == ("2026-11-25", "2026-12-10", 16, False)
    assert b["period_spent"] is None  # Firefly has no account of that name
    assert c == {
        "account": "Tarjeta C", "bank": "x", "statement_day": None, "payment_day": None, "next_statement": None, "next_payment": None,
        "interest_free_days": None, "period_spent": None, "recommended": False,
    }
    assert body["recommended"] == {"account": "Tarjeta A", "interest_free_days": 34}
    # the day after Tarjeta B's cut it becomes the better card
    after = (await client.get(f"{API}/cards/advice?date=2026-11-26", headers=ADMIN)).json()
    assert after["recommended"]["account"] == "Tarjeta B" and after["recommended"]["interest_free_days"] == 45


async def test_cards_advice_without_cards_default_date_errors_and_firefly_down(client, db, card_world, add_own_account):
    empty = (await client.get(f"{API}/cards/advice", headers=ADMIN)).json()
    assert empty["items"] == [] and empty["recommended"] is None and date.fromisoformat(empty["date"])
    for bad in ("2026-13-01", "hoy", "2026-02-30"):
        response = await client.get(f"{API}/cards/advice?date={bad}", headers=ADMIN)
        assert response.status_code == 422 and response.json()["code"] == "invalid_date"
    await own_cards(add_own_account)
    card_world.accounts.side_effect = RuntimeError("down")
    body = (await client.get(f"{API}/cards/advice?date=2026-11-24", headers=ADMIN)).json()
    assert body["items"][0]["period_spent"] is None and body["items"][0]["interest_free_days"] == 34
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    assert (await client.get(f"{API}/cards/advice", headers=reader)).status_code == 200
    assert (await client.get(f"{API}/cards/advice")).status_code == 401


async def test_card_payment_alerts_three_and_one_day_before_once(db, card_world, add_own_account):
    await own_cards(add_own_account)

    async def alerts():
        return list((await db.scalars(select(Alert).where(Alert.type == "card_payment_due").order_by(Alert.id))).all())

    assert await cards.alert_check(db, card_world, date(2026, 11, 24)) == 0  # 4 days
    assert await cards.alert_check(db, card_world, date(2026, 11, 25)) == 1  # A in 3 days
    await db.commit()
    (three,) = await alerts()
    assert three.title == "Pago de Tarjeta A en 3 días" and three.amount == Decimal("250000.00")
    assert "Saldo del periodo: $250.000 COP" in three.body and "28/11/2026" in three.body
    assert await cards.alert_check(db, card_world, date(2026, 11, 25)) == 0  # not repeated
    assert await cards.alert_check(db, card_world, date(2026, 11, 26)) == 0
    assert await cards.alert_check(db, card_world, date(2026, 11, 27)) == 1  # tomorrow
    await db.commit()
    assert (await alerts())[-1].title == "Pago de Tarjeta A mañana"
    assert await cards.alert_check(db, card_world, date(2026, 12, 7)) == 1  # B in 3 days, no balance (not in Firefly)
    await db.commit()
    last = (await alerts())[-1]
    assert last.title.startswith("Pago de Tarjeta B") and last.amount is None and "Saldo del periodo" not in last.body


async def test_card_alerts_are_part_of_the_processor_checks(service, db, card_world, add_own_account, monkeypatch):
    await own_cards(add_own_account)
    monkeypatch.setattr("app.services.processor._now", lambda: datetime(2026, 11, 25, 17, 0, tzinfo=UTC))
    await service.alerts()
    assert (await db.scalar(select(Alert).where(Alert.type == "card_payment_due"))) is not None


async def test_own_accounts_card_days_crud_and_validation(client, db, card_world):
    created = await client.post(f"{API}/own-accounts", headers=ADMIN, json={
        "firefly_account": "Tarjeta A", "bank": "nu", "kind": "credit", "statement_day": 15, "payment_day": 28,
    })
    assert created.status_code == 201, created.text
    account = created.json()
    assert (account["statement_day"], account["payment_day"]) == (15, 28)
    plain = (await client.get(f"{API}/own-accounts", headers=ADMIN)).json()["items"]
    nequi = next(row for row in plain if row["firefly_account"] == "Nequi")
    assert nequi["statement_day"] is None and nequi["payment_day"] is None
    only_one = await client.patch(f"{API}/own-accounts/{account['id']}", headers=ADMIN, json={"payment_day": 5})
    assert (only_one.json()["statement_day"], only_one.json()["payment_day"]) == (15, 5)
    cleared = await client.patch(f"{API}/own-accounts/{account['id']}", headers=ADMIN, json={"statement_day": None})
    assert cleared.status_code == 200 and cleared.json()["statement_day"] is None and cleared.json()["payment_day"] == 5
    for bad in (0, 32, "5", 1.5, True):
        response = await client.patch(f"{API}/own-accounts/{account['id']}", headers=ADMIN, json={"payment_day": bad})
        assert response.status_code == 422 and response.json()["code"] == "validation_error"
    assert (await client.patch(f"{API}/own-accounts/{account['id']}", headers=ADMIN, json={"bank": None})).status_code == 422
    # only credit cards carry the days
    wrong = await client.post(f"{API}/own-accounts", headers=ADMIN, json={
        "firefly_account": "Bancolombia", "bank": "bancolombia", "kind": "savings", "statement_day": 3,
    })
    assert wrong.status_code == 422 and wrong.json()["code"] == "card_days_not_credit"
    nequi_patch = await client.patch(f"{API}/own-accounts/{nequi['id']}", headers=ADMIN, json={"payment_day": 3})
    assert nequi_patch.status_code == 422 and nequi_patch.json()["code"] == "card_days_not_credit"
    # leaving the credit kind clears the days
    moved = await client.patch(f"{API}/own-accounts/{account['id']}", headers=ADMIN, json={"kind": "checking"})
    assert moved.status_code == 200 and (moved.json()["statement_day"], moved.json()["payment_day"]) == (None, None)
    row = await db.get(OwnAccountRow, account["id"], populate_existing=True)
    assert (row.kind, row.statement_day, row.payment_day) == ("checking", None, None)


# --- Migration -------------------------------------------------------------------------------------------------------------


def test_migration_matches_the_models_and_is_chained():
    spec = importlib.util.spec_from_file_location("quality_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("20261125_15", "20261125_14")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE inbox_items (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text("CREATE TABLE own_accounts (id INTEGER PRIMARY KEY)"))
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()
    inspector = sa.inspect(engine)
    for model in (AiJob, AiEvent):
        assert {c["name"] for c in inspector.get_columns(model.__tablename__)} == {c.name for c in model.__table__.columns}
    assert {"second_opinion"} <= {c["name"] for c in inspector.get_columns("inbox_items")}
    assert {"statement_day", "payment_day"} <= {c["name"] for c in inspector.get_columns("own_accounts")}
    with pytest.raises(RuntimeError):
        module.downgrade()


def test_quality_routes_declare_their_scopes():
    from app.main import app
    from tests.test_route_scopes import api_routes, route_scopes

    scopes = {(route.path, tuple(sorted(route.methods))): route_scopes(route) for route in api_routes(app)}
    expected = {
        ("/api/v1/ai/jobs", ("GET",)): ["read"], ("/api/v1/ai/jobs/run-all-now", ("POST",)): ["review"],
        ("/api/v1/ai/jobs/{job_id}/run-now", ("POST",)): ["review"], ("/api/v1/ai/jobs/{job_id}/wait", ("POST",)): ["review"],
        ("/api/v1/ai/jobs/{job_id}/cancel", ("POST",)): ["review"], ("/api/v1/ai/events", ("GET",)): ["read"],
        ("/api/v1/ai/events/{event_id}/apply", ("POST",)): ["review"], ("/api/v1/ai/events/{event_id}/discard", ("POST",)): ["review"],
        ("/api/v1/cards/advice", ("GET",)): ["read"],
    }
    for key, value in expected.items():
        assert scopes.get(key) == value, key
