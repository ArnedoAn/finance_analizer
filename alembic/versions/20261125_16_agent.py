"""Long-task agent (tasks and steps) and the weekly coach tips (v4).

Revision ID: 20261125_16
Revises: 20261125_15
"""
import sqlalchemy as sa
from alembic import op

revision = "20261125_16"
down_revision = "20261125_15"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False, server_default="owner"),
        sa.Column("instruction", sa.Text(), nullable=False, server_default=""),
        sa.Column("month", sa.String(7)),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("run_now", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("window_from", timestamp, nullable=False),
        sa.Column("window_to", timestamp, nullable=False),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("proposal_batches.id")),
        sa.Column("job_id", sa.Integer()),
        sa.Column("summary", sa.Text()),
        sa.Column("report", sa.Text()),
        sa.Column("error_code", sa.String(40)),
        sa.Column("error", sa.String(300)),
        sa.Column("max_calls", sa.Integer(), nullable=False, server_default=sa.text("60")),
        sa.Column("calls_used", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("steps_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("requested_by", sa.String(100), nullable=False, server_default="system"),
        sa.Column("device_id", sa.Integer()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("started_at", timestamp),
        sa.Column("finished_at", timestamp),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    op.create_index("ix_agent_tasks_kind", "agent_tasks", ["kind"])
    op.create_index("ix_agent_tasks_status", "agent_tasks", ["status"])
    op.create_index("ix_agent_tasks_created_at", "agent_tasks", ["created_at"])
    op.create_table(
        "agent_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(40), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.UniqueConstraint("task_id", "n", name="uq_agent_steps_task_n"),
    )
    op.create_index("ix_agent_steps_task_id", "agent_steps", ["task_id"])
    op.create_table(
        "coach_tips",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("week", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("suggestion", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(100)),
        sa.Column("useful", sa.Boolean()),
        sa.Column("feedback_at", timestamp),
        sa.Column("created_at", timestamp, nullable=False),
    )
    op.create_index("ix_coach_tips_week", "coach_tips", ["week"])
    op.create_index("ix_coach_tips_kind", "coach_tips", ["kind"])


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
