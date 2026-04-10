"""Initial migration — create all tables

Revision ID: 0001
Revises: None
Create Date: 2026-04-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), unique=True, index=True, nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("is_superuser", sa.Boolean(), default=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), unique=True, nullable=False),
        sa.Column("default_phone", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("api_key", sa.String(64), unique=True, nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "tracking_numbers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("phone", sa.String(20), unique=True, index=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id"),
            nullable=True,
        ),
        sa.Column("number_type", sa.String(20), default="dynamic"),
        sa.Column("source_label", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("freeze_time", sa.Integer(), default=900),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "visitor_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column(
            "tracking_number_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracking_numbers.id"),
            nullable=True,
        ),
        sa.Column("client_id", sa.String(64), index=True, nullable=False),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("source", sa.String(255), nullable=True),
        sa.Column("medium", sa.String(255), nullable=True),
        sa.Column("campaign", sa.String(255), nullable=True),
        sa.Column("keyword", sa.String(255), nullable=True),
        sa.Column("content", sa.String(255), nullable=True),
        sa.Column("gclid", sa.String(255), nullable=True),
        sa.Column("referrer", sa.Text(), nullable=True),
        sa.Column("landing_page", sa.Text(), nullable=True),
        sa.Column("extra_params", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_activity", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("visitor_sessions.id"),
            nullable=True,
        ),
        sa.Column("uniqueid", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column("caller_number", sa.String(20), index=True, nullable=False),
        sa.Column("tracking_did", sa.String(20), index=True, nullable=False),
        sa.Column("target_number", sa.String(20), nullable=True),
        sa.Column("answered_by", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration", sa.Integer(), default=0),
        sa.Column("billsec", sa.Integer(), default=0),
        sa.Column("disposition", sa.String(20), default="NO ANSWER"),
        sa.Column("is_unique", sa.Boolean(), default=False),
        sa.Column("is_target", sa.Boolean(), default=False),
        sa.Column("source", sa.String(255), nullable=True),
        sa.Column("medium", sa.String(255), nullable=True),
        sa.Column("campaign", sa.String(255), nullable=True),
        sa.Column("keyword", sa.String(255), nullable=True),
        sa.Column("recording_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Индексы для аналитики
    op.create_index("ix_calls_project_started", "calls", ["project_id", "started_at"])
    op.create_index("ix_calls_source", "calls", ["source"])
    op.create_index("ix_sessions_project_created", "visitor_sessions", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_sessions_project_created")
    op.drop_index("ix_calls_source")
    op.drop_index("ix_calls_project_started")
    op.drop_table("calls")
    op.drop_table("visitor_sessions")
    op.drop_table("tracking_numbers")
    op.drop_table("projects")
    op.drop_table("users")
