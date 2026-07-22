"""CLI coverage for restart-safe M12 workflow inspection."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from schemabridge.entrypoints.cli.main import app

runner = CliRunner()


def test_workflow_demo_start_then_show_restores_same_pause(tmp_path: Path) -> None:
    environment = {
        "DATABASE_URL": (
            "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge"
        ),
        "SCHEMABRIDGE_DRAFT_STORE_PATH": str(tmp_path / "workflow.db"),
    }
    started = runner.invoke(
        app,
        [
            "workflow-demo",
            "--action",
            "start",
            "--workflow-id",
            "cli-workflow",
            "--json",
        ],
        env=environment,
    )
    assert started.exit_code == 0, started.stdout
    started_payload = json.loads(started.stdout)

    restored = runner.invoke(
        app,
        [
            "workflow-demo",
            "--action",
            "show",
            "--workflow-id",
            "cli-workflow",
            "--json",
        ],
        env=environment,
    )
    assert restored.exit_code == 0, restored.stdout
    restored_payload = json.loads(restored.stdout)

    assert restored_payload == started_payload
    assert restored_payload["draft"]["stage"] == "decision_required"
    assert restored_payload["draft"]["checkpoint"]["kind"] == "intent_confirmation"
    assert restored_payload["publication_adapter"] == "fake:local-idempotency-only"
    assert '"sql"' not in restored.stdout.casefold()
