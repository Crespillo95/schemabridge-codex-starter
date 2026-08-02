"""Immutable PostgreSQL semantic reports and atomic signed decision commits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.control_plane.postgres_registry_control import (
    _audit_hash,
    _canonical_fingerprint,
    _event_id,
    _isoformat,
)
from schemabridge.adapters.control_plane.workspace_lock import workspace_control_lock_id
from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.semantic_change import (
    SemanticChangeDependencyIndexPort,
    SemanticChangePortError,
    SemanticChangePortErrorCode,
)
from schemabridge.domain.joins import DeclaredRelationship
from schemabridge.domain.semantic_change import (
    JoinEvidenceBaseline,
    SemanticChangeCommit,
    SemanticChangeDecision,
    SemanticChangeDecisionAction,
    SemanticChangeDecisionApproval,
    SemanticChangeDecisionProposal,
    SemanticChangeReport,
    SemanticChangeSeverity,
    SemanticChangeStatus,
    SemanticEvidenceBaseline,
    SemanticEvidenceObservation,
    SemanticImpactSet,
    semantic_change_fingerprint,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope


@dataclass(frozen=True, slots=True)
class PostgresSemanticChangeStore:
    """One reconciler-owned store; reports replay and decisions commit atomically."""

    dsn: str = field(repr=False)
    dependency_index: SemanticChangeDependencyIndexPort
    audit_signing_keys: Mapping[str, bytes] = field(repr=False)
    active_audit_key_version: str
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-reconciler"
    retention: timedelta = timedelta(days=365)
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not self.audit_signing_keys
            or self.active_audit_key_version not in self.audit_signing_keys
            or any(
                not version.strip() or len(version) > 80 or len(key) < 32 or len(set(key)) < 8
                for version, key in self.audit_signing_keys.items()
            )
            or self.retention < timedelta(days=1)
            or self.retention > timedelta(days=3_650)
        ):
            raise ValueError("semantic change store configuration is invalid")
        object.__setattr__(
            self,
            "_database",
            _ControlDatabase(
                self.dsn,
                self.schema,
                application_name=self.application_name,
                connection_provider=self.connection_provider,
            ),
        )

    def load_report(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
    ) -> SemanticChangeReport | None:
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                return self._load_report(connection, scope, report_id)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("semantic change report read failed") from error

    def load_observation(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
    ) -> SemanticEvidenceObservation | None:
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                return self._load_observation(connection, scope, report_id)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("semantic evidence observation read failed") from error

    def record_report(
        self,
        report: SemanticChangeReport,
        observation: SemanticEvidenceObservation,
    ) -> SemanticChangeReport:
        _validate_report_observation(report, observation)
        impacts = self.dependency_index.resolve_impacts(report.context, report.findings)
        if _impact_summary(impacts) != report.impacts:
            raise SemanticChangePortError(
                SemanticChangePortErrorCode.INVALID_RESPONSE,
                "semantic impact set does not match its immutable report",
            )
        reports = self._database.table("semantic_change_reports")
        try:
            with self._database.connect() as connection:
                self._insert_report(connection, reports, report, observation)
                self._insert_findings(connection, report, observation)
                self._insert_impacts(connection, report, impacts)
                stored_report = self._load_report(connection, report.context.scope, report.id)
                stored_observation = self._load_observation(
                    connection,
                    report.context.scope,
                    report.id,
                )
                if stored_report != report or stored_observation != observation:
                    raise SemanticChangePortError(
                        SemanticChangePortErrorCode.CAS_CONFLICT,
                        "semantic report identity already contains different evidence",
                    )
            return report
        except SemanticChangePortError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("semantic change report commit failed") from error

    def load_baseline(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticEvidenceBaseline | None:
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                return self._load_baseline(connection, scope)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("semantic evidence baseline read failed") from error

    def load_head(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticChangeCommit | None:
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                return self._load_head(connection, scope)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("semantic evidence head read failed") from error

    def commit_decision(
        self,
        proposal: SemanticChangeDecisionProposal,
        approval: SemanticChangeDecisionApproval,
        decision: SemanticChangeDecision,
        baseline: SemanticEvidenceBaseline | None,
    ) -> SemanticChangeCommit:
        _validate_decision_parts(proposal, approval, decision, baseline)
        scope = proposal.report.context.scope
        heads = self._database.table("semantic_change_heads")
        try:
            with self._database.connect() as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (workspace_control_lock_id(scope.workspace_id),),
                )
                replay = self._load_exact_replay(
                    connection,
                    scope,
                    proposal,
                    approval,
                    decision,
                )
                if replay is not None:
                    return replay
                current = connection.execute(
                    sql.SQL(
                        """
                        SELECT head_revision, baseline_revision, baseline_report_id,
                               baseline_fingerprint, updated_at
                        FROM {heads}
                        WHERE workspace_id = %s
                          AND catalog_scope = %s
                          AND registry_id = %s
                        FOR UPDATE
                        """
                    ).format(heads=heads),
                    _scope(scope),
                ).fetchone()
                current_revision = 0 if current is None else _integer(current[0])
                if current_revision != proposal.expected_head_revision:
                    raise _cas_conflict()
                stored_report = self._load_report(connection, scope, proposal.report.id)
                stored_observation = self._load_observation(
                    connection,
                    scope,
                    proposal.report.id,
                )
                if stored_report != proposal.report or stored_observation != proposal.observation:
                    raise _cas_conflict()
                if baseline is not None:
                    self._insert_approved_bindings(
                        connection,
                        proposal.observation,
                        baseline,
                        approval,
                        proposal.action,
                    )
                    self._insert_approved_profiles(
                        connection,
                        proposal.observation,
                        baseline,
                        approval,
                        proposal.action,
                    )
                audit_event_id, audit_hash = self._append_audit(
                    connection,
                    proposal,
                    approval,
                    decision,
                )
                self._insert_resolution(
                    connection,
                    proposal,
                    approval,
                    decision,
                    baseline,
                    audit_event_id,
                )
                self._write_head(
                    connection,
                    heads,
                    proposal,
                    decision,
                    baseline,
                    current,
                )
                return SemanticChangeCommit(
                    scope=scope,
                    head_revision=proposal.expected_head_revision + 1,
                    decision=decision,
                    baseline=baseline,
                    audit_event_hash=audit_hash,
                )
        except SemanticChangePortError:
            raise
        except UniqueViolation as error:
            raise _cas_conflict() from error
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("semantic change decision commit failed") from error

    def _insert_report(
        self,
        connection: psycopg.Connection[Any],
        table: sql.Composed,
        report: SemanticChangeReport,
        observation: SemanticEvidenceObservation,
    ) -> None:
        heads = self._database.table("semantic_change_heads")
        head_row = connection.execute(
            sql.SQL(
                """
                SELECT head_revision
                FROM {heads}
                WHERE workspace_id = %s
                  AND catalog_scope = %s
                  AND registry_id = %s
                """
            ).format(heads=heads),
            _scope(report.context.scope),
        ).fetchone()
        expected_head_revision = 0 if head_row is None else _integer(head_row[0])
        severity_counts = {
            severity: sum(item.severity is severity for item in report.findings)
            for severity in SemanticChangeSeverity
        }
        finding_set_fingerprint = semantic_change_fingerprint(
            [item.fingerprint for item in report.findings]
        )
        impact_count = (
            report.impacts.mapping_count
            + report.impacts.join_count
            + report.impacts.workflow_count
            + report.impacts.recipe_count
        )
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    report_id, workspace_id, catalog_scope, registry_id,
                    registry_generation, registry_version, registry_fingerprint,
                    pointer_transition_id, pointer_fingerprint,
                    context_fingerprint, observation_fingerprint,
                    expected_head_revision, baseline_revision,
                    baseline_fingerprint, report_kind, outcome,
                    catalog_generation_vector_json,
                    catalog_generation_vector_fingerprint,
                    dependency_index_watermark,
                    dependency_index_fingerprint,
                    dependency_index_complete, governed_mapping_count,
                    governed_join_count, finding_count, review_finding_count,
                    blocking_finding_count, informational_finding_count,
                    impact_count, finding_set_fingerprint,
                    impact_set_fingerprint, report_fingerprint, context_json,
                    observation_json, report_json, inspected_at, retain_until
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                """
            ).format(table=table),
            (
                report.id,
                *_scope(report.context.scope),
                report.context.pointer_generation,
                report.context.registry_version,
                report.context.registry_fingerprint,
                report.context.pointer_transition_id,
                report.context.pointer_fingerprint,
                report.context.fingerprint,
                report.observation_fingerprint,
                expected_head_revision,
                report.baseline_revision or 0,
                report.baseline_fingerprint,
                "baseline_review" if report.baseline_revision is None else "drift_review",
                report.status.value,
                Jsonb(report.catalog_generations.model_dump(mode="json")),
                report.catalog_generations.fingerprint,
                report.impacts.watermark,
                report.impacts.dependency_index_fingerprint,
                report.impacts.complete,
                len(report.context.mappings),
                len(report.context.joins),
                len(report.findings),
                severity_counts[SemanticChangeSeverity.REVIEW_REQUIRED],
                severity_counts[SemanticChangeSeverity.BLOCKING],
                severity_counts[SemanticChangeSeverity.INFORMATIONAL],
                impact_count,
                finding_set_fingerprint,
                report.impacts.impact_set_fingerprint,
                report.fingerprint,
                Jsonb(report.context.model_dump(mode="json")),
                Jsonb(observation.model_dump(mode="json")),
                Jsonb(report.model_dump(mode="json")),
                report.inspected_at,
                report.inspected_at + self.retention,
            ),
        )

    def _insert_findings(
        self,
        connection: psycopg.Connection[Any],
        report: SemanticChangeReport,
        observation: SemanticEvidenceObservation,
    ) -> None:
        table = self._database.table("semantic_change_findings")
        for finding in report.findings:
            mapping_id = None if finding.mapping is None else finding.mapping.approval_decision_id
            affected_joins = sorted(
                join.contract_id
                for join in report.context.joins
                if finding.mapping is not None
                and finding.mapping.physical_field in {join.left_field, join.right_field}
            )
            connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {table} (
                        finding_id, workspace_id, catalog_scope, registry_id,
                        report_id, finding_sort_key, change_kind, severity,
                        binding_id, mapping_decision_id,
                        affected_join_contracts_json,
                        previous_evidence_fingerprint,
                        current_evidence_fingerprint, risk_codes,
                        finding_fingerprint, finding_json, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT DO NOTHING
                    """
                ).format(table=table),
                (
                    finding.id,
                    *_scope(report.context.scope),
                    report.id,
                    finding.id,
                    finding.kind.value,
                    finding.severity.value,
                    mapping_id,
                    Jsonb(affected_joins),
                    finding.previous_fingerprint,
                    finding.current_fingerprint,
                    list(finding.risks),
                    finding.fingerprint,
                    Jsonb(finding.model_dump(mode="json")),
                    observation.observed_at,
                ),
            )

    def _insert_impacts(
        self,
        connection: psycopg.Connection[Any],
        report: SemanticChangeReport,
        impacts: SemanticImpactSet,
    ) -> None:
        table = self._database.table("semantic_change_impacts")
        severity = {item.id: item.severity for item in report.findings}
        for impact in impacts.impacts:
            state = _impact_state(impact.finding_ids, severity)
            impact_id = "impact_" + semantic_change_fingerprint(
                {
                    "report_id": report.id,
                    "impact_fingerprint": impact.fingerprint,
                }
            )
            connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {table} (
                        impact_id, workspace_id, catalog_scope, registry_id,
                        report_id, impact_sort_key, artifact_kind, artifact_id,
                        artifact_version, artifact_fingerprint,
                        finding_ids_json, impact_state, impact_fingerprint,
                        impact_json, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                    ON CONFLICT DO NOTHING
                    """
                ).format(table=table),
                (
                    impact_id,
                    *_scope(report.context.scope),
                    report.id,
                    (
                        f"{impact.kind.value}:{impact.artifact_id}:"
                        f"{impact.artifact_version or 0:020d}"
                    ),
                    impact.kind.value,
                    impact.artifact_id,
                    impact.artifact_version,
                    impact.fingerprint,
                    Jsonb(list(impact.finding_ids)),
                    state,
                    impact.fingerprint,
                    Jsonb(impact.model_dump(mode="json")),
                    report.inspected_at,
                ),
            )

    def _load_report(
        self,
        connection: psycopg.Connection[Any],
        scope: SemanticRegistryScope,
        report_id: str,
    ) -> SemanticChangeReport | None:
        table = self._database.table("semantic_change_reports")
        row = connection.execute(
            sql.SQL(
                """
                SELECT report_json
                FROM {table}
                WHERE workspace_id = %s
                  AND catalog_scope = %s
                  AND registry_id = %s
                  AND report_id = %s
                """
            ).format(table=table),
            (*_scope(scope), report_id),
        ).fetchone()
        return None if row is None else SemanticChangeReport.model_validate(row[0])

    def _load_observation(
        self,
        connection: psycopg.Connection[Any],
        scope: SemanticRegistryScope,
        report_id: str,
    ) -> SemanticEvidenceObservation | None:
        table = self._database.table("semantic_change_reports")
        row = connection.execute(
            sql.SQL(
                """
                SELECT observation_json
                FROM {table}
                WHERE workspace_id = %s
                  AND catalog_scope = %s
                  AND registry_id = %s
                  AND report_id = %s
                """
            ).format(table=table),
            (*_scope(scope), report_id),
        ).fetchone()
        return None if row is None else SemanticEvidenceObservation.model_validate(row[0])

    def _load_baseline(
        self,
        connection: psycopg.Connection[Any],
        scope: SemanticRegistryScope,
    ) -> SemanticEvidenceBaseline | None:
        heads = self._database.table("semantic_change_heads")
        resolutions = self._database.table("semantic_change_resolutions")
        row = connection.execute(
            sql.SQL(
                """
                SELECT head.baseline_revision, head.baseline_report_id,
                       head.baseline_fingerprint, resolution.approval_id,
                       resolution.approved_at
                FROM {heads} AS head
                LEFT JOIN {resolutions} AS resolution
                  ON resolution.workspace_id = head.workspace_id
                 AND resolution.catalog_scope = head.catalog_scope
                 AND resolution.registry_id = head.registry_id
                 AND resolution.resulting_baseline_revision
                    = head.baseline_revision
                WHERE head.workspace_id = %s
                  AND head.catalog_scope = %s
                  AND head.registry_id = %s
                ORDER BY resolution.committed_at DESC NULLS LAST
                LIMIT 1
                """
            ).format(heads=heads, resolutions=resolutions),
            _scope(scope),
        ).fetchone()
        if row is None or _integer(row[0]) == 0:
            return None
        if row[1] is None or row[2] is None or row[3] is None or row[4] is None:
            raise ValueError("semantic baseline head is incomplete")
        observation = self._load_observation(connection, scope, str(row[1]))
        if observation is None:
            raise ValueError("semantic baseline observation is unavailable")
        baseline = SemanticEvidenceBaseline(
            scope=scope,
            revision=_integer(row[0]),
            context_fingerprint=observation.context.fingerprint,
            pointer_generation=observation.context.pointer_generation,
            pointer_fingerprint=observation.context.pointer_fingerprint,
            registry_version=observation.context.registry_version,
            registry_fingerprint=observation.context.registry_fingerprint,
            catalog_generations=observation.catalog_generations,
            fields=observation.fields,
            joins=tuple(
                JoinEvidenceBaseline(profile=item, approved_at=row[4]) for item in observation.joins
            ),
            approval_id=str(row[3]),
            approved_at=row[4],
            fingerprint=str(row[2]),
        )
        return baseline

    def _load_head(
        self,
        connection: psycopg.Connection[Any],
        scope: SemanticRegistryScope,
    ) -> SemanticChangeCommit | None:
        heads = self._database.table("semantic_change_heads")
        resolutions = self._database.table("semantic_change_resolutions")
        audits = self._database.table("control_audit_events")
        row = connection.execute(
            sql.SQL(
                """
                SELECT head.head_revision, resolution.resolution_id,
                       resolution.proposal_fingerprint, resolution.report_id,
                       resolution.report_fingerprint,
                       resolution.decision_action, resolution.resulting_state,
                       resolution.actor_id, resolution.approved_at,
                       resolution.resolution_fingerprint, audit.event_hash
                FROM {heads} AS head
                JOIN {resolutions} AS resolution
                  ON resolution.workspace_id = head.workspace_id
                 AND resolution.catalog_scope = head.catalog_scope
                 AND resolution.registry_id = head.registry_id
                 AND resolution.resolution_id = head.last_resolution_id
                JOIN {audits} AS audit
                  ON audit.workspace_id = resolution.workspace_id
                 AND audit.event_id = resolution.control_audit_event_id
                WHERE head.workspace_id = %s
                  AND head.catalog_scope = %s
                  AND head.registry_id = %s
                """
            ).format(heads=heads, resolutions=resolutions, audits=audits),
            _scope(scope),
        ).fetchone()
        if row is None:
            return None
        action = SemanticChangeDecisionAction(str(row[5]))
        baseline = (
            None
            if action is SemanticChangeDecisionAction.REJECT_CHANGE
            else self._load_baseline(connection, scope)
        )
        decision = SemanticChangeDecision(
            id=str(row[1]),
            proposal_fingerprint=str(row[2]),
            report_id=str(row[3]),
            report_fingerprint=str(row[4]),
            action=action,
            resulting_status=SemanticChangeStatus(str(row[6])),
            actor=str(row[7]),
            decided_at=row[8],
            baseline=baseline,
            fingerprint=str(row[9]),
        )
        return SemanticChangeCommit(
            scope=scope,
            head_revision=_integer(row[0]),
            decision=decision,
            baseline=baseline,
            audit_event_hash=str(row[10]),
        )

    def _load_exact_replay(
        self,
        connection: psycopg.Connection[Any],
        scope: SemanticRegistryScope,
        proposal: SemanticChangeDecisionProposal,
        approval: SemanticChangeDecisionApproval,
        decision: SemanticChangeDecision,
    ) -> SemanticChangeCommit | None:
        resolutions = self._database.table("semantic_change_resolutions")
        row = connection.execute(
            sql.SQL(
                """
                SELECT proposal_fingerprint, report_id, report_fingerprint,
                       approval_id, actor_id, approved_at, confirmation,
                       resolution_fingerprint
                FROM {resolutions}
                WHERE workspace_id = %s AND resolution_id = %s
                """
            ).format(resolutions=resolutions),
            (scope.workspace_id, decision.id),
        ).fetchone()
        if row is None:
            return None
        if (
            str(row[0]) != proposal.fingerprint
            or str(row[1]) != proposal.report.id
            or str(row[2]) != proposal.report.fingerprint
            or str(row[3]) != approval.id
            or str(row[4]) != approval.actor
            or row[5] != approval.approved_at
            or str(row[6]) != approval.confirmation.value
            or str(row[7]) != decision.fingerprint
        ):
            raise _cas_conflict()
        commit = self._load_head(connection, scope)
        if (
            commit is None
            or commit.head_revision != proposal.expected_head_revision + 1
            or commit.decision != decision
        ):
            raise _cas_conflict()
        return SemanticChangeCommit(
            scope=commit.scope,
            head_revision=commit.head_revision,
            decision=commit.decision,
            baseline=commit.baseline,
            audit_event_hash=commit.audit_event_hash,
            replayed=True,
        )

    def _insert_approved_bindings(
        self,
        connection: psycopg.Connection[Any],
        observation: SemanticEvidenceObservation,
        baseline: SemanticEvidenceBaseline,
        approval: SemanticChangeDecisionApproval,
        action: SemanticChangeDecisionAction,
    ) -> None:
        table = self._database.table("semantic_resource_bindings")
        evidence = self._database.table("semantic_catalog_evidence_projection")
        state = (
            "approved"
            if action is SemanticChangeDecisionAction.ESTABLISH_BASELINE
            else "revalidated"
        )
        for field_evidence in baseline.fields:
            binding = field_evidence.binding
            if binding is None or not field_evidence.present:
                raise _cas_conflict()
            locator = binding.locator
            row = connection.execute(
                sql.SQL(
                    """
                    SELECT asset_key, field_key
                    FROM {evidence}
                    WHERE workspace_id = %s
                      AND catalog_scope = %s
                      AND connection_id = %s
                      AND catalog_generation = %s
                      AND asset_id = %s
                      AND field_path = %s
                    """
                ).format(evidence=evidence),
                (
                    observation.context.scope.workspace_id,
                    observation.context.scope.catalog_scope,
                    locator.asset.connection_id.root,
                    binding.catalog_generation,
                    locator.asset.asset_id.root,
                    list(locator.field_path),
                ),
            ).fetchall()
            if len(row) != 1:
                raise _cas_conflict()
            connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {table} (
                        binding_id, workspace_id, catalog_scope, registry_id,
                        binding_revision, registry_generation, registry_version,
                        registry_fingerprint, pointer_transition_id,
                        mapping_decision_id, mapping_version, logical_field,
                        connection_id, catalog_generation,
                        catalog_generation_fingerprint, asset_key, asset_id,
                        field_key, field_path, normalized_type, nullable,
                        is_part_of_key, field_metadata_fingerprint,
                        field_definition_fingerprint, field_terms_fingerprint,
                        asset_metadata_fingerprint, evidence_fingerprint,
                        binding_fingerprint, binding_state,
                        approved_baseline_revision, approval_id, actor_id,
                        decided_at, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT DO NOTHING
                    """
                ).format(table=table),
                (
                    binding.binding_id,
                    *_scope(observation.context.scope),
                    baseline.revision,
                    observation.context.pointer_generation,
                    observation.context.registry_version,
                    observation.context.registry_fingerprint,
                    observation.context.pointer_transition_id,
                    binding.mapping.approval_decision_id,
                    binding.mapping.version,
                    binding.mapping.logical_field.root,
                    locator.asset.connection_id.root,
                    binding.catalog_generation,
                    binding.catalog_generation_fingerprint,
                    str(row[0][0]),
                    locator.asset.asset_id.root,
                    str(row[0][1]),
                    list(locator.field_path),
                    field_evidence.normalized_type.value
                    if field_evidence.normalized_type is not None
                    else None,
                    field_evidence.nullable,
                    field_evidence.is_part_of_key,
                    binding.field_metadata_fingerprint,
                    binding.field_definition_fingerprint,
                    binding.field_terms_fingerprint,
                    binding.asset_metadata_fingerprint,
                    field_evidence.fingerprint,
                    binding.fingerprint,
                    state,
                    baseline.revision,
                    approval.id,
                    approval.actor,
                    approval.approved_at,
                    approval.approved_at,
                ),
            )

    def _insert_approved_profiles(
        self,
        connection: psycopg.Connection[Any],
        observation: SemanticEvidenceObservation,
        baseline: SemanticEvidenceBaseline,
        approval: SemanticChangeDecisionApproval,
        action: SemanticChangeDecisionAction,
    ) -> None:
        table = self._database.table("semantic_join_profiles")
        bindings = {
            item.mapping.physical_field: item.binding
            for item in baseline.fields
            if item.binding is not None
        }
        state = (
            "approved"
            if action is SemanticChangeDecisionAction.ESTABLISH_BASELINE
            else "revalidated"
        )
        for joined in observation.joins:
            left = bindings.get(joined.join.left_field)
            right = bindings.get(joined.join.right_field)
            if left is None or right is None:
                raise _cas_conflict()
            profile_id_fingerprint = semantic_change_fingerprint(
                {
                    "profile": joined.fingerprint,
                    "baseline_revision": baseline.revision,
                    "left_binding": left.binding_id,
                    "right_binding": right.binding_id,
                }
            )
            profile = joined.profile
            declared = profile.declared_relationship
            connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {table} (
                        profile_id, workspace_id, catalog_scope, registry_id,
                        registry_generation, registry_version,
                        registry_fingerprint, contract_id, contract_version,
                        left_binding_id, right_binding_id,
                        normalization_fingerprint, policy_fingerprint,
                        left_row_count, right_row_count, left_null_count,
                        right_null_count, left_invalid_count, right_invalid_count,
                        left_distinct_count, right_distinct_count,
                        matching_distinct_count, max_left_multiplicity,
                        max_right_multiplicity, declared_relationship,
                        foreign_key_evidence, observed_cardinality,
                        profile_state, approved_baseline_revision, approval_id,
                        actor_id, decided_at, profile_fingerprint, observed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT DO NOTHING
                    """
                ).format(table=table),
                (
                    "profile_" + profile_id_fingerprint,
                    *_scope(observation.context.scope),
                    observation.context.pointer_generation,
                    observation.context.registry_version,
                    observation.context.registry_fingerprint,
                    joined.join.contract_id,
                    joined.join.version,
                    left.binding_id,
                    right.binding_id,
                    semantic_change_fingerprint(
                        {
                            "left": joined.join.left_field,
                            "right": joined.join.right_field,
                        }
                    ),
                    joined.safety_fingerprint,
                    profile.left_row_count,
                    profile.right_row_count,
                    profile.left_null_count,
                    profile.right_null_count,
                    profile.left_invalid_count,
                    profile.right_invalid_count,
                    profile.left_distinct_valid,
                    profile.right_distinct_valid,
                    profile.matching_distinct_keys,
                    profile.left_max_multiplicity,
                    profile.right_max_multiplicity,
                    declared.value,
                    declared is not DeclaredRelationship.NONE,
                    joined.observed_cardinality.value,
                    state,
                    baseline.revision,
                    approval.id,
                    approval.actor,
                    approval.approved_at,
                    joined.fingerprint,
                    observation.observed_at,
                ),
            )

    def _append_audit(
        self,
        connection: psycopg.Connection[Any],
        proposal: SemanticChangeDecisionProposal,
        approval: SemanticChangeDecisionApproval,
        decision: SemanticChangeDecision,
    ) -> tuple[str, str]:
        table = self._database.table("control_audit_events")
        workspace_id = proposal.report.context.scope.workspace_id
        previous_row = connection.execute(
            sql.SQL(
                """
                SELECT event_hash
                FROM {table}
                WHERE workspace_id = %s
                ORDER BY sequence DESC
                LIMIT 1
                """
            ).format(table=table),
            (workspace_id,),
        ).fetchone()
        previous_hash = None if previous_row is None else str(previous_row[0])
        event_payload = {
            "proposal_fingerprint": proposal.fingerprint,
            "approval": approval.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
        }
        payload_fingerprint = _canonical_fingerprint(event_payload)
        event_id = _event_id("semantic-change-decision", decision.id)
        event_hash = _audit_hash(
            key=self.audit_signing_keys[self.active_audit_key_version],
            event_id=event_id,
            workspace_id=workspace_id,
            operation="semantic_change_decision",
            transition_id=None,
            previous_hash=previous_hash,
            payload_fingerprint=payload_fingerprint,
            key_version=self.active_audit_key_version,
            occurred_at=_isoformat(decision.decided_at),
        )
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    event_id, workspace_id, operation, transition_id,
                    previous_hash, event_hash, payload_fingerprint,
                    key_version, event_json, occurred_at
                ) VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=table),
            (
                event_id,
                workspace_id,
                "semantic_change_decision",
                previous_hash,
                event_hash,
                payload_fingerprint,
                self.active_audit_key_version,
                Jsonb(event_payload),
                decision.decided_at,
            ),
        )
        return event_id, event_hash

    def _insert_resolution(
        self,
        connection: psycopg.Connection[Any],
        proposal: SemanticChangeDecisionProposal,
        approval: SemanticChangeDecisionApproval,
        decision: SemanticChangeDecision,
        baseline: SemanticEvidenceBaseline | None,
        audit_event_id: str,
    ) -> None:
        table = self._database.table("semantic_change_resolutions")
        report = proposal.report
        impact_count = (
            report.impacts.mapping_count
            + report.impacts.join_count
            + report.impacts.workflow_count
            + report.impacts.recipe_count
        )
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    resolution_id, workspace_id, catalog_scope, registry_id,
                    report_id, report_fingerprint, expected_head_revision,
                    expected_baseline_revision, resulting_head_revision,
                    resulting_baseline_revision, decision_action,
                    resulting_state, proposal_fingerprint,
                    approval_fingerprint, approval_id, confirmation, actor_id,
                    approved_at, impact_count, impact_set_fingerprint,
                    dependency_index_watermark, dependency_index_fingerprint,
                    approved_binding_set_fingerprint, control_audit_event_id,
                    resolution_fingerprint, committed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s
                )
                """
            ).format(table=table),
            (
                decision.id,
                *_scope(report.context.scope),
                report.id,
                report.fingerprint,
                proposal.expected_head_revision,
                report.baseline_revision or 0,
                proposal.expected_head_revision + 1,
                None if baseline is None else baseline.revision,
                decision.action.value,
                decision.resulting_status.value,
                proposal.fingerprint,
                semantic_change_fingerprint(approval.model_dump(mode="json")),
                approval.id,
                approval.confirmation.value,
                approval.actor,
                approval.approved_at,
                impact_count,
                report.impacts.impact_set_fingerprint,
                report.impacts.watermark,
                report.impacts.dependency_index_fingerprint,
                None if baseline is None else baseline.fingerprint,
                audit_event_id,
                decision.fingerprint,
                decision.decided_at,
            ),
        )

    def _write_head(
        self,
        connection: psycopg.Connection[Any],
        table: sql.Composed,
        proposal: SemanticChangeDecisionProposal,
        decision: SemanticChangeDecision,
        baseline: SemanticEvidenceBaseline | None,
        current: tuple[object, ...] | None,
    ) -> None:
        report = proposal.report
        if baseline is not None:
            baseline_revision = baseline.revision
            baseline_report_id = report.id
            baseline_fingerprint = baseline.fingerprint
        elif current is None:
            baseline_revision = 0
            baseline_report_id = None
            baseline_fingerprint = None
        else:
            baseline_revision = _integer(current[1])
            baseline_report_id = _optional_string(current[2])
            baseline_fingerprint = _optional_string(current[3])
        values = (
            proposal.expected_head_revision + 1,
            report.context.pointer_generation,
            report.context.registry_version,
            report.context.registry_fingerprint,
            report.context.pointer_transition_id,
            baseline_revision,
            baseline_report_id,
            baseline_fingerprint,
            report.id,
            report.fingerprint,
            decision.resulting_status.value,
            report.catalog_generations.fingerprint,
            report.impacts.watermark,
            report.impacts.dependency_index_fingerprint,
            report.impacts.complete,
            decision.id,
            decision.decided_at,
        )
        if current is None:
            connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {table} (
                        workspace_id, catalog_scope, registry_id, head_revision,
                        registry_generation, registry_version,
                        registry_fingerprint, pointer_transition_id,
                        baseline_revision, baseline_report_id,
                        baseline_fingerprint, current_report_id,
                        current_report_fingerprint, state,
                        catalog_generation_vector_fingerprint,
                        dependency_index_watermark,
                        dependency_index_fingerprint,
                        dependency_index_complete, last_resolution_id, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """
                ).format(table=table),
                (*_scope(report.context.scope), *values),
            )
            return
        if decision.decided_at <= _datetime(current[4]):
            raise _cas_conflict()
        updated = connection.execute(
            sql.SQL(
                """
                UPDATE {table}
                SET head_revision = %s,
                    registry_generation = %s,
                    registry_version = %s,
                    registry_fingerprint = %s,
                    pointer_transition_id = %s,
                    baseline_revision = %s,
                    baseline_report_id = %s,
                    baseline_fingerprint = %s,
                    current_report_id = %s,
                    current_report_fingerprint = %s,
                    state = %s,
                    catalog_generation_vector_fingerprint = %s,
                    dependency_index_watermark = %s,
                    dependency_index_fingerprint = %s,
                    dependency_index_complete = %s,
                    last_resolution_id = %s,
                    updated_at = %s
                WHERE workspace_id = %s
                  AND catalog_scope = %s
                  AND registry_id = %s
                  AND head_revision = %s
                """
            ).format(table=table),
            (
                *values,
                *_scope(report.context.scope),
                proposal.expected_head_revision,
            ),
        )
        if updated.rowcount != 1:
            raise _cas_conflict()


def _validate_report_observation(
    report: SemanticChangeReport,
    observation: SemanticEvidenceObservation,
) -> None:
    if (
        report.context != observation.context
        or report.observation_fingerprint != observation.fingerprint
        or report.catalog_generations != observation.catalog_generations
    ):
        raise ValueError("semantic report does not match its evidence observation")


def _validate_decision_parts(
    proposal: SemanticChangeDecisionProposal,
    approval: SemanticChangeDecisionApproval,
    decision: SemanticChangeDecision,
    baseline: SemanticEvidenceBaseline | None,
) -> None:
    if (
        decision.proposal_fingerprint != proposal.fingerprint
        or approval.proposal_fingerprint != proposal.fingerprint
        or approval.report_fingerprint != proposal.report.fingerprint
        or decision.report_id != proposal.report.id
        or decision.report_fingerprint != proposal.report.fingerprint
        or decision.actor != approval.actor
        or decision.decided_at != approval.approved_at
        or decision.baseline != baseline
    ):
        raise ValueError("semantic decision parts do not match")


def _impact_summary(impacts: SemanticImpactSet) -> object:
    from schemabridge.domain.semantic_change import SemanticImpactSummary

    return SemanticImpactSummary.from_set(impacts)


def _impact_state(
    finding_ids: tuple[str, ...],
    severities: dict[str, SemanticChangeSeverity],
) -> str:
    selected = {severities[item] for item in finding_ids}
    if SemanticChangeSeverity.BLOCKING in selected:
        return "blocked"
    if SemanticChangeSeverity.REVIEW_REQUIRED in selected:
        return "review_required"
    return "informational"


def _scope(scope: SemanticRegistryScope) -> tuple[str, str, str]:
    return scope.workspace_id, scope.catalog_scope, scope.registry_id


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("semantic change integer is invalid")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("semantic change identifier is invalid")
    return value


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("semantic change timestamp is invalid")
    return value


def _cas_conflict() -> SemanticChangePortError:
    return SemanticChangePortError(
        SemanticChangePortErrorCode.CAS_CONFLICT,
        "semantic evidence head changed; prepare a new decision",
    )


def _store_unavailable(message: str) -> SemanticChangePortError:
    return SemanticChangePortError(
        SemanticChangePortErrorCode.STORE_UNAVAILABLE,
        message,
    )


__all__ = ["PostgresSemanticChangeStore"]
