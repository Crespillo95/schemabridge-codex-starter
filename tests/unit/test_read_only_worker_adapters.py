from __future__ import annotations

import pytest

from schemabridge.adapters.workflows.read_only import (
    DisabledWorkflowPublisher,
    ReadOnlyWorkflowInspector,
)
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.domain.workflows import WorkflowOperation


def test_read_only_worker_publisher_fails_before_any_external_effect() -> None:
    publisher = DisabledWorkflowPublisher()

    with pytest.raises(WorkflowError) as failure:
        publisher.publish(object(), object())  # type: ignore[arg-type]

    assert failure.value.code is WorkflowErrorCode.PUBLICATION_FAILED
    assert "credential" not in str(failure.value).casefold()


class _EmptyWorkflowStore:
    def load(self, workflow_id: str) -> None:
        del workflow_id
        return None

    def save(self, draft: object, *, expected_revision: int | None) -> None:
        raise AssertionError((draft, expected_revision))


def test_api_workflow_inspector_cannot_mutate_or_recover() -> None:
    inspector = ReadOnlyWorkflowInspector(_EmptyWorkflowStore())  # type: ignore[arg-type]

    with pytest.raises(WorkflowError) as missing:
        inspector.inspect("workflow-safe")
    assert missing.value.code is WorkflowErrorCode.NOT_FOUND

    with pytest.raises(WorkflowError) as execution:
        inspector.decide_execution("workflow-safe", object())  # type: ignore[arg-type]
    assert execution.value.code is WorkflowErrorCode.INVALID_TRANSITION

    with pytest.raises(WorkflowError) as recovery:
        inspector.recover_interrupted(
            "workflow-safe",
            expected_operation=WorkflowOperation.PREVIEW_EXECUTION,
        )
    assert recovery.value.code is WorkflowErrorCode.INVALID_TRANSITION
