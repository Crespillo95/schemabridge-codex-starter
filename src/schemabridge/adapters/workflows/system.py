"""System clock adapter for workflow orchestration."""

from datetime import UTC, datetime


class SystemWorkflowClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
