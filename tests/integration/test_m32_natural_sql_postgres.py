"""Optional M32 result validation through the synthetic read-only PostgreSQL lane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
from schemabridge.adapters.query_studio.advanced_fake_language import (
    M32_REFERENCE_QUESTION_ES,
    DeterministicAdvancedLanguageAdapter,
)
from schemabridge.adapters.query_studio.advanced_security import (
    HmacAdvancedQueryPreviewTokens,
)
from schemabridge.adapters.query_studio.advanced_semantic_index import (
    RegistryWideAdvancedSemanticIndex,
)
from schemabridge.adapters.semantic_registry.memory import (
    InMemoryGovernedSemanticRegistry,
)
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.export import PostgresCopyableSqlRenderer
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.natural_sql import (
    ConfirmNaturalSqlPreview,
    GenerateGovernedCopyableSql,
    PrepareNaturalSqlPreview,
    PrepareOptionalNaturalSqlValidation,
)
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.advanced_query_studio import (
    AdvancedNaturalLanguageInput,
    AdvancedQueryConfirmation,
    AdvancedQueryConfirmationAction,
)
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.resolution import ResolutionLimits

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    def now(self) -> datetime:
        return NOW


@dataclass(frozen=True, slots=True)
class FixedNonce:
    def new_nonce(self) -> str:
        return "nonce_ABCDEFGHIJKLMNOP"


def test_confirmed_copy_artifact_returns_exact_rows_only_in_separate_preview_lane(
    reader_dsn: str,
) -> None:
    loaded = build_semantic_registry(repository_root=ROOT).load()
    registry = InMemoryGovernedSemanticRegistry(loaded.registry, loaded.scope)
    language = DeterministicAdvancedLanguageAdapter()
    tokens = HmacAdvancedQueryPreviewTokens(bytes(range(32)))
    clock = FixedClock()
    guard = SqlGlotPolicyGuard()
    query = AdvancedNaturalLanguageInput(
        text=M32_REFERENCE_QUESTION_ES,
        language=UserLanguage.SPANISH,
    )

    preparation = PrepareNaturalSqlPreview(
        registry=registry,
        mentions=language,
        retrieval=RegistryWideAdvancedSemanticIndex(),
        interpreter=language,
        preview_tokens=tokens,
        clock=clock,
        nonces=FixedNonce(),
    ).execute(query)
    assert preparation.preview is not None
    assert preparation.token is not None
    confirmed = ConfirmNaturalSqlPreview(
        registry=registry,
        preview_tokens=tokens,
        clock=clock,
    ).execute(
        preparation,
        AdvancedQueryConfirmation(
            action=AdvancedQueryConfirmationAction.CONFIRM,
            request_digest=query.digest,
            preview_fingerprint=preparation.preview.fingerprint,
            routed_request_fingerprint=(preparation.preview.routed_request_fingerprint),
            token=preparation.token,
        ),
    )
    generated = GenerateGovernedCopyableSql(
        registry=registry,
        compiler=PostgresQueryCompiler(),
        guard=guard,
        renderer=PostgresCopyableSqlRenderer(),
        limits=ResolutionLimits(),
    ).execute(confirmed)

    assert generated.artifact.executed is False
    assert generated.as_dict()["executed"] is False

    optional = PrepareOptionalNaturalSqlValidation(
        registry=registry,
        compiler=PostgresQueryCompiler(),
        guard=guard,
        limits=ResolutionLimits(),
    ).execute(confirmed)
    assert optional.query.sql != generated.artifact.sql
    assert optional.query.parameters
    result = PsycopgQueryPreview(reader_dsn).execute(optional.query)

    assert result.database_user == "schemabridge_reader"
    assert result.transaction_read_only is True
    assert result.statement_timeout_ms == 5_000
    assert result.columns == (
        "month",
        "category",
        "net_revenue",
        "units",
        "distinct_orders",
        "revenue_rank",
        "revenue_percent",
        "cumulative_revenue",
    )
    assert result.rows == (
        (
            date(2026, 1, 1),
            "BOOKS",
            Decimal("1496.45"),
            25,
            9,
            1,
            Decimal("37.85"),
            Decimal("1496.45"),
        ),
        (
            date(2026, 1, 1),
            "SPORTS",
            Decimal("988.80"),
            16,
            6,
            2,
            Decimal("25.01"),
            Decimal("2485.25"),
        ),
        (
            date(2026, 1, 1),
            "ELECTRONICS",
            Decimal("938.65"),
            23,
            7,
            3,
            Decimal("23.74"),
            Decimal("3423.90"),
        ),
        (
            date(2026, 2, 1),
            "BOOKS",
            Decimal("988.00"),
            16,
            5,
            1,
            Decimal("60.77"),
            Decimal("988.00"),
        ),
        (
            date(2026, 2, 1),
            "HOME",
            Decimal("637.80"),
            12,
            4,
            2,
            Decimal("39.23"),
            Decimal("1625.80"),
        ),
    )
