"""Long-task agent: ``POST/GET /agent/tasks``, ``POST /agent/tasks/{id}/cancel`` and ``GET/PUT /settings/agent``.

Errors are ``{"detail", "code"}``. The agent only reads and adds proposals to an assistant batch (see ``app.services.agent``).
"""
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB, Service
from app.services import agent, audit

router = APIRouter()


class TaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1, max_length=2000)
    month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    run_now: bool = False


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_calls: int = Field(ge=1, le=agent.MAX_CALLS_LIMIT)


@contextmanager
def _coded_errors():
    try:
        yield
    except agent.AgentError as exc:
        raise HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code}) from exc


@router.post("/agent/tasks", status_code=202)
async def create_task(body: TaskBody, db: DB, service: Service, principal: ReviewPrincipal):
    """Queue a long task. ``run_now`` starts it immediately in this server; otherwise it goes to the off-peak queue."""
    with _coded_errors():
        if await agent.active_count(db) >= agent.MAX_ACTIVE:
            raise agent.AgentError(409, "agent_busy", f"Ya hay {agent.MAX_ACTIVE} tareas del agente en curso; espera o cancela una.")
        task = await agent.create_task(db, service.settings, instruction=body.instruction, month=body.month, run_now=body.run_now, principal=principal)
        if not body.run_now:
            await agent.enqueue(db, task)
        await audit.record(
            db, action="agent.task_create", target_type="agent_task", target_id=task.id, principal=principal,
            detail={"month": task.month, "run_now": body.run_now, "instruction_length": len(task.instruction)},
        )
        await db.commit()
    if body.run_now:
        agent.launch(task.id, service.firefly, service.llm, service.settings)
    return {"id": task.id}


@router.get("/agent/tasks", dependencies=[READ])
async def list_tasks(
    db: DB, status: Literal["queued", "deferred", "running", "done", "failed", "cancelled"] | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
):
    tasks, total = await agent.listing(db, status=status, limit=limit, offset=offset)
    return {"items": [await agent.public(db, task, steps=False) for task in tasks], "total": total}


@router.get("/agent/tasks/{task_id}", dependencies=[READ])
async def get_task(task_id: int, db: DB):
    with _coded_errors():
        return await agent.public(db, await agent.get(db, task_id), steps=True)


@router.post("/agent/tasks/{task_id}/cancel")
async def cancel_task(task_id: int, db: DB, principal: ReviewPrincipal):
    with _coded_errors():
        task = await agent.cancel(db, task_id, principal)
        return await agent.public(db, task, steps=True)


@router.get("/settings/agent", dependencies=[READ])
async def get_policy(db: DB):
    return await agent.policy(db)


@router.put("/settings/agent")
async def put_policy(body: PolicyBody, db: DB, principal: ReviewPrincipal):
    previous = await agent.policy(db)
    current = await agent.set_policy(db, body.max_calls)
    await audit.record(
        db, action="settings.agent", target_type="settings", target_id="agent", principal=principal,
        detail={"max_calls": current["max_calls"], "previous": previous["max_calls"]},
    )
    await db.commit()
    return current
