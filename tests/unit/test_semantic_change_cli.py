from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest

from schemabridge import bootstrap
from schemabridge.application.semantic_change import (
    PrepareSemanticChangeDecisionApproval,
    SemanticChangeError,
    SemanticChangeErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.semantic_change import (
    CatalogGenerationObservation,
    CatalogGenerationVector,
    GovernedMappingRef,
    GovernedResourceBinding,
    ObservedFieldEvidence,
    SemanticBindingSelection,
    SemanticBindingSelectionSet,
    SemanticChangeCommit,
    SemanticChangeConfirmation,
    SemanticChangeDecisionAction,
    SemanticChangeDecisionApproval,
    SemanticChangeDecisionProposal,
    SemanticChangeFinding,
    SemanticChangeImpact,
    SemanticChangeInspectionContext,
    SemanticChangeReport,
    SemanticDependencyIndexState,
    SemanticEvidenceObservation,
    SemanticImpactKind,
    SemanticImpactSet,
    build_semantic_change_approval,
    build_semantic_change_decision,
    build_semantic_change_report,
    classify_semantic_change_findings,
    prepare_semantic_change_decision,
    semantic_change_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    PhysicalValueType,
    SemanticRegistryScope,
)
from schemabridge.entrypoints.semantic_change import main as semantic_change_main
from schemabridge.entrypoints.semantic_change.main import (
    SemanticBindingSelectionFile,
    SemanticChangeApprovalEnvelope,
    SemanticChangeAuditVerification,
    SemanticChangeOperatorConfig,
    SemanticChangeOperatorRuntime,
    SemanticChangeOperatorServices,
    _build_runtime,
    command,
    main,
)

NOW = datetime(2026, 7, 24, 14, 0, tzinfo=UTC)
APPROVED_AT = NOW + timedelta(minutes=1)
TRUSTED_ACTOR = "steward-semantic-change"
SCOPE = SemanticRegistryScope(
    workspace_id="workspace-m26-cli",
    catalog_scope="synthetic-demo",
    registry_id="enterprise_registry",
)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


@dataclass
class _Inspect:
    report: SemanticChangeReport
    calls: int = 0
    last_binding_selections: SemanticBindingSelectionSet | None = None

    def execute(
        self,
        *,
        binding_selections: SemanticBindingSelectionSet | None = None,
    ) -> SemanticChangeReport:
        self.calls += 1
        self.last_binding_selections = binding_selections
        return self.report


@dataclass
class _Prepare:
    proposal: SemanticChangeDecisionProposal
    calls: int = 0

    def execute(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
        *,
        action: SemanticChangeDecisionAction,
    ) -> SemanticChangeDecisionProposal:
        self.calls += 1
        assert scope == SCOPE
        assert report_id == self.proposal.report.id
        assert action is self.proposal.action
        return self.proposal


@dataclass
class _Approve:
    calls: int = 0
    last_actor: str | None = None

    def execute(
        self,
        proposal: SemanticChangeDecisionProposal,
        *,
        actor: str,
        approved_at: datetime,
        confirmation: SemanticChangeConfirmation,
    ) -> SemanticChangeDecisionApproval:
        self.calls += 1
        self.last_actor = actor
        return PrepareSemanticChangeDecisionApproval().execute(
            proposal,
            actor=actor,
            approved_at=approved_at,
            confirmation=confirmation,
        )


@dataclass
class _Commit:
    commit: SemanticChangeCommit
    calls: int = 0
    fail_cas: bool = False
    writes: int = 0

    def execute(
        self,
        proposal: SemanticChangeDecisionProposal,
        approval: SemanticChangeDecisionApproval,
    ) -> SemanticChangeCommit:
        self.calls += 1
        if self.fail_cas:
            raise SemanticChangeError(
                SemanticChangeErrorCode.CAS_CONFLICT,
                "semantic evidence changed",
            )
        assert proposal.fingerprint == self.commit.decision.proposal_fingerprint
        assert approval.actor == TRUSTED_ACTOR
        self.writes += 1
        return self.commit


@dataclass
class _Head:
    commit: SemanticChangeCommit | None
    calls: int = 0

    def execute(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticChangeCommit | None:
        self.calls += 1
        assert scope == SCOPE
        return self.commit


@dataclass
class _Audit:
    verification: SemanticChangeAuditVerification
    calls: int = 0

    def execute(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticChangeAuditVerification:
        self.calls += 1
        assert scope == SCOPE
        return self.verification


@dataclass
class _Fixture:
    report: SemanticChangeReport
    proposal: SemanticChangeDecisionProposal
    commit_value: SemanticChangeCommit
    inspect: _Inspect
    prepare: _Prepare
    approve: _Approve
    commit: _Commit
    head: _Head
    audit: _Audit

    @classmethod
    def create(cls) -> _Fixture:
        report, proposal = _proposal()
        approval = build_semantic_change_approval(
            proposal,
            actor=TRUSTED_ACTOR,
            approved_at=APPROVED_AT,
            confirmation=SemanticChangeConfirmation.ESTABLISH,
        )
        decision = build_semantic_change_decision(proposal, approval)
        commit_value = SemanticChangeCommit(
            scope=SCOPE,
            head_revision=1,
            decision=decision,
            baseline=decision.baseline,
            audit_event_hash=semantic_change_fingerprint({"decision": decision.fingerprint}),
        )
        audit = SemanticChangeAuditVerification(
            valid=True,
            event_count=1,
            head_revision=1,
            chain_head_hash=commit_value.audit_event_hash,
        )
        return cls(
            report=report,
            proposal=proposal,
            commit_value=commit_value,
            inspect=_Inspect(report),
            prepare=_Prepare(proposal),
            approve=_Approve(),
            commit=_Commit(commit_value),
            head=_Head(commit_value),
            audit=_Audit(audit),
        )

    def services(self) -> SemanticChangeOperatorServices:
        return SemanticChangeOperatorServices(
            inspect=self.inspect,
            prepare=self.prepare,
            approve=self.approve,
            commit=self.commit,
            head=self.head,
            verify_audit=self.audit,
        )


def _mapping() -> GovernedMappingRef:
    return GovernedMappingRef(
        logical_field=LogicalFieldRef("Customer.customer_id"),
        physical_field=PhysicalFieldRef("crm.customers.customer_id"),
        version=1,
        approval_decision_id="mapping-decision-cli",
        physical_type=PhysicalValueType.STRING,
    )


def _proposal() -> tuple[SemanticChangeReport, SemanticChangeDecisionProposal]:
    mapping = _mapping()
    dependency = SemanticDependencyIndexState(
        scope=SCOPE,
        watermark=1,
        fingerprint=SHA_D,
        complete=True,
    )
    context = SemanticChangeInspectionContext.create(
        scope=SCOPE,
        pointer_generation=3,
        pointer_fingerprint=SHA_A,
        pointer_transition_id="registry-transition-cli",
        registry_version=4,
        registry_fingerprint=SHA_B,
        mappings=(mapping,),
        joins=(),
        dependency_index=dependency,
    )
    generations = CatalogGenerationVector.create(
        (
            CatalogGenerationObservation(
                connection_id=CatalogConnectionId("catalog-main"),
                generation=9,
                inventory_fingerprint=SHA_C,
            ),
        )
    )
    locator = CatalogFieldLocator(
        asset=CatalogAssetLocator(
            workspace_id=SCOPE.workspace_id,
            connection_id=CatalogConnectionId("catalog-main"),
            asset_id=CatalogAssetId("urn:li:dataset:customers"),
        ),
        field_path=("customer_id",),
    )
    binding = GovernedResourceBinding.create(
        mapping=mapping,
        locator=locator,
        catalog_generation=9,
        catalog_generation_fingerprint=generations.fingerprint,
        asset_metadata_fingerprint=SHA_A,
        field_metadata_fingerprint=SHA_D,
        field_definition_fingerprint=SHA_B,
        field_terms_fingerprint=SHA_C,
    )
    field = ObservedFieldEvidence.create(
        mapping=mapping,
        binding=binding,
        present=True,
        candidate_count=1,
        normalized_type=PhysicalValueType.STRING,
        nullable=False,
        is_part_of_key=True,
        asset_metadata_fingerprint=SHA_A,
        field_metadata_fingerprint=SHA_D,
        field_definition_fingerprint=SHA_B,
        field_terms_fingerprint=SHA_C,
        reason_code=None,
    )
    observation = SemanticEvidenceObservation.create(
        context=context,
        catalog_generations=generations,
        fields=(field,),
        joins=(),
        observed_at=NOW,
        complete=True,
    )
    findings = classify_semantic_change_findings(observation, None)
    impacts = _impacts(observation, findings)
    report = build_semantic_change_report(observation, None, impacts)
    proposal = prepare_semantic_change_decision(
        report,
        observation,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        expected_head_revision=0,
    )
    return report, proposal


def _impacts(
    observation: SemanticEvidenceObservation,
    findings: tuple[SemanticChangeFinding, ...],
) -> SemanticImpactSet:
    mapping = observation.context.mappings[0]
    return SemanticImpactSet.create(
        impacts=(
            SemanticChangeImpact.create(
                kind=SemanticImpactKind.MAPPING,
                artifact_id=mapping.logical_field.root,
                artifact_version=mapping.version,
                finding_ids=tuple(item.id for item in findings),
            ),
        ),
        complete=True,
        watermark=observation.context.dependency_index.watermark,
        dependency_index_fingerprint=(observation.context.dependency_index.fingerprint),
    )


def _invoke(
    fixture: _Fixture,
    *arguments: str,
) -> tuple[int, str, str]:
    output = StringIO()
    errors = StringIO()
    status = command(
        arguments,
        services=fixture.services(),
        config=SemanticChangeOperatorConfig(
            scope=SCOPE,
            trusted_actor=TRUSTED_ACTOR,
        ),
        stdout=output,
        stderr=errors,
    )
    return status, output.getvalue(), errors.getvalue()


def _approve_arguments(proposal: SemanticChangeDecisionProposal) -> tuple[str, ...]:
    return (
        "approve",
        "--report-id",
        proposal.report.id,
        "--action",
        proposal.action.value,
        "--proposal-fingerprint",
        proposal.fingerprint,
        "--report-fingerprint",
        proposal.report.fingerprint,
        "--approved-at",
        APPROVED_AT.isoformat(),
        "--confirm",
        SemanticChangeConfirmation.ESTABLISH.value,
    )


def _write_envelope(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


def _binding_selection_payload(
    fixture: _Fixture,
    *,
    scope: SemanticRegistryScope = SCOPE,
) -> tuple[str, SemanticBindingSelectionSet]:
    mapping = fixture.report.context.mappings[0]
    locator = CatalogFieldLocator(
        asset=CatalogAssetLocator(
            workspace_id=scope.workspace_id,
            connection_id=CatalogConnectionId("catalog-main"),
            asset_id=CatalogAssetId("urn:li:dataset:customers"),
        ),
        field_path=("customer_id",),
    )
    selection = SemanticBindingSelection(
        mapping_approval_decision_id=mapping.approval_decision_id,
        mapping_version=mapping.version,
        physical_field=mapping.physical_field,
        locator=locator,
    )
    selection_file = SemanticBindingSelectionFile(
        scope=scope,
        selections=(selection,),
    )
    selection_set = selection_file.as_selection_set()
    payload = (
        json.dumps(
            selection_file.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    return payload, selection_set


def test_help_exposes_only_closed_operator_inputs() -> None:
    fixture = _Fixture.create()
    for selected in (
        "inspect",
        "prepare",
        "approve",
        "commit",
        "head",
        "verify-audit",
    ):
        status, output, errors = _invoke(fixture, selected, "--help")
        assert status == 0
        assert errors == ""
        lowered = output.casefold()
        for forbidden in (
            "--actor",
            "--dsn",
            "--key",
            "--token",
            "--password",
            "--openai",
            "--migrate",
        ):
            assert forbidden not in lowered


def test_inspect_and_prepare_emit_only_minimized_exact_review_json() -> None:
    fixture = _Fixture.create()
    inspect_status, inspected, _ = _invoke(fixture, "inspect")
    prepare_status, prepared, _ = _invoke(
        fixture,
        "prepare",
        "--report-id",
        fixture.report.id,
        "--action",
        SemanticChangeDecisionAction.ESTABLISH_BASELINE.value,
    )

    assert inspect_status == prepare_status == 0
    inspection = json.loads(inspected)
    preparation = json.loads(prepared)
    assert inspection["report_id"] == fixture.report.id
    assert inspection["report_fingerprint"] == fixture.report.fingerprint
    assert preparation == {
        "action": "establish_baseline",
        "confirmation_required": "ESTABLISH SEMANTIC EVIDENCE BASELINE",
        "expected_head_revision": 0,
        "ok": True,
        "proposal_fingerprint": fixture.proposal.fingerprint,
        "report_fingerprint": fixture.report.fingerprint,
        "report_id": fixture.report.id,
        "writes_performed": False,
    }
    assert fixture.inspect.calls == 1
    assert fixture.prepare.calls == 1
    encoded = f"{inspected}{prepared}".casefold()
    for forbidden in (
        "physical_field",
        "field_definition",
        "source_value",
        "sql",
        "dsn",
        "password",
        "token",
        "openai",
    ):
        assert forbidden not in encoded


def test_inspect_accepts_only_a_canonical_bounded_exact_binding_file(
    tmp_path: Path,
) -> None:
    fixture = _Fixture.create()
    payload, expected = _binding_selection_payload(fixture)
    selection_path = tmp_path / "binding-selections.json"
    _write_envelope(selection_path, payload)

    status, output, errors = _invoke(
        fixture,
        "inspect",
        "--binding-selections",
        str(selection_path),
    )

    assert status == 0
    assert errors == ""
    inspected = json.loads(output)
    assert inspected["binding_selection_count"] == 1
    assert inspected["binding_selection_fingerprint"] == expected.fingerprint
    assert fixture.inspect.last_binding_selections == expected
    assert inspected["status"] == "review_required"
    assert "locator" not in output.casefold()


def test_inspect_rejects_noncanonical_cross_scope_and_oversized_binding_files(
    tmp_path: Path,
) -> None:
    fixture = _Fixture.create()
    canonical, _ = _binding_selection_payload(fixture)
    noncanonical = tmp_path / "noncanonical.json"
    _write_envelope(
        noncanonical,
        json.dumps(json.loads(canonical), indent=2),
    )
    foreign_scope = SemanticRegistryScope(
        workspace_id="workspace-m26-foreign",
        catalog_scope=SCOPE.catalog_scope,
        registry_id=SCOPE.registry_id,
    )
    foreign_payload, _ = _binding_selection_payload(fixture, scope=foreign_scope)
    foreign = tmp_path / "foreign.json"
    _write_envelope(foreign, foreign_payload)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 2_097_153)

    for path in (noncanonical, foreign, oversized):
        status, output, errors = _invoke(
            fixture,
            "inspect",
            "--binding-selections",
            str(path),
        )
        assert status == 1
        assert output == ""
        assert json.loads(errors)["code"] == "semantic_change_cli_invalid_request"

    assert fixture.inspect.calls == 0


def test_approve_uses_only_trusted_actor_time_and_closed_confirmation() -> None:
    fixture = _Fixture.create()
    status, output, errors = _invoke(
        fixture,
        *_approve_arguments(fixture.proposal),
    )

    assert status == 0
    assert errors == ""
    envelope = SemanticChangeApprovalEnvelope.model_validate_json(output)
    assert envelope.report_id == fixture.report.id
    assert envelope.proposal_fingerprint == fixture.proposal.fingerprint
    assert envelope.approval.actor == TRUSTED_ACTOR
    assert envelope.approval.approved_at == APPROVED_AT
    assert envelope.approval.confirmation is SemanticChangeConfirmation.ESTABLISH
    assert fixture.approve.calls == 1
    assert fixture.approve.last_actor == TRUSTED_ACTOR
    assert (
        output
        == json.dumps(
            envelope.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    for forbidden in ("dsn", "password", "token", "openai", "secret", "api_key"):
        assert forbidden not in output.casefold()


def test_approve_rejects_stale_fingerprint_and_naive_time_before_approval() -> None:
    fixture = _Fixture.create()
    stale_arguments = list(_approve_arguments(fixture.proposal))
    stale_arguments[stale_arguments.index("--proposal-fingerprint") + 1] = "0" * 64
    stale_status, _, stale_error = _invoke(fixture, *stale_arguments)

    naive_arguments = list(_approve_arguments(fixture.proposal))
    naive_arguments[naive_arguments.index("--approved-at") + 1] = "2026-07-24T14:01:00"
    naive_status, _, naive_error = _invoke(fixture, *naive_arguments)

    open_confirmation = list(_approve_arguments(fixture.proposal))
    open_confirmation[open_confirmation.index("--confirm") + 1] = "APPROVE EVERYTHING"
    confirmation_status, _, confirmation_error = _invoke(
        fixture,
        *open_confirmation,
    )

    assert stale_status == naive_status == confirmation_status == 1
    assert json.loads(stale_error)["code"] == "semantic_change_approval_mismatch"
    assert json.loads(naive_error)["code"] == "semantic_change_cli_invalid_request"
    assert json.loads(confirmation_error)["code"] == "semantic_change_cli_invalid_request"
    assert fixture.approve.calls == 0


def test_commit_reprepares_and_commits_only_canonical_exact_envelope(
    tmp_path: Path,
) -> None:
    fixture = _Fixture.create()
    approved_status, envelope, _ = _invoke(
        fixture,
        *_approve_arguments(fixture.proposal),
    )
    envelope_path = tmp_path / "approval.json"
    _write_envelope(envelope_path, envelope)

    status, output, errors = _invoke(
        fixture,
        "commit",
        "--approval-envelope",
        str(envelope_path),
    )

    assert approved_status == status == 0
    assert errors == ""
    payload = json.loads(output)
    assert payload["head_revision"] == 1
    assert payload["decision_id"] == fixture.commit_value.decision.id
    assert payload["report_fingerprint"] == fixture.report.fingerprint
    assert payload["writes_performed"] is True
    assert fixture.prepare.calls == 2
    assert fixture.commit.calls == fixture.commit.writes == 1


def test_commit_rejects_noncanonical_tampered_and_wrong_actor_envelopes(
    tmp_path: Path,
) -> None:
    fixture = _Fixture.create()
    _, canonical, _ = _invoke(fixture, *_approve_arguments(fixture.proposal))

    noncanonical_path = tmp_path / "noncanonical.json"
    noncanonical_path.write_text(
        json.dumps(json.loads(canonical), indent=2),
        encoding="utf-8",
    )
    noncanonical_status, _, noncanonical_error = _invoke(
        fixture,
        "commit",
        "--approval-envelope",
        str(noncanonical_path),
    )

    other_approval = build_semantic_change_approval(
        fixture.proposal,
        actor="untrusted-actor",
        approved_at=APPROVED_AT,
        confirmation=SemanticChangeConfirmation.ESTABLISH,
    )
    wrong_actor = SemanticChangeApprovalEnvelope.create(
        fixture.proposal,
        other_approval,
    )
    wrong_actor_path = tmp_path / "wrong-actor.json"
    _write_envelope(
        wrong_actor_path,
        json.dumps(
            wrong_actor.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    wrong_actor_status, _, wrong_actor_error = _invoke(
        fixture,
        "commit",
        "--approval-envelope",
        str(wrong_actor_path),
    )

    assert noncanonical_status == wrong_actor_status == 1
    assert json.loads(noncanonical_error)["code"] == "semantic_change_approval_mismatch"
    assert json.loads(wrong_actor_error)["code"] == "semantic_change_approval_mismatch"
    assert fixture.commit.calls == fixture.commit.writes == 0


def test_commit_surfaces_closed_cas_conflict_with_zero_writes(tmp_path: Path) -> None:
    fixture = _Fixture.create()
    fixture.commit.fail_cas = True
    _, envelope, _ = _invoke(fixture, *_approve_arguments(fixture.proposal))
    envelope_path = tmp_path / "approval.json"
    _write_envelope(envelope_path, envelope)

    status, output, errors = _invoke(
        fixture,
        "commit",
        "--approval-envelope",
        str(envelope_path),
    )

    assert status == 1
    assert output == ""
    assert json.loads(errors) == {
        "code": "semantic_change_cas_conflict",
        "ok": False,
    }
    assert fixture.commit.calls == 1
    assert fixture.commit.writes == 0


def test_head_and_verify_audit_are_minimized_and_read_only() -> None:
    fixture = _Fixture.create()
    head_status, head_output, _ = _invoke(fixture, "head")
    audit_status, audit_output, _ = _invoke(fixture, "verify-audit")

    assert head_status == audit_status == 0
    head = json.loads(head_output)
    audit = json.loads(audit_output)
    assert head["head_revision"] == 1
    assert head["operation"] == "head"
    assert head["writes_performed"] is False
    assert audit == {
        "audit_valid": True,
        "chain_head_hash": fixture.commit_value.audit_event_hash,
        "event_count": 1,
        "head_revision": 1,
        "ok": True,
        "writes_performed": False,
    }
    assert fixture.head.calls == fixture.audit.calls == 1
    assert fixture.commit.writes == 0


def test_verify_audit_accepts_unrelated_events_in_shared_workspace_chain() -> None:
    fixture = _Fixture.create()
    fixture.audit.verification = SemanticChangeAuditVerification(
        valid=True,
        event_count=3,
        head_revision=1,
        chain_head_hash=SHA_A,
    )

    status, output, errors = _invoke(fixture, "verify-audit")

    assert status == 0
    assert errors == ""
    assert json.loads(output)["event_count"] == 3


def test_invalid_audit_and_unknown_secret_options_fail_closed() -> None:
    fixture = _Fixture.create()
    fixture.audit.verification = SemanticChangeAuditVerification(
        valid=False,
        event_count=1,
        head_revision=1,
        chain_head_hash=SHA_A,
    )
    audit_status, _, audit_error = _invoke(fixture, "verify-audit")
    option_status, option_output, option_error = _invoke(
        fixture,
        "inspect",
        "--dsn",
        "postgresql://operator:protected@example.invalid/control",
    )

    assert audit_status == option_status == 1
    assert json.loads(audit_error)["code"] == "semantic_change_audit_invalid"
    assert json.loads(option_error)["code"] == "semantic_change_cli_invalid_request"
    assert option_output == ""
    combined = f"{option_output}{option_error}".casefold()
    for forbidden in ("postgresql", "operator:protected", "example.invalid"):
        assert forbidden not in combined


def test_service_exception_is_sanitized_without_stack_or_vendor_text() -> None:
    fixture = _Fixture.create()

    class _BrokenInspect:
        def execute(
            self,
            *,
            binding_selections: SemanticBindingSelectionSet | None = None,
        ) -> SemanticChangeReport:
            del binding_selections
            raise RuntimeError("driver secret at postgresql://protected")

    services = fixture.services()
    services = SemanticChangeOperatorServices(
        inspect=_BrokenInspect(),
        prepare=services.prepare,
        approve=services.approve,
        commit=services.commit,
        head=services.head,
        verify_audit=services.verify_audit,
    )
    output = StringIO()
    errors = StringIO()
    status = command(
        ["inspect"],
        services=services,
        config=SemanticChangeOperatorConfig(
            scope=SCOPE,
            trusted_actor=TRUSTED_ACTOR,
        ),
        stdout=output,
        stderr=errors,
    )

    assert status == 1
    assert output.getvalue() == ""
    assert json.loads(errors.getvalue()) == {
        "code": "semantic_change_service_unavailable",
        "ok": False,
    }
    assert "driver" not in errors.getvalue()
    assert "postgresql" not in errors.getvalue()


def test_runtime_is_resolved_lazily_from_the_composition_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _Fixture.create()
    expected = SemanticChangeOperatorRuntime(
        services=fixture.services(),
        config=SemanticChangeOperatorConfig(
            scope=SCOPE,
            trusted_actor=TRUSTED_ACTOR,
        ),
    )
    calls = 0

    def build_runtime() -> SemanticChangeOperatorRuntime:
        nonlocal calls
        calls += 1
        return expected

    monkeypatch.setattr(
        bootstrap,
        "build_semantic_change_operator_runtime",
        build_runtime,
        raising=False,
    )

    assert _build_runtime() is expected
    assert calls == 1


def test_main_executes_composed_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _Fixture.create()
    runtime = SemanticChangeOperatorRuntime(
        services=fixture.services(),
        config=SemanticChangeOperatorConfig(
            scope=SCOPE,
            trusted_actor=TRUSTED_ACTOR,
        ),
    )
    monkeypatch.setattr(semantic_change_main, "_build_runtime", lambda: runtime)
    monkeypatch.setattr("sys.argv", ["schemabridge-semantic-change", "head"])

    with pytest.raises(SystemExit) as exit_error:
        main()

    assert exit_error.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["operation"] == "head"


@pytest.mark.parametrize(
    "arguments",
    [
        ("--help",),
        ("inspect", "--help"),
    ],
)
def test_main_help_does_not_compose_runtime(
    arguments: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    composition_calls = 0

    def unexpected_composition() -> SemanticChangeOperatorRuntime:
        nonlocal composition_calls
        composition_calls += 1
        raise RuntimeError("runtime composition must not run for help")

    monkeypatch.setattr(
        semantic_change_main,
        "_build_runtime",
        unexpected_composition,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["schemabridge-semantic-change", *arguments],
    )

    with pytest.raises(SystemExit) as exit_error:
        main()

    assert exit_error.value.code == 0
    assert composition_calls == 0
    captured = capsys.readouterr()
    assert "usage: schemabridge-semantic-change" in captured.out
    assert captured.err == ""


def test_main_sanitizes_composition_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_composition() -> SemanticChangeOperatorRuntime:
        raise RuntimeError("protected postgresql://operator:secret@example.invalid")

    monkeypatch.setattr(semantic_change_main, "_build_runtime", fail_composition)
    monkeypatch.setattr("sys.argv", ["schemabridge-semantic-change", "head"])

    with pytest.raises(SystemExit) as exit_error:
        main()

    assert exit_error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "code": "semantic_change_service_unavailable",
        "ok": False,
    }
    for forbidden in ("protected", "postgresql", "operator", "secret", "example.invalid"):
        assert forbidden not in captured.err.casefold()
