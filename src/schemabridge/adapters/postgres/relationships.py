"""Read-only PostgreSQL aggregate evidence for explicitly allowlisted join proposals."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from schemabridge.adapters.connectors.source_identity import (
    PostgresSourceIdentityMismatchError,
    require_postgres_source_identity,
)
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileSourceCancelled,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.joins import (
    DeclaredRelationship,
    JoinProposal,
    NormalizedJoinKey,
    RelationshipProfile,
)
from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileProposal


class PsycopgRelationshipEvidenceAdapter:
    """Collect counts only; the reader transaction and proposal allowlist are independent controls."""

    def __init__(
        self,
        dsn: str,
        allowed_proposals: tuple[JoinProposal, ...],
        *,
        expected_user: str = "schemabridge_reader",
        expected_source_identity_fingerprint: str | None = None,
        statement_timeout_ms: int = 5_000,
    ) -> None:
        if not dsn:
            raise ValueError("relationship evidence DSN must not be blank")
        if not 100 <= statement_timeout_ms <= 60_000:
            raise ValueError("relationship evidence timeout must be between 100 and 60000 ms")
        self._dsn = dsn
        self._allowed = {proposal.id: proposal for proposal in allowed_proposals}
        self._expected_user = expected_user
        self._expected_source_identity_fingerprint = expected_source_identity_fingerprint
        self._statement_timeout_ms = statement_timeout_ms

    def profile(
        self,
        proposal: JoinProposal,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> RelationshipProfile:
        if self._allowed.get(proposal.id) != proposal:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.EVIDENCE_NOT_ALLOWED,
                "relationship proposal is not in the exact aggregate-evidence allowlist",
            )
        _require_continue(should_continue)
        try:
            import psycopg
        except ModuleNotFoundError as error:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.EVIDENCE_UNAVAILABLE,
                "PostgreSQL support is not installed; install schemabridge[postgres]",
            ) from error
        try:
            _require_continue(should_continue)
            with psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{self._statement_timeout_ms}ms",),
                )
                cursor.execute(
                    "SELECT current_user, current_setting('transaction_read_only'), "
                    "current_setting('statement_timeout'), "
                    "COALESCE(inet_server_addr()::TEXT, 'local_socket'), "
                    "COALESCE(inet_server_port(), 0), current_database()"
                )
                safety = cursor.fetchone()
                if safety is None:
                    raise ValueError("PostgreSQL returned no transaction safety facts")
                reader_user = str(safety[0])
                read_only = str(safety[1]).casefold() == "on"
                timeout_ms = _milliseconds(str(safety[2]))
                if reader_user != self._expected_user or not read_only:
                    raise RelationshipWorkflowError(
                        RelationshipErrorCode.EVIDENCE_NOT_ALLOWED,
                        "relationship evidence connection is not the required read-only reader",
                    )
                if timeout_ms != self._statement_timeout_ms:
                    raise RelationshipWorkflowError(
                        RelationshipErrorCode.EVIDENCE_NOT_ALLOWED,
                        "relationship evidence statement timeout was not applied",
                    )
                if self._expected_source_identity_fingerprint is not None:
                    require_postgres_source_identity(
                        expected_fingerprint=self._expected_source_identity_fingerprint,
                        server_address=safety[3] if len(safety) > 3 else None,
                        server_port=safety[4] if len(safety) > 4 else None,
                        database=safety[5] if len(safety) > 5 else None,
                        user=reader_user,
                    )
                _require_continue(should_continue)
                left = _profile_side(cursor, proposal.left_key)
                _require_continue(should_continue)
                right = _profile_side(cursor, proposal.right_key)
                _require_continue(should_continue)
                matching = _matching_distinct_keys(cursor, proposal.left_key, proposal.right_key)
                _require_continue(should_continue)
                declared = _declared_relationship(cursor, proposal.left_key, proposal.right_key)
        except SemanticJoinProfileSourceCancelled:
            raise
        except PostgresSourceIdentityMismatchError:
            raise
        except RelationshipWorkflowError:
            raise
        except Exception as error:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.INVALID_EVIDENCE,
                "PostgreSQL relationship evidence query failed",
            ) from error
        return RelationshipProfile(
            left_row_count=left[0],
            right_row_count=right[0],
            left_null_count=left[1],
            right_null_count=right[1],
            left_invalid_count=left[2],
            right_invalid_count=right[2],
            left_distinct_valid=left[3],
            right_distinct_valid=right[3],
            matching_distinct_keys=matching,
            left_max_multiplicity=left[4],
            right_max_multiplicity=right[4],
            declared_relationship=declared,
            reader_user=reader_user,
            transaction_read_only=read_only,
            statement_timeout_ms=timeout_ms,
        )


@dataclass(frozen=True, slots=True)
class QueuedProposalRelationshipEvidenceAdapter:
    """Profile only the exact canonical proposal claimed from the governed queue.

    The worker cannot invent or list proposals: the reconciler is the sole queue
    producer, and this adapter constructs a one-item allowlist for each already
    validated claim before opening the read-only source connection.
    """

    dsn: str = field(repr=False)
    expected_connection_id: CatalogConnectionId
    expected_user: str = "schemabridge_reader"
    statement_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("relationship evidence DSN must not be blank")
        if not self.expected_user.strip() or len(self.expected_user) > 120:
            raise ValueError("relationship evidence reader identity is invalid")
        if not 100 <= self.statement_timeout_ms <= 60_000:
            raise ValueError("relationship evidence timeout must be between 100 and 60000 ms")

    def profile_bound(
        self,
        proposal: SemanticJoinProfileProposal,
    ) -> RelationshipProfile:
        checked = SemanticJoinProfileProposal.model_validate(proposal.model_dump(mode="json"))
        if checked.connection_id != self.expected_connection_id:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.EVIDENCE_NOT_ALLOWED,
                "relationship proposal targets another catalog connection",
            )
        return PsycopgRelationshipEvidenceAdapter(
            self.dsn,
            (checked.proposal,),
            expected_user=self.expected_user,
            statement_timeout_ms=self.statement_timeout_ms,
        ).profile(checked.proposal)


def _require_continue(should_continue: Callable[[], bool] | None) -> None:
    if should_continue is None:
        return
    try:
        allowed = should_continue()
    except SemanticJoinProfileSourceCancelled:
        raise
    except Exception:
        raise SemanticJoinProfileSourceCancelled(
            "semantic profile source operation was cooperatively cancelled"
        ) from None
    if allowed is not True:
        raise SemanticJoinProfileSourceCancelled(
            "semantic profile source operation was cooperatively cancelled"
        )


def _profile_side(cursor: Any, key: NormalizedJoinKey) -> tuple[int, int, int, int, int]:
    from psycopg import sql

    schema, table, field = _parts(key)
    column = sql.Identifier(field)
    normalized = _normalization_expression(key, sql.SQL("source.{}").format(column))
    query = sql.SQL(
        """
        WITH normalized AS (
            SELECT source.{column} IS NULL AS is_null, {normalized} AS normalized_key
            FROM {schema}.{table} AS source
        ), multiplicities AS (
            SELECT normalized_key, COUNT(*) AS row_count
            FROM normalized
            WHERE normalized_key IS NOT NULL
            GROUP BY normalized_key
        )
        SELECT
            COUNT(*)::BIGINT,
            COUNT(*) FILTER (WHERE is_null)::BIGINT,
            COUNT(*) FILTER (WHERE NOT is_null AND normalized_key IS NULL)::BIGINT,
            COUNT(DISTINCT normalized_key)::BIGINT,
            COALESCE((SELECT MAX(row_count) FROM multiplicities), 0)::BIGINT
        FROM normalized
        """
    ).format(
        column=column,
        normalized=normalized,
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
    )
    cursor.execute(query)
    row = cursor.fetchone()
    if row is None or len(row) != 5:
        raise ValueError("PostgreSQL returned an invalid side profile")
    return tuple(int(value) for value in row)  # type: ignore[return-value]


def _matching_distinct_keys(
    cursor: Any,
    left: NormalizedJoinKey,
    right: NormalizedJoinKey,
) -> int:
    from psycopg import sql

    left_schema, left_table, left_field = _parts(left)
    right_schema, right_table, right_field = _parts(right)
    left_expression = _normalization_expression(
        left, sql.SQL("source.{}").format(sql.Identifier(left_field))
    )
    right_expression = _normalization_expression(
        right, sql.SQL("source.{}").format(sql.Identifier(right_field))
    )
    query = sql.SQL(
        """
        WITH left_keys AS (
            SELECT DISTINCT {left_expression} AS normalized_key
            FROM {left_schema}.{left_table} AS source
        ), right_keys AS (
            SELECT DISTINCT {right_expression} AS normalized_key
            FROM {right_schema}.{right_table} AS source
        )
        SELECT COUNT(*)::BIGINT
        FROM left_keys
        INNER JOIN right_keys USING (normalized_key)
        WHERE normalized_key IS NOT NULL
        """
    ).format(
        left_expression=left_expression,
        left_schema=sql.Identifier(left_schema),
        left_table=sql.Identifier(left_table),
        right_expression=right_expression,
        right_schema=sql.Identifier(right_schema),
        right_table=sql.Identifier(right_table),
    )
    cursor.execute(query)
    row = cursor.fetchone()
    if row is None:
        raise ValueError("PostgreSQL returned no overlap profile")
    return int(row[0])


def _declared_relationship(
    cursor: Any,
    left: NormalizedJoinKey,
    right: NormalizedJoinKey,
) -> DeclaredRelationship:
    left_parts = _parts(left)
    right_parts = _parts(right)
    if _foreign_key_exists(cursor, left_parts, right_parts):
        return DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT
    if _foreign_key_exists(cursor, right_parts, left_parts):
        return DeclaredRelationship.RIGHT_FOREIGN_KEY_TO_LEFT
    return DeclaredRelationship.NONE


def _foreign_key_exists(
    cursor: Any,
    source: tuple[str, str, str],
    target: tuple[str, str, str],
) -> bool:
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_constraint AS relationship
            INNER JOIN pg_class AS source_table
                ON source_table.oid = relationship.conrelid
            INNER JOIN pg_namespace AS source_namespace
                ON source_namespace.oid = source_table.relnamespace
            INNER JOIN pg_class AS target_table
                ON target_table.oid = relationship.confrelid
            INNER JOIN pg_namespace AS target_namespace
                ON target_namespace.oid = target_table.relnamespace
            INNER JOIN LATERAL unnest(relationship.conkey) WITH ORDINALITY
                AS source_key(attribute_number, position) ON TRUE
            INNER JOIN LATERAL unnest(relationship.confkey) WITH ORDINALITY
                AS target_key(attribute_number, position)
                ON target_key.position = source_key.position
            INNER JOIN pg_attribute AS source_attribute
                ON source_attribute.attrelid = relationship.conrelid
               AND source_attribute.attnum = source_key.attribute_number
            INNER JOIN pg_attribute AS target_attribute
                ON target_attribute.attrelid = relationship.confrelid
               AND target_attribute.attnum = target_key.attribute_number
            WHERE relationship.contype = 'f'
              AND source_namespace.nspname = %s
              AND source_table.relname = %s
              AND source_attribute.attname = %s
              AND target_namespace.nspname = %s
              AND target_table.relname = %s
              AND target_attribute.attname = %s
        )
        """,
        (*source, *target),
    )
    row = cursor.fetchone()
    return bool(row and row[0])


def _normalization_expression(key: NormalizedJoinKey, column: Any) -> Any:
    from psycopg import sql

    operations = tuple(step.operation for step in key.transformation_plan.steps)
    if operations == ("identity",):
        return sql.SQL("{}::TEXT").format(column)
    if operations == ("trim", "validate_regex", "strip_leading_zeros", "reject_invalid"):
        return sql.SQL(
            "CASE WHEN BTRIM({column}) ~ '^[0-9]+$' "
            "THEN COALESCE(NULLIF(LTRIM(BTRIM({column}), '0'), ''), '0') END"
        ).format(column=column)
    if operations == (
        "validate_finite",
        "validate_integral",
        "cast_integer_to_string",
        "reject_invalid",
    ):
        return sql.SQL(
            "CASE WHEN {column} NOT IN "
            "('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION, "
            "'-Infinity'::DOUBLE PRECISION) "
            "AND {column} = TRUNC({column}) "
            "AND {column} >= 0 "
            "AND {column} <= 9007199254740991 "
            "THEN TRUNC({column})::BIGINT::TEXT END"
        ).format(column=column)
    raise RelationshipWorkflowError(
        RelationshipErrorCode.EVIDENCE_NOT_ALLOWED,
        "relationship normalization plan is outside the bounded PostgreSQL interpreter",
    )


def _parts(key: NormalizedJoinKey) -> tuple[str, str, str]:
    parts = key.physical_field.root.split(".")
    if len(parts) != 3:
        raise RelationshipWorkflowError(
            RelationshipErrorCode.EVIDENCE_NOT_ALLOWED,
            "nested relationship field paths are outside the PostgreSQL evidence adapter",
        )
    return parts[0], parts[1], parts[2]


def _milliseconds(value: str) -> int:
    normalized = value.strip().casefold()
    units: Sequence[tuple[str, int]] = (("ms", 1), ("s", 1_000), ("min", 60_000))
    for suffix, multiplier in units:
        if normalized.endswith(suffix):
            number = normalized[: -len(suffix)].strip()
            return int(float(number) * multiplier)
    return int(normalized)
