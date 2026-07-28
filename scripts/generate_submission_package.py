"""Generate judge-readable M18 artifacts from the implemented synthetic release path."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from schemabridge.adapters.datahub.fake_writeback import FakeCatalogWriteAdapter
from schemabridge.adapters.storage.reviews import InMemoryReviewStore
from schemabridge.application.canonical_review import (
    DecideCanonicalMapping,
    PrepareCanonicalPublication,
    StartCanonicalReview,
)
from schemabridge.application.guided_requests import GuidedRequestCase, build_demo_guided_input
from schemabridge.application.review_demo import build_customer_review_draft
from schemabridge.bootstrap import (
    build_evaluation_runner,
    build_governed_request_executor,
    build_governed_request_preparer,
    build_guided_request_builder,
    build_semantic_planning_context,
)
from schemabridge.config import Settings
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.evaluation import EvaluationReport
from schemabridge.domain.resolution import RejectedSourceRecord
from schemabridge.domain.reviews import (
    PublicationApproval,
    PublicationConfirmation,
    mapping_target_id,
)
from schemabridge.domain.workflows import fingerprint_payload

_OUTPUT_NAMES = (
    "logical-model-customer.yml",
    "column-mapping-customer-key.yml",
    "join-contract-customer-account-holder.yml",
    "analytical-request-secondary-holders.yml",
    "resolved-query-plan-secondary-holders.yml",
    "generated-secondary-holders.sql",
    "query-validation-report.yml",
    "rejected-records.csv",
    "datahub-writeback.yml",
    "evaluation-summary.md",
    "manifest.json",
)


class SubmissionPackageError(RuntimeError):
    """The requested artifact set cannot be represented honestly."""


@dataclass(frozen=True, slots=True)
class ReleaseState:
    revision: str
    dirty: bool

    @property
    def release_ready(self) -> bool:
        return self.revision != "missing" and not self.dirty


def inspect_release_state(root: Path) -> ReleaseState:
    revision = subprocess.run(
        ("git", "rev-parse", "--verify", "HEAD"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=normal"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return ReleaseState(
        revision=revision.stdout.strip() if revision.returncode == 0 else "missing",
        dirty=revision.returncode != 0 or bool(status.stdout.strip()),
    )


def require_release_state(state: ReleaseState, *, allow_uncommitted: bool) -> None:
    if not state.release_ready and not allow_uncommitted:
        raise SubmissionPackageError(
            "submission artifacts require an existing clean HEAD; use --allow-uncommitted only "
            "for explicitly non-release development evidence"
        )


def build_core_artifacts(root: Path) -> dict[str, bytes]:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
            "SCHEMABRIDGE_MAX_QUERY_ROWS": 500,
            "SCHEMABRIDGE_STATEMENT_TIMEOUT_MS": 5000,
        }
    )
    validated = build_guided_request_builder(repository_root=root).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    preparer = build_governed_request_preparer(repository_root=root, settings=settings)
    prepared = preparer.execute(validated)
    result = build_governed_request_executor(
        execution_kind="recorded",
        repository_root=root,
        settings=settings,
        prepare=preparer,
    ).execute_prepared(prepared)
    context = build_semantic_planning_context(repository_root=root).load()

    customer = next(
        model for model in context.logical_context.models if model.id.root == "Customer"
    )
    customer_key_mappings = tuple(
        item
        for item in context.mapping_set.mappings
        if item.mapping.logical_field.root == "Customer.customer_key"
    )
    contract = next(
        item for item in context.join_contracts.contracts if item.id == "customer_to_account_holder"
    )
    publication = _canonical_publication_evidence()

    validation = {
        "artifact_kind": "recorded_observation_revalidated_by_current_compiler_and_guard",
        "catalog_mode": "recorded synthetic planning context",
        "source_mode": "recorded synthetic PostgreSQL observation",
        "sql_policy": {
            "status": result.policy_status,
            "findings": [
                {"code": finding.code.value, "message": finding.message}
                for finding in result.policy_findings
            ],
        },
        "query_fingerprint": fingerprint_payload(
            {
                "sql": result.sql,
                "parameters": list(result.parameters),
                "max_rows": prepared.query.max_rows,
                "statement_timeout_ms": prepared.query.statement_timeout_ms,
            }
        ),
        "parameters": list(result.parameters),
        "preview": result.preview.as_dict(),
        "rejected_source_safety": {
            "database_user": result.rejected_sources.database_user,
            "transaction_read_only": result.rejected_sources.transaction_read_only,
            "statement_timeout_ms": result.rejected_sources.statement_timeout_ms,
            "total_records": result.rejected_sources.total_records,
            "truncated": result.rejected_sources.truncated,
        },
        "limitations": [
            "This artifact replays one fingerprint-bound synthetic observation; it is not a live database claim.",
            "The full local path recompiles, reparses, and executes with the dedicated read-only PostgreSQL role.",
        ],
    }
    return {
        "logical-model-customer.yml": _yaml_bytes(
            {
                "source": context.source,
                "model": customer.model_dump(mode="json"),
            }
        ),
        "column-mapping-customer-key.yml": _yaml_bytes(
            {
                "source": context.source,
                "mappings": [item.model_dump(mode="json") for item in customer_key_mappings],
            }
        ),
        "join-contract-customer-account-holder.yml": _yaml_bytes(
            {"source": context.source, "contract": contract.model_dump(mode="json")}
        ),
        "analytical-request-secondary-holders.yml": _yaml_bytes(
            {
                "context_source": validated.context_source,
                "context_version": validated.context_version,
                "request": validated.request.model_dump(mode="json"),
                "required_models": [item.root for item in validated.required_models],
                "approved_join_contracts": list(validated.join_contract_ids),
            }
        ),
        "resolved-query-plan-secondary-holders.yml": _yaml_bytes(
            prepared.resolved_plan.model_dump(mode="json")
        ),
        "generated-secondary-holders.sql": (result.sql.rstrip() + "\n").encode(),
        "query-validation-report.yml": _yaml_bytes(validation),
        "rejected-records.csv": _rejection_csv(result.rejected_sources.records),
        "datahub-writeback.yml": _yaml_bytes(publication),
    }


def build_evaluation_summary(root: Path, database_url: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="schemabridge-m18-") as directory:
        settings = Settings.model_validate(
            {
                "DATABASE_URL": database_url,
                "SCHEMABRIDGE_DRAFT_STORE_PATH": Path(directory) / "evaluation.db",
            }
        )
        report = build_evaluation_runner(
            repository_root=root, settings=settings
        ).execute_evaluation()
    if not report.successful:
        raise SubmissionPackageError("deterministic release evaluation failed")
    return _evaluation_markdown(report).encode()


def build_manifest(artifacts: dict[str, bytes], state: ReleaseState) -> bytes:
    entries = [
        {
            "path": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(artifacts.items())
    ]
    package_checksum = hashlib.sha256(
        "".join(f"{item['path']}:{item['sha256']}\n" for item in entries).encode()
    ).hexdigest()
    payload = {
        "schema_version": 1,
        "source_revision": state.revision,
        "release_ready": state.release_ready,
        "release_label": (
            state.revision if state.release_ready else f"{state.revision}-dirty-development"
        ),
        "generation_command": "make submission-package",
        "development_generation_command": "make submission-package-dev",
        "package_sha256": package_checksum,
        "artifacts": entries,
        "notice": (
            "Release evidence from a clean commit."
            if state.release_ready
            else "Development evidence only; regenerate after the release commit."
        ),
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def write_artifacts(output: Path, artifacts: dict[str, bytes]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    unexpected = sorted(
        path.name for path in output.iterdir() if path.is_file() and path.name not in _OUTPUT_NAMES
    )
    if unexpected:
        raise SubmissionPackageError(
            "refusing to leave unmanifested files in the final artifact directory: "
            + ", ".join(unexpected)
        )
    for name, payload in artifacts.items():
        (output / name).write_bytes(payload)


def _canonical_publication_evidence() -> dict[str, object]:
    store = InMemoryReviewStore()
    draft = StartCanonicalReview(store).execute(build_customer_review_draft())
    decide = DecideCanonicalMapping(store)
    for revision, item in enumerate(draft.mappings, start=1):
        snapshot = decide.execute(
            draft.id,
            mapping_target_id(item.mapping),
            DecisionAction.APPROVE,
            expected_revision=revision,
            actor="submission-steward",
            decided_at=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            rationale="Approved synthetic evidence for the generated submission artifact.",
        )
    publication = PrepareCanonicalPublication(store).execute(snapshot.draft.id)
    approval = PublicationApproval(
        id="submission-customer-canonical-v5",
        draft_id=publication.draft_id,
        draft_version=publication.draft_version,
        payload_fingerprint=publication.fingerprint,
        actor="submission-steward",
        approved_at=datetime(2026, 7, 22, 12, 5, tzinfo=UTC),
        decision_ids=tuple(decision.id for decision in publication.decisions),
        confirmation=PublicationConfirmation.PUBLISH_APPROVED_CANONICAL_CONTEXT,
    )
    writer = FakeCatalogWriteAdapter()
    before = writer.read_context(publication)
    result = writer.publish(publication, approval)
    after = writer.read_context(publication)
    return {
        "artifact_kind": "approval_gated_writeback_contract",
        "adapter_mode": "contract-compatible fake; no DataHub mutation performed by generation",
        "live_datahub_verification": {
            "command": ".venv/bin/pytest -m integration tests/integration/test_datahub_writeback.py",
            "requires": "local DataHub Core plus ignored scoped writer credentials",
            "release_run_status": "must be regenerated and recorded from the clean release commit",
        },
        "before": None if before is None else before.model_dump(mode="json"),
        "approval": approval.model_dump(mode="json"),
        "publication": publication.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
        "after": None if after is None else after.model_dump(mode="json"),
        "reuse_rule": (
            "Published context is retrieved by fingerprint; query recipes never execute saved SQL "
            "and must pass current planning and SQL validation again."
        ),
    }


def _evaluation_markdown(report: EvaluationReport) -> str:
    deterministic = next(run for run in report.runs if run.mode.value == "deterministic")
    metrics = [metric for section in deterministic.sections for metric in section.metrics]
    observed = [
        case
        for section in deterministic.sections
        for case in section.cases
        if case.detail in {"false_positive", "false_negative"}
    ]
    live = next(run for run in report.runs if run.mode.value == "live_llm")
    lines = [
        "# Synthetic evaluation summary",
        "",
        "> Small, tuned synthetic fixture. Raw counts only; this is not production evidence.",
        "",
        f"- Required deterministic run: **{'PASS' if report.successful else 'FAIL'}**",
        f"- Source revision: `{report.release.revision}`",
        f"- Dirty working tree: `{str(report.release.dirty).lower()}`",
        f"- Source fingerprint: `{report.release.source_fingerprint}`",
        f"- Fixture fingerprint: `{report.fixture_fingerprint}`",
        f"- Live LLM: **{live.status.value}** — {live.reason}",
        "- Regression threshold: none; the fixture is too small to justify one.",
        "",
        "| Metric | Raw count | Value |",
        "|---|---:|---:|",
    ]
    lines.extend(
        f"| `{metric.name}` | {metric.numerator:g}/{metric.denominator:g} | {metric.value:.3f} |"
        for metric in metrics
    )
    lines.extend(("", "## Difficult cases retained", ""))
    lines.extend(
        f"- `{case.id}`: **{case.detail}** (expected `{case.expected}`, actual `{case.actual}`)."
        for case in observed
    )
    lines.extend(
        (
            "",
            "The complete case-by-case report is generated by `make evaluate`. Malicious SQL cases "
            "are guard-only and are never executed.",
            "",
        )
    )
    return "\n".join(lines)


def _rejection_csv(records: tuple[RejectedSourceRecord, ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(("logical_field", "physical_field", "source_value", "code", "reason"))
    for record in records:
        writer.writerow(
            (
                record.logical_field.root,
                record.physical_field.root,
                "NULL" if record.source_value is None else record.source_value,
                record.code.value,
                record.reason,
            )
        )
    return stream.getvalue().encode()


def _yaml_bytes(payload: object) -> bytes:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("examples/final"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--allow-uncommitted", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    output = (
        (root / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    )
    if not output.is_relative_to(root):
        raise SubmissionPackageError("submission output must remain inside the repository")
    state = inspect_release_state(root)
    require_release_state(state, allow_uncommitted=args.allow_uncommitted)
    artifacts = build_core_artifacts(root)
    artifacts["evaluation-summary.md"] = build_evaluation_summary(root, args.database_url)
    artifacts["manifest.json"] = build_manifest(artifacts, state)
    write_artifacts(output, artifacts)
    print(
        f"Generated {len(artifacts)} M18 artifacts at {output.relative_to(root)}; "
        f"release_ready={str(state.release_ready).lower()}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SubmissionPackageError as error:
        print(f"Submission package failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
