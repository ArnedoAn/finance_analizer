"""Background apply of an assistant batch (``POST /assistant/batches/{id}/apply`` answers 202 with a run).

Firefly is slow and the reverse proxy gives up on a request long before a big batch finishes, so the request only
validates and records a run; the work happens in a task of this process with its own database session and its own
``Processor`` (same pattern as ``server_reset``). The run lives in the table ``assistant_runs`` and is updated after
every proposal, so ``GET /assistant/batches/{id}`` shows the progress live and survives a restart: a ``running`` run
found at startup lost its task and is marked ``failed`` with its unfinished proposals ready to be approved again.

The audit entry ``assistant.apply`` is written when the run ends, with the ``Idempotency-Key``. Exactly one run per batch
may be ``running``; the same key on the same batch always answers the run it created.
"""
import asyncio
from collections.abc import Callable
from functools import partial

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models_assistant import AssistantRun, Proposal
from app.services import assistant, audit
from app.services.processor import Processor

logger = get_logger(__name__)

RESTARTED = "El servidor se reinició; aplica de nuevo"

# Strong references: the loop only keeps weak ones, and a collected task would silently stop applying.
_tasks: set[asyncio.Task] = set()
_start_lock = asyncio.Lock()


class ApplyRunningError(Exception):
    """Another run of the batch is still ``running``."""

    def __init__(self, run: AssistantRun):
        super().__init__("A run of this batch is still applying")
        self.run = run


def public(run: AssistantRun) -> dict:
    """The ``Run`` of the contract. ``pending`` is what the run has not reached yet."""
    reached = {*run.applied, *(entry["id"] for entry in run.failed)}
    return {
        "id": run.id, "status": run.status, "approved_ids": list(run.approved_ids), "applied": list(run.applied),
        "failed": list(run.failed), "pending": [pid for pid in run.approved_ids if pid not in reached],
        "started_at": run.started_at, "finished_at": run.finished_at,
    }


async def latest(session, batch_id: int) -> AssistantRun | None:
    query = select(AssistantRun).where(AssistantRun.batch_id == batch_id).order_by(AssistantRun.id.desc()).limit(1).execution_options(populate_existing=True)
    return await session.scalar(query)


async def _by_key(session, batch_id: int, key: str) -> AssistantRun | None:
    query = select(AssistantRun).where(AssistantRun.batch_id == batch_id, AssistantRun.idempotency_key == key).execution_options(populate_existing=True)
    return await session.scalar(query)


async def start(
    session, processor: Processor, session_factory, batch_id: int, *, approved_ids: list[int], discard_others: bool, principal, idempotency_key: str | None = None,
) -> tuple[AssistantRun, Callable[[], None] | None]:
    """Validate and record a run. Returns ``(run, launch)``; ``launch()`` starts the work and is None when the key already had a run.

    ``AssistantError`` for what ``assistant.prepare_apply`` rejects, ``ApplyRunningError`` when a different request is
    still applying the batch. The caller composes its response first and then calls ``launch()``, so the response always
    shows the run as it was created, whatever the task does next.
    """
    async with _start_lock:
        await assistant.get_batch(session, batch_id)
        if idempotency_key and (known := await _by_key(session, batch_id, idempotency_key)) is not None:
            return known, None
        ids = await assistant.prepare_apply(session, batch_id, approved_ids=approved_ids, discard_others=discard_others)
        if (running := await assistant.active_run(session, batch_id)) is not None:
            raise ApplyRunningError(running)
        discarded = await assistant.discard_rest(session, batch_id, ids) if discard_others else []
        run = AssistantRun(batch_id=batch_id, status="running", idempotency_key=idempotency_key, approved_ids=ids, applied=[], failed=[], discarded=discarded, started_at=now_utc())
        session.add(run)
        await session.commit()
    run_id = run.id

    def launch() -> None:
        task = asyncio.get_running_loop().create_task(_runner(session_factory, processor, run_id, batch_id, ids, principal))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    return run, launch


async def wait_idle() -> None:
    """Wait until no run is working (tests, and nothing else)."""
    while _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)


async def shutdown() -> None:
    """Stop the runs at server shutdown; the proposal in flight stays ``failed`` and can be approved again."""
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _record(session, run_id: int, proposal_id: int, error: str | None) -> None:
    run = await session.get(AssistantRun, run_id, populate_existing=True)
    if error is None:
        run.applied = [*run.applied, proposal_id]
    else:
        run.failed = [*run.failed, {"id": proposal_id, "detail": error}]
    await session.commit()


async def _audit(session, run: AssistantRun, principal) -> None:
    detail = {"run_id": run.id, "status": run.status, "applied": run.applied, "failed": [entry["id"] for entry in run.failed], "discarded": run.discarded}
    if run.idempotency_key:
        detail["idempotency_key"] = run.idempotency_key
    await audit.record(session, action="assistant.apply", target_type="batch", target_id=run.batch_id, detail=detail, principal=principal)


async def _close(session, run: AssistantRun, status: str, principal, *, detail: str | None = None) -> None:
    """End ``run``: whatever it did not reach is reported as failed with ``detail``, the batch status is refreshed and the audit entry written."""
    if detail is not None:
        reached = {*run.applied, *(entry["id"] for entry in run.failed)}
        run.failed = [*run.failed, *({"id": pid, "detail": detail} for pid in run.approved_ids if pid not in reached)]
    run.status, run.finished_at = status, now_utc()
    await assistant.finish_batch(session, run.batch_id)
    await _audit(session, run, principal)
    await session.commit()


async def _runner(session_factory, processor: Processor, run_id: int, batch_id: int, ids: list[int], principal) -> None:
    try:
        async with session_factory() as session:
            worker = Processor(session, processor.firefly, processor.llm, settings=processor.settings)
            await assistant.apply_proposals(worker, ids, principal, partial(_record, session, run_id))
            await _close(session, await session.get(AssistantRun, run_id, populate_existing=True), "done", principal)
    except BaseException as exc:
        logger.error("assistant_run_failed", run_id=run_id, error_type=type(exc).__name__)
        await _abort(session_factory, run_id, principal)
        if not isinstance(exc, Exception):
            raise  # a cancellation must reach the task


async def _abort(session_factory, run_id: int, principal) -> None:
    try:
        async with session_factory() as session:
            await session.rollback()
            run = await session.get(AssistantRun, run_id, populate_existing=True)
            if run is not None and run.status == "running":
                await _close(session, run, "failed", principal, detail=assistant.INTERRUPTED)
    except Exception as exc:
        logger.error("assistant_run_abort_failed", run_id=run_id, error_type=type(exc).__name__)


async def recover_orphans(session) -> int:
    """At startup: a ``running`` run lost its task with the previous process. It becomes ``failed`` and its proposals stay approvable."""
    runs = list((await session.scalars(select(AssistantRun).where(AssistantRun.status == "running"))).all())
    for run in runs:
        proposals = (await session.scalars(select(Proposal).where(Proposal.id.in_(run.approved_ids), Proposal.status == "applying").execution_options(populate_existing=True))).all()
        for proposal in proposals:
            proposal.status, proposal.error, proposal.applied_at = "failed", RESTARTED, None
        await _close(session, run, "failed", None, detail=RESTARTED)
        logger.warning("assistant_run_orphaned", run_id=run.id, batch_id=run.batch_id)
    await session.commit()
    return len(runs)
