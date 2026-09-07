"""Durable live retrieval jobs and encrypted, versioned source snapshots."""

from datetime import date, datetime

from sqlalchemy import UUID, Date, DateTime, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from mars.db.base import Base


class LiveSyncJob(Base):
    __tablename__ = "live_sync_job"
    __table_args__ = (
        Index("ix_live_sync_scope_period", "scope_key", "period_start", "period_end"),
        {"schema": "mars_analytics"},
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    job_status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    completed_steps: Mapped[int] = mapped_column(Integer, default=0)
    total_steps: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    checkpoint: Mapped[bytes | None] = mapped_column(LargeBinary)
    snapshot: Mapped[bytes | None] = mapped_column(LargeBinary)
    snapshot_status: Mapped[str | None] = mapped_column(String(32))
