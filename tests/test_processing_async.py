"""
Tests for async processing jobs (polling contract).
"""

import pytest
from fastapi import HTTPException

from app.api.dependencies import RequestSessionContext
from app.api.endpoints.processing import get_processing_job_status
from app.db.repositories import ProcessingJobRepository


@pytest.mark.asyncio
class TestProcessingJobs:
    """Contract tests for async processing job lifecycle."""

    async def test_job_lifecycle_queued_running_completed(self, db_session) -> None:
        repo = ProcessingJobRepository(db_session, session_id="jobSess111")
        job = await repo.create(request_payload={"max_emails": 10, "dry_run": False})
        await db_session.commit()

        queued = await repo.get_by_id(job.id)
        assert queued is not None
        assert queued.status == "queued"

        await repo.mark_running(job.id)
        await db_session.commit()
        running = await repo.get_by_id(job.id)
        assert running is not None
        assert running.status == "running"
        assert running.started_at is not None

        await repo.mark_completed(
            job.id,
            result_payload={
                "total_emails": 1,
                "processed": 1,
                "created": 1,
                "skipped": 0,
                "failed": 0,
                "dry_run": False,
                "results": [],
                "processing_time_ms": 120,
            },
        )
        await db_session.commit()
        completed = await repo.get_by_id(job.id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.completed_at is not None
        assert completed.result_payload is not None

    async def test_job_lifecycle_failed(self, db_session) -> None:
        repo = ProcessingJobRepository(db_session, session_id="jobFail111")
        job = await repo.create(request_payload={"max_emails": 5})
        await db_session.commit()

        await repo.mark_failed(job.id, "boom")
        await db_session.commit()

        failed = await repo.get_by_id(job.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_message == "boom"
        assert failed.completed_at is not None

    async def test_job_status_isolation_by_session(self, db_session) -> None:
        owner_repo = ProcessingJobRepository(db_session, session_id="ownerS111")
        other_repo = ProcessingJobRepository(db_session, session_id="otherS111")
        job = await owner_repo.create(request_payload={"max_emails": 1})
        await db_session.commit()

        owner_ctx = RequestSessionContext(session_id="ownerS111", is_new=False)
        owner_view = await get_processing_job_status(job.id, owner_ctx, db_session)
        assert owner_view.job_id == job.id
        assert owner_view.session_id == "ownerS111"

        other_ctx = RequestSessionContext(session_id="otherS111", is_new=False)
        with pytest.raises(HTTPException) as exc:
            await get_processing_job_status(job.id, other_ctx, db_session)
        assert exc.value.status_code == 404

        # Sanity check that other session cannot retrieve the record directly.
        hidden = await other_repo.get_by_id(job.id)
        assert hidden is None
