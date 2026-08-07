"""Provider-free regressions for the M27 v16 semantic authority boundary.

The historical filename is retained so the focused M27 gate remains stable.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from schemabridge.adapters.language import openai_query_studio as subject
from schemabridge.adapters.language.openai_boundary import (
    OpenAIAdapterError,
    OpenAIAdapterErrorCode,
    OpenAIResponsesConfig,
    OpenAIStage,
)
from schemabridge.application.ports.query_studio import QueryStudioPortError
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    ApprovedPublicMetadataSurface,
    DescriptionExpansionInput,
    DescriptionQuery,
    DescriptionSearchProbe,
    OpaqueCandidateId,
    ProviderConfigurationFacts,
    QueryFieldPurpose,
    QueryStudioInterpretationInput,
    QueryStudioPromptCandidate,
    QueryStudioPromptModel,
    QueryStudioPromptVocabulary,
    QueryStudioRequiredSelectionCounts,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import (
    DateGrain,
    FilterOperator,
    MetricOperation,
    SortDirection,
)


def _candidate_id(marker: str) -> OpaqueCandidateId:
    return OpaqueCandidateId(f"qsc1_{marker * 32}")


def _filter_probe() -> DescriptionSearchProbe:
    return DescriptionSearchProbe(
        purpose_id="filter_1",
        query=DescriptionQuery("governed filter value"),
        source_span="filter value",
        intended_use=QueryFieldPurpose.FILTER,
        roles=(LogicalFieldRole.ATTRIBUTE,),
        filter_operator=FilterOperator.EQUALS,
    )


def _filter_candidate(
    canonical_type: CanonicalType,
    *,
    allowed_values: tuple[str, ...] = (),
) -> QueryStudioPromptCandidate:
    return QueryStudioPromptCandidate(
        candidate_id=_candidate_id("f"),
        logical_field=LogicalFieldRef("Synthetic.filter_value"),
        definition="Synthetic governed filter value.",
        canonical_type=canonical_type,
        role=LogicalFieldRole.ATTRIBUTE,
        intended_uses=(QueryFieldPurpose.FILTER,),
        purpose_ids=("filter_1",),
        allowed_values=allowed_values,
        score=100,
    )


@pytest.mark.parametrize(
    ("source_text", "canonical_type", "allowed_values", "expected"),
    (
        ("amount equals -5", CanonicalType.INTEGER, (), -5),
        ("amount equals +1.50", CanonicalType.DECIMAL, (), 1.5),
        ("orders on 2026-07-27", CanonicalType.DATE, (), date(2026, 7, 27)),
        (
            "orders after 2026-07-27T12:34:56+02:00",
            CanonicalType.TIMESTAMP,
            (),
            datetime.fromisoformat("2026-07-27T12:34:56+02:00"),
        ),
        ("count active products", CanonicalType.BOOLEAN, (), True),
        ("count inactive products", CanonicalType.BOOLEAN, (), False),
        (
            "clientes que sean segundo titular",
            CanonicalType.STRING,
            ("PRIMARY", "SECONDARY"),
            "SECONDARY",
        ),
    ),
)
def test_v16_derives_typed_filter_values_only_from_the_business_request(
    source_text: str,
    canonical_type: CanonicalType,
    allowed_values: tuple[str, ...],
    expected: object,
) -> None:
    assert (
        subject._derive_filter_value(
            probe=_filter_probe(),
            candidate=_filter_candidate(
                canonical_type,
                allowed_values=allowed_values,
            ),
            source_text=source_text,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("source_text", "canonical_type", "allowed_values"),
    (
        ("amount equals -5 or +1.50", CanonicalType.DECIMAL, ()),
        (
            "active and inactive accounts",
            CanonicalType.STRING,
            ("ACTIVE", "INACTIVE"),
        ),
        ("active and inactive products", CanonicalType.BOOLEAN, ()),
        (
            "orders between 2026-07-27 and 2026-07-28",
            CanonicalType.DATE,
            (),
        ),
    ),
)
def test_v16_filter_derivation_rejects_conflicting_explicit_values(
    source_text: str,
    canonical_type: CanonicalType,
    allowed_values: tuple[str, ...],
) -> None:
    with pytest.raises(QueryStudioPortError):
        subject._derive_filter_value(
            probe=_filter_probe(),
            candidate=_filter_candidate(
                canonical_type,
                allowed_values=allowed_values,
            ),
            source_text=source_text,
        )


@pytest.mark.parametrize(
    ("text", "language", "metric_span", "temporal_span", "grain"),
    (
        (
            "sum revenue by month",
            UserLanguage.ENGLISH,
            "revenue",
            "month",
            DateGrain.MONTH,
        ),
        (
            "suma importe por mes",
            UserLanguage.SPANISH,
            "importe",
            "mes",
            DateGrain.MONTH,
        ),
        (
            "sum revenue by week",
            UserLanguage.ENGLISH,
            "revenue",
            "week",
            DateGrain.WEEK,
        ),
        (
            "suma importe por semana",
            UserLanguage.SPANISH,
            "importe",
            "semana",
            DateGrain.WEEK,
        ),
        (
            "sum revenue by year",
            UserLanguage.ENGLISH,
            "revenue",
            "year",
            DateGrain.YEAR,
        ),
        (
            "suma importe por año",
            UserLanguage.SPANISH,
            "importe",
            "año",
            DateGrain.YEAR,
        ),
    ),
)
def test_grounded_temporal_grain_spans_survive_full_expansion_validation(
    text: str,
    language: UserLanguage,
    metric_span: str,
    temporal_span: str,
    grain: DateGrain,
) -> None:
    value = DescriptionExpansionInput(
        text=DescriptionQuery(text),
        language=language,
    )
    expansion = subject._reconstruct_expansion(
        value,
        subject._OpenAIDescriptionExpansion.model_validate(
            {
                "slots": (
                    {
                        "slot_id": "metric_1",
                        "query": metric_span,
                    },
                    {
                        "slot_id": "dimension_1",
                        "query": temporal_span,
                    },
                )
            }
        ),
    )

    subject._require_grounded_expansion(value, expansion)
    subject._require_complete_expansion(value, expansion)
    assert expansion.probes[1].date_grain is grain


def _metric_candidate(marker: str, *, purpose_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "candidate_id": _candidate_id(marker),
        "logical_field": LogicalFieldRef(f"Sales.metric_{marker}"),
        "definition": f"Synthetic governed metric {marker}.",
        "canonical_type": CanonicalType.DECIMAL,
        "role": LogicalFieldRole.MEASURE,
        "intended_uses": (QueryFieldPurpose.METRIC,),
        "purpose_ids": purpose_ids,
        "score": 100,
    }


def test_numbered_prompt_vocabulary_requires_every_exact_slot_to_have_a_candidate() -> None:
    with pytest.raises(ValidationError):
        QueryStudioPromptVocabulary(
            context_source="synthetic:v16-security",
            context_version=1,
            models=(
                QueryStudioPromptModel(
                    id=LogicalModelRef("Sales"),
                    definition="Synthetic governed sales model.",
                ),
            ),
            candidates=(
                QueryStudioPromptCandidate(**_metric_candidate("a", purpose_ids=("metric_1",))),
                QueryStudioPromptCandidate(
                    candidate_id=_candidate_id("b"),
                    logical_field=LogicalFieldRef("Sales.country"),
                    definition="Synthetic governed country.",
                    canonical_type=CanonicalType.STRING,
                    role=LogicalFieldRole.ATTRIBUTE,
                    intended_uses=(QueryFieldPurpose.DIMENSION,),
                    purpose_ids=("dimension_1",),
                    score=99,
                ),
                QueryStudioPromptCandidate(
                    candidate_id=_candidate_id("c"),
                    logical_field=LogicalFieldRef("Sales.category"),
                    definition="Synthetic governed category.",
                    canonical_type=CanonicalType.STRING,
                    role=LogicalFieldRole.ATTRIBUTE,
                    intended_uses=(QueryFieldPurpose.DIMENSION,),
                    purpose_ids=("dimension_1",),
                    score=98,
                ),
            ),
            required_selection_counts=QueryStudioRequiredSelectionCounts(
                dimensions=2,
                metrics=1,
                filters=0,
            ),
            metric_operations=tuple(MetricOperation),
            filter_operators=tuple(FilterOperator),
            date_grains=tuple(DateGrain),
            sort_directions=tuple(SortDirection),
        )


def test_prompt_candidate_purpose_id_must_match_its_declared_use() -> None:
    with pytest.raises(ValidationError):
        QueryStudioPromptCandidate(
            **_metric_candidate("a", purpose_ids=("filter_1",)),
        )


_SEMANTIC_SCOPE_FINGERPRINT = "a" * 64
_REGISTRY_FINGERPRINT = "b" * 64


def _external_configuration(
    *,
    registry_fingerprint: str = _REGISTRY_FINGERPRINT,
) -> ProviderConfigurationFacts:
    config = OpenAIResponsesConfig()
    return ProviderConfigurationFacts.create(
        adapter="openai_responses",
        model_snapshot=config.model.value,
        reasoning_effort=config.reasoning_effort.value,
        endpoint_region=config.region.value,
        prompt_version=config.prompt_version,
        schema_version=config.schema_version,
        matcher_version="m27-deterministic-v9",
        attempt_policy_version="m27-durable-attempts-v3",
        managed_config_fingerprint=config.fingerprint(stage=OpenAIStage.INTERPRETATION),
        provider_contract_fingerprint=subject.openai_provider_contract_fingerprint(config),
        public_metadata_policy_fingerprint=(
            subject.openai_public_metadata_policy_fingerprint(
                config,
                semantic_scope_fingerprint=_SEMANTIC_SCOPE_FINGERPRINT,
                registry_fingerprint=registry_fingerprint,
            )
        ),
        public_metadata_semantic_scope_fingerprint=_SEMANTIC_SCOPE_FINGERPRINT,
        public_metadata_registry_fingerprint=registry_fingerprint,
        external_ai=True,
    )


def _interpretation_with_metadata(
    definition: str,
    *,
    configuration: ProviderConfigurationFacts,
    surface_policy_fingerprint: str | None,
    surface_registry_fingerprint: str | None = None,
) -> QueryStudioInterpretationInput:
    text = DescriptionQuery("count customers by country")
    expansion_input = DescriptionExpansionInput(
        text=text,
        language=UserLanguage.ENGLISH,
    )
    expansion = subject._reconstruct_expansion(
        expansion_input,
        subject._OpenAIDescriptionExpansion.model_validate(
            {
                "slots": (
                    {
                        "slot_id": "metric_1",
                        "query": "customer identifier",
                    },
                    {
                        "slot_id": "dimension_1",
                        "query": "country",
                    },
                )
            }
        ),
    )
    vocabulary = QueryStudioPromptVocabulary(
        context_source="synthetic:v16-security",
        context_version=1,
        models=(
            QueryStudioPromptModel(
                id=LogicalModelRef("Customer"),
                definition="Synthetic governed customer model.",
            ),
        ),
        candidates=(
            QueryStudioPromptCandidate(
                candidate_id=_candidate_id("m"),
                logical_field=LogicalFieldRef("Customer.customer_key"),
                definition="Stable synthetic customer identifier.",
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.IDENTIFIER,
                intended_uses=(QueryFieldPurpose.METRIC,),
                purpose_ids=("metric_1",),
                score=100,
            ),
            QueryStudioPromptCandidate(
                candidate_id=_candidate_id("d"),
                logical_field=LogicalFieldRef("Customer.country"),
                definition=definition,
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.ATTRIBUTE,
                intended_uses=(QueryFieldPurpose.DIMENSION,),
                purpose_ids=("dimension_1",),
                score=99,
            ),
        ),
        required_selection_counts=QueryStudioRequiredSelectionCounts(
            dimensions=1,
            metrics=1,
            filters=0,
        ),
        metric_operations=tuple(MetricOperation),
        filter_operators=tuple(FilterOperator),
        date_grains=tuple(DateGrain),
        sort_directions=tuple(SortDirection),
    )
    surface = (
        None
        if surface_policy_fingerprint is None
        else ApprovedPublicMetadataSurface.create(
            policy_fingerprint=surface_policy_fingerprint,
            semantic_scope_fingerprint=(configuration.public_metadata_semantic_scope_fingerprint),
            registry_fingerprint=(
                surface_registry_fingerprint or configuration.public_metadata_registry_fingerprint
            ),
            vocabulary_fingerprint=vocabulary.fingerprint,
        )
    )
    return QueryStudioInterpretationInput(
        text=text,
        language=UserLanguage.ENGLISH,
        vocabulary=vocabulary,
        expansion=expansion,
        public_metadata_surface=surface,
    )


def test_restricted_metadata_is_blocked_despite_apparently_valid_surface_evidence() -> None:
    configuration = _external_configuration()
    value = _interpretation_with_metadata(
        "CONFIDENTIAL employee performance rating",
        configuration=configuration,
        surface_policy_fingerprint=configuration.public_metadata_policy_fingerprint,
    )

    with pytest.raises(OpenAIAdapterError) as raised:
        subject._interpretation_provider_input(value, configuration=configuration)

    assert raised.value.code is OpenAIAdapterErrorCode.METADATA_NOT_PUBLIC


@pytest.mark.parametrize("surface_policy_fingerprint", (None, "f" * 64))
def test_missing_or_mismatched_public_metadata_evidence_blocks_before_boundary(
    surface_policy_fingerprint: str | None,
) -> None:
    configuration = _external_configuration()
    value = _interpretation_with_metadata(
        "Public synthetic customer country.",
        configuration=configuration,
        surface_policy_fingerprint=surface_policy_fingerprint,
    )

    with pytest.raises(OpenAIAdapterError) as raised:
        subject._interpretation_provider_input(value, configuration=configuration)

    assert raised.value.code is OpenAIAdapterErrorCode.METADATA_NOT_PUBLIC


def test_public_metadata_surface_from_another_registry_blocks_before_boundary() -> None:
    configuration = _external_configuration()
    value = _interpretation_with_metadata(
        "Public synthetic customer country.",
        configuration=configuration,
        surface_policy_fingerprint=configuration.public_metadata_policy_fingerprint,
        surface_registry_fingerprint="c" * 64,
    )

    with pytest.raises(OpenAIAdapterError) as raised:
        subject._interpretation_provider_input(value, configuration=configuration)

    assert raised.value.code is OpenAIAdapterErrorCode.METADATA_NOT_PUBLIC


def test_provider_configuration_fingerprint_changes_when_only_registry_changes() -> None:
    before = _external_configuration(registry_fingerprint="b" * 64)
    after = _external_configuration(registry_fingerprint="c" * 64)

    assert before.managed_config_fingerprint == after.managed_config_fingerprint
    assert before.provider_contract_fingerprint == after.provider_contract_fingerprint
    assert (
        before.public_metadata_semantic_scope_fingerprint
        == after.public_metadata_semantic_scope_fingerprint
    )
    assert before.public_metadata_policy_fingerprint != after.public_metadata_policy_fingerprint
    assert before.public_metadata_registry_fingerprint != after.public_metadata_registry_fingerprint
    assert before.fingerprint != after.fingerprint


def test_not_active_is_server_owned_not_equals_active_without_inversion() -> None:
    filter_slot = next(
        slot
        for slot in subject._required_slot_specs("count customers that are not active")
        if slot.intended_use is QueryFieldPurpose.FILTER
    )
    probe = _filter_probe().model_copy(
        update={"filter_operator": filter_slot.filter_operator},
    )

    assert filter_slot.filter_operator is FilterOperator.NOT_EQUALS
    assert (
        subject._derive_filter_value(
            probe=probe,
            candidate=_filter_candidate(
                CanonicalType.STRING,
                allowed_values=("ACTIVE", "INACTIVE"),
            ),
            source_text="count customers that are not active",
        )
        == "ACTIVE"
    )
