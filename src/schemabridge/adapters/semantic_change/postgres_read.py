"""Minimized PostgreSQL runtime gate and authenticated HTTP read projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TypeVar

import psycopg
from psycopg import sql

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.semantic_change import (
    SemanticChangePortError,
    SemanticChangePortErrorCode,
)
from schemabridge.application.ports.semantic_change_read import (
    MAX_SEMANTIC_CHANGE_PAGE_SIZE,
    SemanticChangeFindingFilter,
    SemanticChangeFindingPublic,
    SemanticChangeImpactFilter,
    SemanticChangeImpactPublic,
    SemanticChangePageKey,
    SemanticChangeReadPortError,
    SemanticChangeReadPortErrorCode,
    SemanticChangeReportFilter,
    SemanticChangeReportPublic,
    SemanticChangeStorePage,
    SemanticChangeTargetKind,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeKind,
    SemanticChangeSeverity,
    SemanticChangeStatus,
    SemanticContextGateAssessment,
    SemanticImpactKind,
    SemanticPlanDependencies,
)

_PublicT = TypeVar(
    "_PublicT",
    SemanticChangeReportPublic,
    SemanticChangeFindingPublic,
    SemanticChangeImpactPublic,
)


@dataclass(frozen=True, slots=True)
class PostgresSemanticChangeGateReader:
    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-runtime"
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
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

    def assess(
        self,
        dependencies: SemanticPlanDependencies,
    ) -> SemanticContextGateAssessment:
        pointers = self._database.table("registry_active_pointers")
        gate = self._database.table("semantic_context_gate_projection")
        mapping_ids = [item.approval_decision_id for item in dependencies.mappings]
        join_ids = [item.contract_id for item in dependencies.joins]
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                pointer_row = connection.execute(
                    sql.SQL(
                        """
                        SELECT generation, registry_version, registry_fingerprint,
                               registry_target, transition_id, activated_by,
                               activated_at, decision_ids_json
                        FROM {pointers}
                        WHERE workspace_id = %s
                          AND catalog_scope = %s
                          AND registry_id = %s
                        """
                    ).format(pointers=pointers),
                    _scope(dependencies),
                ).fetchone()
                if pointer_row is None:
                    return _ineligible(
                        dependencies,
                        (SemanticChangeKind.REGISTRY_CHANGED,),
                    )
                pointer = ActiveRegistryPointer.model_validate(
                    {
                        "scope": dependencies.scope.model_dump(mode="json"),
                        "generation": pointer_row[0],
                        "registry_version": pointer_row[1],
                        "registry_fingerprint": pointer_row[2],
                        "registry_target": pointer_row[3],
                        "transition_id": pointer_row[4],
                        "activated_by": pointer_row[5],
                        "activated_at": pointer_row[6],
                        "decision_ids": pointer_row[7],
                    }
                )
                if (
                    pointer.generation != dependencies.pointer_generation
                    or registry_projection_fingerprint(pointer) != dependencies.pointer_fingerprint
                    or pointer.registry_version != dependencies.registry_version
                    or pointer.registry_fingerprint != dependencies.registry_fingerprint
                ):
                    return _ineligible(
                        dependencies,
                        (SemanticChangeKind.REGISTRY_CHANGED,),
                    )
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT dependency_kind, dependency_id, dependency_version,
                               baseline_revision, context_state,
                               catalog_evidence_available,
                               catalog_evidence_matches,
                               catalog_generation_covered,
                               gate_eligible,
                               connection_id
                        FROM {gate}
                        WHERE workspace_id = %s
                          AND catalog_scope = %s
                          AND registry_id = %s
                          AND (
                              (
                                  dependency_kind = 'mapping'
                                  AND dependency_id = ANY(%s)
                              )
                              OR
                              (
                                  dependency_kind = 'join'
                                  AND dependency_id = ANY(%s)
                              )
                          )
                        ORDER BY dependency_kind, dependency_id, dependency_version
                        """
                    ).format(gate=gate),
                    (*_scope(dependencies), mapping_ids, join_ids),
                ).fetchall()
            return _gate_assessment(dependencies, rows)
        except SemanticChangePortError:
            raise
        except (psycopg.Error, TypeError, ValueError) as error:
            raise SemanticChangePortError(
                SemanticChangePortErrorCode.RESOURCE_UNAVAILABLE,
                "semantic context gate is unavailable",
            ) from error


@dataclass(frozen=True, slots=True)
class PostgresSemanticChangeReadStore:
    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-api"
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
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

    def list_reports(
        self,
        workspace_id: str,
        *,
        filters: SemanticChangeReportFilter,
        page_size: int,
        after: SemanticChangePageKey | None,
    ) -> SemanticChangeStorePage[SemanticChangeReportPublic]:
        _page_size(page_size)
        view = self._database.table("semantic_change_report_public")
        predicates = [sql.SQL("workspace_id = %s")]
        params: list[object] = [workspace_id]
        if filters.status is not None:
            predicates.append(sql.SQL("status = %s"))
            params.append(filters.status.value)
        if after is not None:
            predicates.append(sql.SQL("(inspected_at, report_id) < (%s, %s)"))
            params.extend((after.sort_value, after.stable_id))
        params.append(page_size + 1)
        query = sql.SQL(
            """
            SELECT workspace_id, report_id, status, pointer_generation,
                   pointer_fingerprint, registry_version, registry_fingerprint,
                   catalog_generation_count, observation_fingerprint,
                   baseline_revision, baseline_fingerprint, finding_count,
                   mapping_impact_count, join_impact_count,
                   workflow_impact_count, recipe_impact_count,
                   impacts_complete, dependency_watermark,
                   impact_set_fingerprint, inspected_at, fingerprint
            FROM {view}
            WHERE {where}
            ORDER BY inspected_at DESC, report_id DESC
            LIMIT %s
            """
        ).format(view=view, where=sql.SQL(" AND ").join(predicates))
        rows = self._read(query, tuple(params))
        items = tuple(_report(row) for row in rows[:page_size])
        return _page(
            items,
            rows_read=len(rows),
            page_size=page_size,
            key=(
                None
                if not items
                else SemanticChangePageKey(
                    sort_value=_iso(items[-1].inspected_at),
                    stable_id=items[-1].report_id,
                )
            ),
        )

    def load_report(
        self,
        workspace_id: str,
        report_id: str,
    ) -> SemanticChangeReportPublic | None:
        view = self._database.table("semantic_change_report_public")
        rows = self._read(
            sql.SQL(
                """
                SELECT workspace_id, report_id, status, pointer_generation,
                       pointer_fingerprint, registry_version, registry_fingerprint,
                       catalog_generation_count, observation_fingerprint,
                       baseline_revision, baseline_fingerprint, finding_count,
                       mapping_impact_count, join_impact_count,
                       workflow_impact_count, recipe_impact_count,
                       impacts_complete, dependency_watermark,
                       impact_set_fingerprint, inspected_at, fingerprint
                FROM {view}
                WHERE workspace_id = %s AND report_id = %s
                """
            ).format(view=view),
            (workspace_id, report_id),
        )
        return None if not rows else _report(rows[0])

    def list_findings(
        self,
        workspace_id: str,
        report_id: str,
        *,
        filters: SemanticChangeFindingFilter,
        page_size: int,
        after: SemanticChangePageKey | None,
    ) -> SemanticChangeStorePage[SemanticChangeFindingPublic]:
        _page_size(page_size)
        view = self._database.table("semantic_change_finding_public")
        predicates = [sql.SQL("workspace_id = %s"), sql.SQL("report_id = %s")]
        params: list[object] = [workspace_id, report_id]
        if filters.kind is not None:
            predicates.append(sql.SQL("kind = %s"))
            params.append(filters.kind.value)
        if filters.severity is not None:
            predicates.append(sql.SQL("severity = %s"))
            params.append(filters.severity.value)
        if after is not None:
            if after.sort_value != after.stable_id:
                raise _read_invalid()
            predicates.append(sql.SQL("finding_id > %s"))
            params.append(after.stable_id)
        params.append(page_size + 1)
        rows = self._read(
            sql.SQL(
                """
                SELECT workspace_id, report_id, finding_id, kind, severity,
                       target_kind, target_id, target_version,
                       previous_fingerprint, current_fingerprint, risks,
                       fingerprint
                FROM {view}
                WHERE {where}
                ORDER BY finding_id
                LIMIT %s
                """
            ).format(view=view, where=sql.SQL(" AND ").join(predicates)),
            tuple(params),
        )
        items = tuple(_finding(row) for row in rows[:page_size])
        return _page(
            items,
            rows_read=len(rows),
            page_size=page_size,
            key=(
                None
                if not items
                else SemanticChangePageKey(
                    sort_value=items[-1].finding_id,
                    stable_id=items[-1].finding_id,
                )
            ),
        )

    def list_impacts(
        self,
        workspace_id: str,
        report_id: str,
        *,
        filters: SemanticChangeImpactFilter,
        page_size: int,
        after: SemanticChangePageKey | None,
    ) -> SemanticChangeStorePage[SemanticChangeImpactPublic]:
        _page_size(page_size)
        view = self._database.table("semantic_change_impact_public")
        predicates = [sql.SQL("workspace_id = %s"), sql.SQL("report_id = %s")]
        params: list[object] = [workspace_id, report_id]
        if filters.kind is not None:
            predicates.append(sql.SQL("kind = %s"))
            params.append(filters.kind.value)
        if after is not None:
            predicates.append(sql.SQL("(impact_sort_key, fingerprint) > (%s, %s)"))
            params.extend((after.sort_value, after.stable_id))
        params.append(page_size + 1)
        rows = self._read(
            sql.SQL(
                """
                SELECT workspace_id, report_id, impact_sort_key, kind,
                       artifact_id, artifact_version, finding_ids, fingerprint
                FROM {view}
                WHERE {where}
                ORDER BY impact_sort_key, fingerprint
                LIMIT %s
                """
            ).format(view=view, where=sql.SQL(" AND ").join(predicates)),
            tuple(params),
        )
        items = tuple(_impact(row) for row in rows[:page_size])
        key = (
            None
            if not items
            else SemanticChangePageKey(
                sort_value=str(rows[len(items) - 1][2]),
                stable_id=items[-1].fingerprint,
            )
        )
        return _page(items, rows_read=len(rows), page_size=page_size, key=key)

    def _read(
        self,
        query: sql.Composed,
        params: tuple[object, ...],
    ) -> list[tuple[object, ...]]:
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                return [tuple(row) for row in connection.execute(query, params).fetchall()]
        except psycopg.Error as error:
            raise SemanticChangeReadPortError(
                SemanticChangeReadPortErrorCode.UNAVAILABLE,
                "semantic change resource is unavailable",
            ) from error


def _gate_assessment(
    dependencies: SemanticPlanDependencies,
    rows: list[tuple[object, ...]],
) -> SemanticContextGateAssessment:
    expected = {
        ("mapping", item.approval_decision_id, item.version) for item in dependencies.mappings
    } | {("join", item.contract_id, item.version) for item in dependencies.joins}
    grouped: dict[tuple[str, str, int], list[tuple[object, ...]]] = {}
    for row in rows:
        grouped.setdefault(
            (str(row[0]), str(row[1]), _integer(row[2])),
            [],
        ).append(row)
    reasons: set[SemanticChangeKind] = set()
    states: set[SemanticChangeStatus] = set()
    revisions: set[int] = set()
    connection_ids: set[CatalogConnectionId] = set()
    for identity in expected:
        matches = grouped.get(identity, [])
        if not matches:
            reasons.add(SemanticChangeKind.BINDING_MISSING)
            continue
        if len(matches) != 1:
            reasons.add(SemanticChangeKind.BINDING_AMBIGUOUS)
            continue
        row = matches[0]
        revisions.add(_integer(row[3]))
        try:
            states.add(SemanticChangeStatus(str(row[4])))
        except ValueError:
            reasons.add(SemanticChangeKind.EVIDENCE_UNAVAILABLE)
        if not bool(row[5]):
            reasons.add(SemanticChangeKind.FIELD_REMOVED)
        elif not bool(row[6]) or not bool(row[7]):
            reasons.add(SemanticChangeKind.EVIDENCE_UNAVAILABLE)
        elif not bool(row[8]):
            reasons.add(SemanticChangeKind.BASELINE_REQUIRED)
        raw_connection_id = row[9]
        try:
            connection_id = CatalogConnectionId.model_validate(raw_connection_id)
        except (TypeError, ValueError):
            reasons.add(SemanticChangeKind.BINDING_MISSING)
        else:
            connection_ids.add(connection_id)
    if set(grouped) != expected:
        reasons.add(SemanticChangeKind.BINDING_AMBIGUOUS)
    if len(revisions) != 1:
        reasons.add(SemanticChangeKind.EVIDENCE_UNAVAILABLE)
    if not connection_ids:
        reasons.add(SemanticChangeKind.BINDING_MISSING)
    elif len(connection_ids) > 1:
        reasons.add(SemanticChangeKind.BINDING_AMBIGUOUS)
    eligible = not reasons
    if eligible:
        status = (
            SemanticChangeStatus.REVALIDATED
            if states == {SemanticChangeStatus.REVALIDATED}
            else SemanticChangeStatus.CURRENT
        )
        return SemanticContextGateAssessment(
            dependencies_fingerprint=dependencies.fingerprint,
            eligible=True,
            status=status,
            connection_id=next(iter(connection_ids)),
            reason_codes=(),
            baseline_revision=next(iter(revisions)),
        )
    status = (
        SemanticChangeStatus.REJECTED
        if SemanticChangeStatus.REJECTED in states
        else (
            SemanticChangeStatus.REVIEW_REQUIRED
            if SemanticChangeStatus.REVIEW_REQUIRED in states
            else SemanticChangeStatus.BLOCKED
        )
    )
    return SemanticContextGateAssessment(
        dependencies_fingerprint=dependencies.fingerprint,
        eligible=False,
        status=status,
        connection_id=None,
        reason_codes=tuple(sorted(reasons, key=lambda item: item.value)),
        baseline_revision=(next(iter(revisions)) if len(revisions) == 1 else None),
    )


def _ineligible(
    dependencies: SemanticPlanDependencies,
    reasons: tuple[SemanticChangeKind, ...],
) -> SemanticContextGateAssessment:
    return SemanticContextGateAssessment(
        dependencies_fingerprint=dependencies.fingerprint,
        eligible=False,
        status=SemanticChangeStatus.BLOCKED,
        connection_id=None,
        reason_codes=tuple(sorted(set(reasons), key=lambda item: item.value)),
        baseline_revision=None,
    )


def _report(row: tuple[object, ...]) -> SemanticChangeReportPublic:
    return SemanticChangeReportPublic(
        workspace_id=str(row[0]),
        report_id=str(row[1]),
        status=SemanticChangeStatus(str(row[2])),
        pointer_generation=_integer(row[3]),
        pointer_fingerprint=str(row[4]),
        registry_version=_integer(row[5]),
        registry_fingerprint=str(row[6]),
        catalog_generation_count=_integer(row[7]),
        observation_fingerprint=str(row[8]),
        baseline_revision=None if row[9] is None else _integer(row[9]),
        baseline_fingerprint=None if row[10] is None else str(row[10]),
        finding_count=_integer(row[11]),
        mapping_impact_count=_integer(row[12]),
        join_impact_count=_integer(row[13]),
        workflow_impact_count=_integer(row[14]),
        recipe_impact_count=_integer(row[15]),
        impacts_complete=bool(row[16]),
        dependency_watermark=_integer(row[17]),
        impact_set_fingerprint=str(row[18]),
        inspected_at=_datetime(row[19]),
        fingerprint=str(row[20]),
    )


def _finding(row: tuple[object, ...]) -> SemanticChangeFindingPublic:
    return SemanticChangeFindingPublic(
        workspace_id=str(row[0]),
        report_id=str(row[1]),
        finding_id=str(row[2]),
        kind=SemanticChangeKind(str(row[3])),
        severity=SemanticChangeSeverity(str(row[4])),
        target_kind=SemanticChangeTargetKind(str(row[5])),
        target_id=str(row[6]),
        target_version=_integer(row[7]),
        previous_fingerprint=None if row[8] is None else str(row[8]),
        current_fingerprint=None if row[9] is None else str(row[9]),
        risks=_string_tuple(row[10]),
        fingerprint=str(row[11]),
    )


def _impact(row: tuple[object, ...]) -> SemanticChangeImpactPublic:
    return SemanticChangeImpactPublic(
        workspace_id=str(row[0]),
        report_id=str(row[1]),
        kind=SemanticImpactKind(str(row[3])),
        artifact_id=str(row[4]),
        artifact_version=None if row[5] is None else _integer(row[5]),
        finding_ids=_string_tuple(row[6]),
        fingerprint=str(row[7]),
    )


def _page(
    items: tuple[_PublicT, ...],
    *,
    rows_read: int,
    page_size: int,
    key: SemanticChangePageKey | None,
) -> SemanticChangeStorePage[_PublicT]:
    return SemanticChangeStorePage(
        items=items,
        page_size=page_size,
        rows_read=rows_read,
        has_more=rows_read > page_size,
        last_key=key,
    )


def _page_size(value: int) -> None:
    if isinstance(value, bool) or not 1 <= value <= MAX_SEMANTIC_CHANGE_PAGE_SIZE:
        raise ValueError("semantic change page size is invalid")


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _read_invalid()
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _read_invalid()
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise _read_invalid()
    return tuple(value)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _scope(dependencies: SemanticPlanDependencies) -> tuple[str, str, str]:
    scope = dependencies.scope
    return scope.workspace_id, scope.catalog_scope, scope.registry_id


def _read_invalid() -> SemanticChangeReadPortError:
    return SemanticChangeReadPortError(
        SemanticChangeReadPortErrorCode.INVALID_RESPONSE,
        "semantic change store returned an invalid projection",
    )


__all__ = [
    "PostgresSemanticChangeGateReader",
    "PostgresSemanticChangeReadStore",
]
