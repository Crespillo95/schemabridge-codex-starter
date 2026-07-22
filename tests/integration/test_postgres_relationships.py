"""Read-only relationship profiling against the deterministic synthetic PostgreSQL service."""

import pytest

from schemabridge.adapters.postgres.relationships import PsycopgRelationshipEvidenceAdapter
from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.domain.joins import Cardinality, DeclaredRelationship, classify_cardinality

pytestmark = pytest.mark.integration


def test_postgres_relationship_evidence_matches_both_north_star_contracts(
    reader_dsn: str,
) -> None:
    proposals = build_north_star_join_proposals()
    adapter = PsycopgRelationshipEvidenceAdapter(reader_dsn, proposals)

    customer_holder = adapter.profile(proposals[0])
    holder_account = adapter.profile(proposals[1])

    assert (
        customer_holder.left_row_count,
        customer_holder.right_row_count,
        customer_holder.left_null_count,
        customer_holder.right_null_count,
        customer_holder.left_invalid_count,
        customer_holder.right_invalid_count,
        customer_holder.left_distinct_valid,
        customer_holder.right_distinct_valid,
        customer_holder.matching_distinct_keys,
        customer_holder.left_max_multiplicity,
        customer_holder.right_max_multiplicity,
    ) == (7, 9, 0, 1, 0, 2, 7, 5, 5, 1, 2)
    assert customer_holder.declared_relationship is DeclaredRelationship.NONE
    assert classify_cardinality(customer_holder).cardinality is Cardinality.ONE_TO_MANY

    assert (
        holder_account.left_row_count,
        holder_account.right_row_count,
        holder_account.left_distinct_valid,
        holder_account.right_distinct_valid,
        holder_account.matching_distinct_keys,
    ) == (9, 9, 9, 9, 9)
    assert holder_account.declared_relationship is DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT
    assert classify_cardinality(holder_account).cardinality is Cardinality.MANY_TO_ONE
    assert holder_account.reader_user == "schemabridge_reader"
    assert holder_account.transaction_read_only is True
    assert holder_account.statement_timeout_ms == 5_000
