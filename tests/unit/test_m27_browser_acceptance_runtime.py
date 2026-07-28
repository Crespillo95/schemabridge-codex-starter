from __future__ import annotations

import json
import os
import secrets
import stat
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
import yaml
from scripts import m27_browser_acceptance_runtime as runtime

from schemabridge.adapters.language.openai_boundary import (
    OpenAIRegion,
    OpenAIResponsesBoundary,
    OpenAIResponsesConfig,
)
from schemabridge.adapters.language.openai_query_studio import (
    OpenAIQueryStudioIntentAdapter,
)
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
    CatalogRefreshCommand,
    CatalogRefreshMode,
    CatalogSourceAsset,
    CatalogSourcePage,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.query_studio import (
    ExecutableEvidenceStatus,
    GovernedBindingFactsRequest,
    GovernedFieldBinding,
    GovernedSearchKey,
    PhysicalDiscoveryCandidate,
    PhysicalDiscoveryCursor,
    PhysicalDiscoveryStatus,
    PhysicalFieldDiscoveryPage,
    PhysicalFieldDiscoveryRequest,
    ProviderConfigurationFacts,
    QueryStudioScopeSnapshot,
    SearchSignal,
    SearchSignalBreakdown,
    SearchSignalCode,
)
from schemabridge.domain.registry_control import RegistryActivationConfirmation
from schemabridge.domain.semantic_registry import (
    PhysicalValueType,
    SemanticRegistryScope,
)
from schemabridge.entrypoints.streamlit.query_studio import (
    _physical_discovery_label,
)


def _state(
    *,
    policy_state: str = "absent",
    cardinality_profile: str = "large",
) -> runtime.BrowserAcceptanceState:
    configured = policy_state != "absent"
    asset_count, field_count = runtime._cardinality_for_profile(cardinality_profile)
    connection_count = runtime._connection_count_for_profile(cardinality_profile)
    return runtime.BrowserAcceptanceState(
        database="schemabridge_m27_browser_012345abcdef",
        local_workspace="m27-browser-012345abcdef",
        local_subject=runtime.LOCAL_SUBJECT,
        workspace_id="sb_workspace_v1_" + ("a" * 64),
        created_at="2026-07-26T12:00:00+00:00",
        control_schema_version=8,
        registry_generation=2,
        registry_version=2,
        registry_fingerprint="b" * 64,
        evidence_head_revision=4,
        evidence_baseline_revision=4,
        catalog_generation=9,
        governed_catalog_generation_vector_fingerprint="c" * 64,
        physical_catalog_generation_vector_fingerprint="e" * 64,
        connection_count=connection_count,
        asset_count=asset_count,
        field_count=field_count,
        governed_mapping_count=31,
        ai_policy_state=policy_state,
        ai_policy_version=1 if configured else None,
        ai_policy_model=runtime.PINNED_MODEL if configured else None,
        ai_policy_region=runtime.PINNED_REGION if configured else None,
        ai_policy_configuration_fingerprint=("d" * 64 if configured else None),
    )


def _expected_live_configuration(
    model_snapshot: str = runtime.PINNED_MODEL,
) -> ProviderConfigurationFacts:
    return runtime._expected_live_configuration_for_state(
        _state(),
        model_snapshot,
    )


def _state_dir(root: Path, state: runtime.BrowserAcceptanceState) -> Path:
    local = root / ".local"
    local.mkdir()
    state_dir = local / "m27-browser"
    state_dir.mkdir(mode=0o700)
    runtime._write_owner_only(state_dir / runtime.STATE_FILE, state.to_json())
    for filename, value in (
        (runtime.AUDIT_KEY_FILE, b"audit-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789\n"),
        (
            runtime.IDENTITY_KEY_FILE,
            b"identity-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789\n",
        ),
        (
            runtime.QUERY_STUDIO_KEY_FILE,
            b"query-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789\n",
        ),
        (
            runtime.PSEUDONYM_KEY_FILE,
            b"pseudonym-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789\n",
        ),
    ):
        runtime._write_owner_only(state_dir / filename, value)
    return state_dir


def _postflight_scope(
    state: runtime.BrowserAcceptanceState,
) -> SemanticRegistryScope:
    return SemanticRegistryScope(
        workspace_id=state.workspace_id,
        catalog_scope=runtime.CATALOG_SCOPE,
        registry_id=runtime.REGISTRY_ID,
    )


def _stale_witness(
    state: runtime.BrowserAcceptanceState,
    *,
    catalog_generation: int | None = None,
    physical_vector: str | None = None,
    governance_available: bool = True,
) -> runtime._BrowserAcceptanceStaleWitness:
    return runtime._BrowserAcceptanceStaleWitness(
        active_catalog_generation=(
            state.catalog_generation if catalog_generation is None else catalog_generation
        ),
        physical_catalog_generation_vector_fingerprint=(
            state.physical_catalog_generation_vector_fingerprint
            if physical_vector is None
            else physical_vector
        ),
        connection_count=state.connection_count,
        asset_count=state.asset_count,
        field_count=state.field_count,
        registry_generation=(state.registry_generation if governance_available else None),
        registry_version=state.registry_version if governance_available else None,
        registry_fingerprint=(state.registry_fingerprint if governance_available else None),
        governed_catalog_generation_vector_fingerprint=(
            state.governed_catalog_generation_vector_fingerprint if governance_available else None
        ),
        governed_mapping_count=(state.governed_mapping_count if governance_available else None),
    )


def _physical_postflight_candidate(
    state: runtime.BrowserAcceptanceState,
    *,
    hostile: bool,
) -> PhysicalDiscoveryCandidate:
    field_path = ("hostile_metadata_fixture_" + ("x" * 175),) if hostile else ("account_no",)
    return PhysicalDiscoveryCandidate(
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=state.workspace_id,
                connection_id=CatalogConnectionId("connection_m27"),
                asset_id=CatalogAssetId("noise.table_hostile" if hostile else "noise.table_first"),
            ),
            field_path=field_path,
        ),
        generation=runtime.EXPECTED_CATALOG_GENERATION,
        asset_qualified_name=("noise.table_hostile" if hostile else "noise.table_first"),
        native_type="text",
        definition=(
            f"{runtime.HOSTILE_METADATA_QUERY} synthetic hostile metadata"
            if hostile
            else "Synthetic first physical field"
        ),
        metadata_fingerprint=("9" if hostile else "8") * 64,
    )


def _cross_connection_candidate(
    state: runtime.BrowserAcceptanceState,
    *,
    connection_id: str,
    generation: int,
) -> PhysicalDiscoveryCandidate:
    return PhysicalDiscoveryCandidate(
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=state.workspace_id,
                connection_id=CatalogConnectionId(connection_id),
                asset_id=CatalogAssetId(runtime.CROSS_CONNECTION_HOMONYM_ASSET),
            ),
            field_path=runtime.CROSS_CONNECTION_HOMONYM_FIELD_PATH,
        ),
        generation=generation,
        asset_qualified_name=runtime.CROSS_CONNECTION_HOMONYM_ASSET,
        native_type="integer",
        definition=runtime.CROSS_CONNECTION_HOMONYM_QUERY,
        metadata_fingerprint=("1" if connection_id == runtime.PRIMARY_CONNECTION_ID else "2") * 64,
    )


def _cross_connection_governed_binding(
    state: runtime.BrowserAcceptanceState,
) -> GovernedFieldBinding:
    return GovernedFieldBinding(
        binding_id="binding_sales_order_key",
        binding_fingerprint="3" * 64,
        logical_field=LogicalFieldRef("SalesOrder.order_key"),
        physical_field=PhysicalFieldRef("sales.orders.order_id"),
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=state.workspace_id,
                connection_id=CatalogConnectionId(runtime.PRIMARY_CONNECTION_ID),
                asset_id=CatalogAssetId(runtime.CROSS_CONNECTION_HOMONYM_ASSET),
            ),
            field_path=runtime.CROSS_CONNECTION_HOMONYM_FIELD_PATH,
        ),
        mapping_version=1,
        mapping_approval_decision_id="ground-truth-map-order-key-v1",
        physical_type=PhysicalValueType.INTEGER,
        evidence_status=ExecutableEvidenceStatus.CURRENT,
        catalog_generation=runtime.EXPECTED_CATALOG_GENERATION,
        catalog_generation_fingerprint="4" * 64,
        asset_qualified_name=runtime.CROSS_CONNECTION_HOMONYM_ASSET,
        asset_metadata_fingerprint="5" * 64,
        field_metadata_fingerprint="6" * 64,
        field_definition_fingerprint="7" * 64,
        field_terms_fingerprint="8" * 64,
        native_type="integer",
        definition=runtime.CROSS_CONNECTION_HOMONYM_QUERY,
        nullable=False,
        is_part_of_key=True,
        signals=SearchSignalBreakdown.create(
            (
                SearchSignal(
                    code=SearchSignalCode.EXACT_PHYSICAL_FIELD,
                    value=1_000,
                ),
            )
        ),
    )


def test_state_is_exact_bounded_and_summary_excludes_scope_and_secrets() -> None:
    state = _state(policy_state="enabled")

    restored = runtime.BrowserAcceptanceState.from_json(state.to_json())
    summary = runtime._safe_summary(restored)
    payload = json.loads(summary)

    assert restored == state
    assert payload["cardinality_profile"] == "large"
    assert payload["physical_asset_count"] == 5_434
    assert payload["physical_field_count"] == 41_028
    assert payload["governed_mapping_count"] == 31
    assert payload["expected_live_model"] == runtime.PINNED_MODEL
    assert (
        payload["expected_live_configuration_fingerprint"]
        == _expected_live_configuration().fingerprint
    )
    policy_configuration_fingerprint = state.ai_policy_configuration_fingerprint
    assert policy_configuration_fingerprint is not None
    for forbidden in (
        state.workspace_id,
        state.registry_fingerprint,
        state.governed_catalog_generation_vector_fingerprint,
        state.physical_catalog_generation_vector_fingerprint,
        policy_configuration_fingerprint,
        "OPENAI_API_KEY",
        "password",
    ):
        assert forbidden not in summary


def test_state_rejects_wrong_scale_and_duplicate_json_keys() -> None:
    with pytest.raises(ValueError, match="cardinality"):
        replace(_state(), field_count=40_734)
    with pytest.raises(ValueError, match="cardinality"):
        replace(_state(), connection_count=2)
    with pytest.raises(ValueError, match="registry evidence"):
        replace(_state(), evidence_head_revision=3)
    with pytest.raises(ValueError, match="registry evidence"):
        replace(_state(), catalog_generation=8)

    raw = _state().to_json().decode("ascii")
    duplicate = raw.replace(
        '"schema_version":1',
        '"schema_version":1,"schema_version":1',
        1,
    ).encode("ascii")
    with pytest.raises(runtime.BrowserAcceptanceSetupError):
        runtime.BrowserAcceptanceState.from_json(duplicate)

    extra = raw.replace("}\n", ',"unexpected":"must-not-be-accepted"}\n').encode("ascii")
    with pytest.raises(runtime.BrowserAcceptanceSetupError):
        runtime.BrowserAcceptanceState.from_json(extra)


def test_small_state_round_trips_exact_10_tables_75_fields_without_schema_fork() -> None:
    state = _state(cardinality_profile="small")

    restored = runtime.BrowserAcceptanceState.from_json(state.to_json())
    payload = json.loads(runtime._safe_summary(restored))

    assert restored == state
    assert restored.cardinality_profile == "small"
    assert payload["cardinality_profile"] == "small"
    assert payload["physical_asset_count"] == 10
    assert payload["physical_field_count"] == 75
    assert json.loads(state.to_json())["schema_version"] == 1


def test_two_connection_state_round_trips_exact_dynamic_aggregate_without_changing_large() -> None:
    state = _state(cardinality_profile="two_connections")

    restored = runtime.BrowserAcceptanceState.from_json(state.to_json())
    payload = json.loads(runtime._safe_summary(restored))

    assert restored == state
    assert restored.cardinality_profile == "two_connections"
    assert (
        restored.connection_count,
        restored.asset_count,
        restored.field_count,
    ) == (2, 11, 76)
    assert payload["cardinality_profile"] == "two_connections"
    assert payload["connection_count"] == 2
    assert payload["physical_asset_count"] == 11
    assert payload["physical_field_count"] == 76
    assert _state().cardinality_profile == "large"
    assert (
        _state().connection_count,
        _state().asset_count,
        _state().field_count,
    ) == (1, 5_434, 41_028)


def test_owner_only_files_reject_broader_permissions(tmp_path: Path) -> None:
    secret = tmp_path / "runtime.key"
    runtime._write_owner_only(secret, b"bounded-M27-secret\n")

    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert runtime._read_owner_only(secret, maximum=1024) == b"bounded-M27-secret\n"

    secret.chmod(0o640)
    with pytest.raises(runtime.BrowserAcceptanceSetupError):
        runtime._read_owner_only(secret, maximum=1024)


def test_acceptance_seed_environment_is_key_free_for_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-propagate")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "must-not-propagate")
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL",
        "must-not-propagate",
    )
    monkeypatch.setenv("HTTPS_PROXY", "must-not-propagate")

    environment = runtime._acceptance_environment(
        "schemabridge_m27_browser_012345abcdef",
        "m27-browser-012345abcdef",
        runtime.LOCAL_SUBJECT,
        "generated-audit-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789",
    )

    assert "OPENAI_API_KEY" not in environment
    assert "DATAHUB_GMS_TOKEN" not in environment
    assert "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL" not in environment
    assert "HTTPS_PROXY" not in environment
    assert environment["SCHEMABRIDGE_TEST_M27_BROWSER_SEED"] == "1"
    assert environment["SCHEMABRIDGE_TEST_M27_CARDINALITY_PROFILE"] == "large"
    assert environment["SCHEMABRIDGE_TEST_M26_RETAIN_DATABASE"] == "1"

    small_environment = runtime._acceptance_environment(
        "schemabridge_m27_browser_012345abcdef",
        "m27-browser-012345abcdef",
        runtime.LOCAL_SUBJECT,
        "generated-audit-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789",
        cardinality_profile="small",
    )
    assert small_environment["SCHEMABRIDGE_TEST_M27_CARDINALITY_PROFILE"] == "small"
    assert "OPENAI_API_KEY" not in small_environment

    two_connection_environment = runtime._acceptance_environment(
        "schemabridge_m27_browser_012345abcdef",
        "m27-browser-012345abcdef",
        runtime.LOCAL_SUBJECT,
        "generated-audit-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789",
        cardinality_profile="two_connections",
    )
    assert (
        two_connection_environment["SCHEMABRIDGE_TEST_M27_CARDINALITY_PROFILE"] == "two_connections"
    )
    assert "OPENAI_API_KEY" not in two_connection_environment


def test_post_seed_analyze_has_exact_static_quoted_scope_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, statement: object) -> None:
            captured["statement"] = statement

    def connect(dsn: str, *, autocommit: bool) -> _Connection:
        captured["dsn"] = dsn
        captured["autocommit"] = autocommit
        return _Connection()

    monkeypatch.setattr(
        "scripts.m27_browser_acceptance_runtime.psycopg.connect",
        connect,
    )

    runtime._analyze_catalog_statistics("schemabridge_m27_browser_012345abcdef")

    statement = captured["statement"]
    assert hasattr(statement, "as_string")
    assert statement.as_string() == (
        'ANALYZE "schemabridge_control"."catalog_connections", '
        '"schemabridge_control"."catalog_generations", '
        '"schemabridge_control"."catalog_assets", '
        '"schemabridge_control"."catalog_fields"'
    )
    assert captured["dsn"] == (
        "postgresql://schemabridge_migrator:schemabridge_migrator@"
        "127.0.0.1:55434/schemabridge_m27_browser_012345abcdef"
    )
    assert captured["autocommit"] is True


def test_stale_scope_witness_uses_api_for_direct_select_and_runtime_for_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    api_dsn = runtime._role_dsn("schemabridge_api", state.database)
    runtime_dsn = runtime._runtime_dsn(state.database)
    responses = {
        api_dsn: [(state.catalog_generation,)],
        runtime_dsn: [
            (
                state.physical_catalog_generation_vector_fingerprint,
                state.connection_count,
                state.asset_count,
                state.field_count,
            ),
            (
                state.registry_generation,
                state.registry_version,
                state.registry_fingerprint,
                None,
                None,
                None,
                None,
                None,
                state.governed_catalog_generation_vector_fingerprint,
                state.governed_mapping_count,
            ),
        ],
    }
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    class _Result:
        def __init__(self, row: tuple[object, ...]) -> None:
            self._row = row

        def fetchone(self) -> tuple[object, ...]:
            return self._row

    class _Connection:
        def __init__(self, dsn: str) -> None:
            self._dsn = dsn

        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(
            self,
            statement: str,
            params: tuple[str, ...],
        ) -> _Result:
            normalized_statement = " ".join(statement.split())
            calls.append((self._dsn, normalized_statement, params))
            return _Result(responses[self._dsn].pop(0))

    def connect(dsn: str) -> _Connection:
        return _Connection(dsn)

    monkeypatch.setattr(
        "scripts.m27_browser_acceptance_runtime.psycopg.connect",
        connect,
    )

    witness = runtime._read_stale_scope_witness(state)

    assert witness == _stale_witness(state)
    assert tuple(dsn for dsn, _, _ in calls) == (
        api_dsn,
        runtime_dsn,
        runtime_dsn,
    )
    assert "FROM schemabridge_control.catalog_connections" in calls[0][1]
    assert "load_physical_discovery_scope" in calls[1][1]
    assert "load_query_studio_governance_scope" in calls[2][1]
    assert all("catalog_connections" not in statement for _dsn, statement, _params in calls[1:])
    assert calls[0][2] == (
        state.workspace_id,
        runtime.CATALOG_SCOPE,
        runtime.PRIMARY_CONNECTION_ID,
    )
    assert (
        calls[1][2]
        == calls[2][2]
        == (
            state.workspace_id,
            runtime.CATALOG_SCOPE,
            runtime.REGISTRY_ID,
        )
    )
    assert responses == {api_dsn: [], runtime_dsn: []}


def test_catalog_witnesses_reject_generation_or_scope_mismatch_with_valid_hashes() -> None:
    expected_scope = SemanticRegistryScope(
        workspace_id="workspace-m27",
        catalog_scope=runtime.CATALOG_SCOPE,
        registry_id=runtime.REGISTRY_ID,
    )
    other_scope = expected_scope.model_copy(update={"workspace_id": "workspace-other"})
    expected_generation = frozenset({runtime.EXPECTED_CATALOG_GENERATION})

    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="witnesses differ"):
        runtime._validate_catalog_witnesses(
            expected_scope=expected_scope,
            physical_scope=expected_scope,
            governed_scope=expected_scope,
            expected_physical_generations=expected_generation,
            physical_generations=frozenset({runtime.EXPECTED_CATALOG_GENERATION - 1}),
            governed_generations=expected_generation,
            physical_vector="1" * 64,
            governed_vector="2" * 64,
        )
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="witnesses differ"):
        runtime._validate_catalog_witnesses(
            expected_scope=expected_scope,
            physical_scope=expected_scope,
            governed_scope=other_scope,
            expected_physical_generations=expected_generation,
            physical_generations=expected_generation,
            governed_generations=expected_generation,
            physical_vector="1" * 64,
            governed_vector="2" * 64,
        )


def test_two_connection_homonym_is_physically_ambiguous_but_only_primary_is_governed() -> None:
    state = _state(cardinality_profile="two_connections")
    primary = _cross_connection_candidate(
        state,
        connection_id=runtime.PRIMARY_CONNECTION_ID,
        generation=runtime.EXPECTED_CATALOG_GENERATION,
    )
    secondary = _cross_connection_candidate(
        state,
        connection_id=runtime.SECONDARY_CONNECTION_ID,
        generation=runtime.EXPECTED_SECONDARY_CATALOG_GENERATION,
    )
    page = PhysicalFieldDiscoveryPage(
        items=(primary, secondary),
        page_size=50,
    )
    governed = _cross_connection_governed_binding(state)

    generations = runtime._validate_cross_connection_homonym_witness(
        page,
        governed_items=(governed,),
        scope=_postflight_scope(state),
    )

    assert generations == frozenset(
        {
            runtime.EXPECTED_CATALOG_GENERATION,
            runtime.EXPECTED_SECONDARY_CATALOG_GENERATION,
        }
    )
    assert {item.locator.asset.connection_id.root for item in page.items} == {
        runtime.PRIMARY_CONNECTION_ID,
        runtime.SECONDARY_CONNECTION_ID,
    }
    assert {
        (
            item.asset_qualified_name,
            item.locator.field_path,
        )
        for item in page.items
    } == {
        (
            runtime.CROSS_CONNECTION_HOMONYM_ASSET,
            runtime.CROSS_CONNECTION_HOMONYM_FIELD_PATH,
        )
    }
    assert all(item.status is PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW for item in page.items)
    assert governed.locator.asset.connection_id.root == runtime.PRIMARY_CONNECTION_ID
    labels = tuple(_physical_discovery_label(item) for item in page.items)
    assert labels == (
        "[warehouse-primary] sales.orders.order_id · needs_mapping_review",
        "[warehouse-shadow] sales.orders.order_id · needs_mapping_review",
    )
    assert len(set(labels)) == 2

    duplicate_primary = secondary.model_copy(
        update={
            "locator": secondary.locator.model_copy(
                update={
                    "asset": secondary.locator.asset.model_copy(
                        update={"connection_id": CatalogConnectionId(runtime.PRIMARY_CONNECTION_ID)}
                    )
                }
            )
        }
    )
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="homonym witness"):
        runtime._validate_cross_connection_homonym_witness(
            PhysicalFieldDiscoveryPage(
                items=(primary, duplicate_primary),
                page_size=50,
            ),
            governed_items=(governed,),
            scope=_postflight_scope(state),
        )


def test_postflight_metrics_are_integer_only_below_the_strict_budget() -> None:
    metrics = runtime.BrowserAcceptancePostflightMetrics(
        cold_physical_empty_ms=0,
        physical_hostile_exact_ms=1,
        governed_traversal_ms=4_999,
    )

    assert metrics.governed_traversal_ms == 4_999
    assert (
        runtime._bounded_postflight_milliseconds(
            4_999_999_999,
            operation="test postflight",
        )
        == 4_999
    )
    for invalid in (-1, 5_000, True, 1.5):
        with pytest.raises(ValueError, match="timing"):
            runtime.BrowserAcceptancePostflightMetrics(
                cold_physical_empty_ms=invalid,  # type: ignore[arg-type]
                physical_hostile_exact_ms=1,
                governed_traversal_ms=1,
            )
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="5000 ms"):
        runtime._bounded_postflight_milliseconds(
            5_000_000_000,
            operation="test postflight",
        )


def test_status_runs_postflight_before_state_query_and_only_summarizes_integer_ms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state = _state()
    state_dir = _state_dir(tmp_path, state)
    metrics = runtime.BrowserAcceptancePostflightMetrics(
        cold_physical_empty_ms=101,
        physical_hostile_exact_ms=202,
        governed_traversal_ms=303,
    )
    events: list[str] = []

    def postflight(observed: runtime.BrowserAcceptanceState) -> object:
        assert observed == state
        events.append("postflight")
        return metrics

    def query_state(*args: object, **kwargs: object) -> runtime.BrowserAcceptanceState:
        events.append("query_state")
        return state

    monkeypatch.setattr(runtime, "_run_catalog_postflight", postflight)
    monkeypatch.setattr(runtime, "_query_state", query_state)

    observed = runtime._status(state_dir)
    payload = json.loads(runtime._safe_summary(observed))
    persisted = json.loads(state.to_json())

    assert events == ["postflight", "query_state"]
    assert observed.postflight_metrics == metrics
    assert payload["postflight_ms"] == {
        "cold_physical_empty": 101,
        "governed_traversal": 303,
        "physical_hostile_exact": 202,
    }
    assert all(type(value) is int for value in payload["postflight_ms"].values())
    assert "postflight_metrics" not in persisted
    assert "postflight_ms" not in persisted


def test_catalog_postflight_times_exact_physical_and_pooled_governed_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    scope = _postflight_scope(state)
    scope_snapshot = QueryStudioScopeSnapshot(
        scope=scope,
        registry_version=state.registry_version,
        registry_fingerprint=state.registry_fingerprint,
        pointer_generation=state.registry_generation,
        pointer_fingerprint="7" * 64,
        evidence_head_revision=state.evidence_head_revision,
        evidence_baseline_revision=state.evidence_baseline_revision,
        evidence_baseline_fingerprint="6" * 64,
        catalog_generation_vector_fingerprint=(
            state.governed_catalog_generation_vector_fingerprint
        ),
    )
    regular_candidate = _physical_postflight_candidate(state, hostile=False)
    hostile_candidate = _physical_postflight_candidate(state, hostile=True)
    physical_queries: list[str | None] = []
    governed_page_sizes: list[int] = []
    events: list[str] = []

    class _PhysicalReader:
        def search(
            self,
            request: PhysicalFieldDiscoveryRequest,
        ) -> PhysicalFieldDiscoveryPage:
            query = request.query
            normalized = None if query is None else query.root
            physical_queries.append(normalized)
            events.append(f"physical:{normalized or 'empty'}")
            if normalized is None:
                return PhysicalFieldDiscoveryPage(
                    items=(regular_candidate,),
                    page_size=1,
                    next_cursor=PhysicalDiscoveryCursor("next-physical-page"),
                )
            assert normalized == runtime.HOSTILE_METADATA_QUERY
            return PhysicalFieldDiscoveryPage(
                items=(hostile_candidate,),
                page_size=1,
                next_cursor=None,
            )

    class _PhysicalFactory:
        @classmethod
        def from_signing_key(cls, **kwargs: object) -> _PhysicalReader:
            dsn = kwargs["dsn"]
            assert isinstance(dsn, str)
            assert dsn.endswith(state.database)
            events.append("physical_reader")
            return _PhysicalReader()

    class _Pool:
        def __init__(self, settings: object) -> None:
            self.settings = settings
            self.opened = False
            events.append("pool_created")

        def __enter__(self) -> _Pool:
            self.opened = True
            events.append("pool_opened")
            return self

        def __exit__(self, *args: object) -> None:
            self.opened = False
            events.append("pool_closed")

    class _GovernedReader:
        def __init__(
            self,
            dsn: str,
            *,
            connection_provider: object,
        ) -> None:
            assert dsn.endswith(state.database)
            assert isinstance(connection_provider, _Pool)
            assert connection_provider.opened
            self.pool = connection_provider
            events.append("governed_reader")

        def search(self, request: GovernedBindingFactsRequest) -> SimpleNamespace:
            assert self.pool.opened
            page_size = request.page_size
            after = request.after
            start = 0 if after is None else int(after.binding_id.rsplit("_", maxsplit=1)[1]) + 1
            if start:
                assert request.expected_scope == scope_snapshot
            end = min(start + page_size, runtime.EXPECTED_GOVERNED_MAPPING_COUNT)
            governed_page_sizes.append(page_size)
            events.append(f"governed:{page_size}:{start}")
            items = tuple(
                SimpleNamespace(
                    binding_id=f"binding_{index:03d}",
                    catalog_generation=runtime.EXPECTED_CATALOG_GENERATION,
                )
                for index in range(start, end)
            )
            next_key = (
                GovernedSearchKey(
                    score=0,
                    logical_field=LogicalFieldRef(f"Customer.field_{end - 1:03d}"),
                    binding_id=f"binding_{end - 1:03d}",
                    scope_fingerprint=scope_snapshot.fingerprint,
                    request_fingerprint=request.continuation_request_fingerprint,
                    binding_facts_fingerprint=request.request_fingerprint,
                )
                if end < runtime.EXPECTED_GOVERNED_MAPPING_COUNT
                else None
            )
            return SimpleNamespace(
                scope=scope_snapshot,
                items=items,
                page_size=page_size,
                rows_read=len(items) + (1 if next_key is not None else 0),
                next_key=next_key,
            )

    ticks = iter(
        (
            1_000_000_000,
            1_123_456_789,
            2_000_000_000,
            2_234_567_890,
            3_000_000_000,
            3_345_678_901,
        )
    )
    clock_index = 0

    def monotonic_ns() -> int:
        nonlocal clock_index
        events.append(f"clock:{clock_index}")
        clock_index += 1
        return next(ticks)

    monkeypatch.setattr(runtime, "PostgresPhysicalFieldDiscovery", _PhysicalFactory)
    monkeypatch.setattr(runtime, "PostgresControlPool", _Pool)
    monkeypatch.setattr(runtime, "PostgresGovernedBindingFactsSearch", _GovernedReader)
    monkeypatch.setattr(time, "monotonic_ns", monotonic_ns)

    metrics = runtime._run_catalog_postflight(state)

    assert metrics == runtime.BrowserAcceptancePostflightMetrics(
        cold_physical_empty_ms=123,
        physical_hostile_exact_ms=234,
        governed_traversal_ms=345,
    )
    assert physical_queries == [None, runtime.HOSTILE_METADATA_QUERY]
    assert governed_page_sizes.count(1) == 31
    assert governed_page_sizes.count(17) == 2
    assert governed_page_sizes.count(50) == 1
    assert events.index("pool_opened") < events.index("clock:4")
    assert events.index("clock:5") < events.index("pool_closed")


def test_physical_postflight_rejects_any_executable_status() -> None:
    state = _state()
    candidate = _physical_postflight_candidate(state, hostile=False).model_copy(
        update={"status": "current"}
    )

    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="non-executable"):
        runtime._validate_non_executable_physical_candidate(
            candidate,
            scope=_postflight_scope(state),
        )


def test_fake_runtime_omits_key_and_live_runtime_reuses_only_process_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state = _state(policy_state="enabled")
    state_dir = _state_dir(tmp_path, state)
    monkeypatch.setenv("OPENAI_API_KEY", "existing-process-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.invalid")
    monkeypatch.setenv("ALL_PROXY", "http://attacker.invalid")

    fake = runtime._runtime_environment(state_dir, state, ai_mode="fake")
    live = runtime._runtime_environment(state_dir, state, ai_mode="live")

    assert "OPENAI_API_KEY" not in fake
    assert live["OPENAI_API_KEY"] == "existing-process-key"
    assert live["SCHEMABRIDGE_QUERY_STUDIO_AI_MODEL"] == runtime.PINNED_MODEL
    assert live["SCHEMABRIDGE_QUERY_STUDIO_AI_REGION"] == "global"
    for forbidden in ("OPENAI_BASE_URL", "ALL_PROXY"):
        assert forbidden not in fake
        assert forbidden not in live


def test_reviewed_model_selector_is_closed_and_configuration_is_model_specific(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert runtime.REVIEWED_MODEL_SNAPSHOTS == (
        "gpt-5-nano-2025-08-07",
        "gpt-5.4-nano-2026-03-17",
        "gpt-5.6-luna",
    )
    configurations = tuple(
        _expected_live_configuration(model) for model in runtime.REVIEWED_MODEL_SNAPSHOTS
    )
    assert tuple(item.model_snapshot for item in configurations) == (
        runtime.REVIEWED_MODEL_SNAPSHOTS
    )
    assert len({item.fingerprint for item in configurations}) == len(configurations)

    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state = _state()
    state_dir = _state_dir(tmp_path, state)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-read")
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="reviewed"):
        _expected_live_configuration("attacker/model")
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="reviewed"):
        runtime._runtime_environment(
            state_dir,
            state,
            ai_mode="live",
            model_snapshot="attacker/model",
        )


@pytest.mark.parametrize(
    "command",
    ("status", "streamlit", "evaluate-live", "policy-guide"),
)
def test_model_selecting_commands_accept_only_the_closed_reviewed_order(
    command: str,
) -> None:
    selected = runtime.REVIEWED_MODEL_SNAPSHOTS[0]
    parsed = runtime._parser().parse_args((command, "--ai-model", selected))

    assert parsed.ai_model == selected
    with pytest.raises(SystemExit):
        runtime._parser().parse_args((command, "--ai-model", "attacker/model"))


def test_provider_free_attestation_commands_are_closed_and_dispatch_exact_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "m27-state"
    report = Path(
        f"reports/m27-query-studio-live-history/campaign-ledger-attestation-{'a' * 64}.json"
    )
    attest = runtime._parser().parse_args(("--state-dir", str(state_dir), "attest-live"))
    verify = runtime._parser().parse_args(
        (
            "--state-dir",
            str(state_dir),
            "verify-live-attestation",
            "--attestation-json",
            str(report),
        )
    )
    assert attest.command == "attest-live"
    assert verify.command == "verify-live-attestation"
    assert verify.attestation_json == report

    calls: list[tuple[Path, Path | None]] = []

    def attest_live(configured_state_dir: Path) -> str:
        calls.append((configured_state_dir, None))
        return '{"verified":true}'

    def verify_live(configured_state_dir: Path, configured_report: Path) -> str:
        calls.append((configured_state_dir, configured_report))
        return '{"verified":true}'

    monkeypatch.setattr(runtime, "_attest_live", attest_live)
    monkeypatch.setattr(runtime, "_verify_live_attestation", verify_live)

    assert runtime.main(("--state-dir", str(state_dir), "attest-live")) == 0
    assert (
        runtime.main(
            (
                "--state-dir",
                str(state_dir),
                "verify-live-attestation",
                "--attestation-json",
                str(report),
            )
        )
        == 0
    )
    assert calls == [(state_dir, None), (state_dir, report)]
    assert capsys.readouterr().out.splitlines() == [
        '{"verified":true}',
        '{"verified":true}',
    ]


def test_attestation_summary_exposes_v2_witness_and_ordinal_proof_without_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    history = tmp_path / "reports/m27-query-studio-live-history"
    history.mkdir(parents=True)
    path = history / f"campaign-ledger-attestation-{'a' * 64}.json"
    path.write_text("{}")
    report = SimpleNamespace(
        schema_version=2,
        campaign=SimpleNamespace(
            logical_report_sha256="b" * 64,
            ordered_execution_witness_digest_sha256="c" * 64,
        ),
        ledger=SimpleNamespace(
            ledger_digest_sha256="d" * 64,
            ordinal_binding_digest_sha256="e" * 64,
            reservation_count=16,
            unique_compatible_matching_count=1,
        ),
    )

    rendered = runtime._attestation_summary(
        report,
        path,
        verified=True,
    )
    payload = json.loads(rendered)

    assert payload == {
        "attestation_file_sha256": "a" * 64,
        "attestation_path": str(path.relative_to(tmp_path)),
        "attestation_schema_version": 2,
        "campaign_ordered_execution_witness_digest_sha256": "c" * 64,
        "campaign_report_sha256": "b" * 64,
        "ledger_digest_sha256": "d" * 64,
        "ordinal_binding_digest_sha256": "e" * 64,
        "provider_attempts": 16,
        "unique_compatible_matching_count": 1,
        "verified": True,
    }
    assert "reservation_id" not in rendered
    assert "request_id" not in rendered


@pytest.mark.parametrize(
    ("scenario", "confirmation"),
    (
        (
            runtime.M27StaleScenario.CATALOG,
            runtime.STALE_CATALOG_CONFIRMATION,
        ),
        (
            runtime.M27StaleScenario.REGISTRY,
            runtime.STALE_REGISTRY_CONFIRMATION,
        ),
    ),
)
def test_real_stale_scenarios_are_closed_and_require_exact_confirmation_before_state_access(
    scenario: runtime.M27StaleScenario,
    confirmation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = runtime._parser().parse_args(("stale", scenario.value, "--confirm", confirmation))

    assert parsed.scenario == scenario.value
    assert parsed.confirm == confirmation
    with pytest.raises(SystemExit):
        runtime._parser().parse_args(("stale", "provider_or_sql_fake", "--confirm", confirmation))

    def must_not_load(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("state was accessed before exact drift confirmation")

    monkeypatch.setattr(runtime, "_load_state", must_not_load)
    with pytest.raises(
        runtime.BrowserAcceptanceSetupError,
        match="exact M27 stale-preview confirmation",
    ):
        runtime._apply_stale_scenario(
            tmp_path / ".local/m27-browser",
            scenario,
            "APPLY SOMETHING ELSE",
        )


def test_stale_scenario_rejects_an_already_drifted_database_before_any_second_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state = _state()
    state_dir = _state_dir(tmp_path, state)
    monkeypatch.setattr(
        runtime,
        "_read_stale_scope_witness",
        lambda observed: _stale_witness(
            observed,
            catalog_generation=observed.catalog_generation + 1,
            physical_vector="f" * 64,
        ),
    )

    def must_not_write(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a second controlled drift was attempted")

    monkeypatch.setattr(runtime, "_apply_stale_catalog_drift", must_not_write)
    with pytest.raises(
        runtime.BrowserAcceptanceSetupError,
        match="untouched retained baseline",
    ):
        runtime._apply_stale_scenario(
            state_dir,
            runtime.M27StaleScenario.CATALOG,
            runtime.STALE_CATALOG_CONFIRMATION,
        )


def test_stale_catalog_clones_and_promotes_one_real_generation_without_provider_or_query_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    refresh_id = "refresh-m27-stale-catalog"
    lease = SimpleNamespace(
        indexer_id=runtime.STALE_CATALOG_INDEXER,
        fencing_token=7,
    )
    events: list[str] = []
    requested_commands: list[object] = []
    persisted_pages: list[object] = []

    class _RefreshStore:
        def __init__(
            self,
            dsn: str,
            *,
            application_name: str,
        ) -> None:
            self.role = "api" if "schemabridge_api:schemabridge_api" in dsn else "catalog"
            assert dsn.endswith(state.database)
            assert application_name == f"schemabridge-control-{self.role}"
            events.append(f"store:{self.role}")

        def request(self, command: object) -> SimpleNamespace:
            assert self.role == "api"
            requested_commands.append(command)
            events.append("request")
            return SimpleNamespace(refresh=SimpleNamespace(refresh_id=refresh_id))

        def claim_next(self, **kwargs: object) -> SimpleNamespace:
            assert self.role == "catalog"
            assert kwargs["indexer_id"] == runtime.STALE_CATALOG_INDEXER
            assert str(kwargs["lease_duration"]) == "0:05:00"
            assert len(str(kwargs["lease_capability"])) >= 32
            events.append("claim")
            return SimpleNamespace(refresh_id=refresh_id, lease=lease)

        def begin_staging(
            self,
            workspace_id: str,
            observed_refresh_id: str,
            **kwargs: object,
        ) -> SimpleNamespace:
            assert workspace_id == state.workspace_id
            assert observed_refresh_id == refresh_id
            assert kwargs["indexer_id"] == runtime.STALE_CATALOG_INDEXER
            assert kwargs["fencing_token"] == 7
            events.append("begin")
            return SimpleNamespace(
                base_generation=state.catalog_generation,
                target_generation=state.catalog_generation + 1,
            )

        def persist_page(
            self,
            workspace_id: str,
            observed_refresh_id: str,
            **kwargs: object,
        ) -> SimpleNamespace:
            assert workspace_id == state.workspace_id
            assert observed_refresh_id == refresh_id
            persisted_pages.append(kwargs["page"])
            events.append("persist")
            return SimpleNamespace()

        def complete(
            self,
            workspace_id: str,
            observed_refresh_id: str,
            **kwargs: object,
        ) -> SimpleNamespace:
            assert workspace_id == state.workspace_id
            assert observed_refresh_id == refresh_id
            assert kwargs["expected_base_generation"] == state.catalog_generation
            events.append("complete")
            return SimpleNamespace(target_generation=state.catalog_generation + 1)

    class _GovernedReader:
        def __init__(self, dsn: str) -> None:
            assert dsn.endswith(state.database)
            events.append("governed-reader")

        def search(
            self,
            request: GovernedBindingFactsRequest,
        ) -> SimpleNamespace:
            assert request.scope == _postflight_scope(state)
            assert request.page_size == state.governed_mapping_count
            events.append("governed-search")
            return SimpleNamespace(
                items=tuple(
                    SimpleNamespace(catalog_generation=state.catalog_generation + 1)
                    for _ in range(state.governed_mapping_count)
                ),
                next_key=None,
            )

    monkeypatch.setattr(runtime, "PostgresCatalogRefreshStore", _RefreshStore)
    monkeypatch.setattr(
        runtime,
        "PostgresGovernedBindingFactsSearch",
        _GovernedReader,
    )
    monkeypatch.setattr(
        runtime,
        "_read_stale_scope_witness",
        lambda observed: _stale_witness(
            observed,
            catalog_generation=observed.catalog_generation + 1,
            physical_vector="f" * 64,
        ),
    )
    monkeypatch.setattr(
        secrets,
        "token_urlsafe",
        lambda length: "ephemeral-lease-capability-0123456789-ABCDEFGH",
    )

    result = runtime._apply_stale_catalog_drift(state)
    payload = json.loads(result.to_json())

    assert len(requested_commands) == 1
    command = requested_commands[0]
    assert isinstance(command, CatalogRefreshCommand)
    assert command.workspace_id == state.workspace_id
    assert command.connection_id == CatalogConnectionId(runtime.PRIMARY_CONNECTION_ID)
    assert command.mode is CatalogRefreshMode.DELTA
    assert command.requested_by == runtime.STALE_CATALOG_ACTOR
    assert len(command.idempotency_digest) == 64
    assert len(persisted_pages) == 1
    page = persisted_pages[0]
    assert isinstance(page, CatalogSourcePage)
    assert page.mode is CatalogRefreshMode.DELTA
    assert page.changes == ()
    assert page.source_complete
    assert events == [
        "store:api",
        "request",
        "store:catalog",
        "claim",
        "begin",
        "persist",
        "complete",
        "governed-reader",
        "governed-search",
    ]
    assert payload == {
        "drift": {
            "after": 10,
            "before": 9,
            "dimension": "catalog_generation",
        },
        "external_ai_called": False,
        "query_sql_executed": False,
        "scenario": "stale_catalog",
        "stale_preview_confirmable": False,
    }


def test_stale_registry_uses_approved_audited_rollback_and_removes_governed_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state = _state()
    state_dir = _state_dir(tmp_path, state)
    scope = _postflight_scope(state)
    current_pointer = SimpleNamespace(
        generation=state.registry_generation,
        registry_version=state.registry_version,
        registry_fingerprint=state.registry_fingerprint,
        scope=scope,
    )
    rollback_pointer = SimpleNamespace(
        generation=1,
        registry_version=1,
        registry_fingerprint="a" * 64,
        scope=scope,
    )
    rollback_transition = SimpleNamespace(
        id="registry-transition-v1-" + ("1" * 64),
        active_pointer=rollback_pointer,
    )
    committed_pointer = SimpleNamespace(
        generation=state.registry_generation + 1,
        registry_version=1,
        registry_fingerprint="a" * 64,
        scope=scope,
    )
    proposal = object()
    approval = object()
    events: list[str] = []

    class _Control:
        def __init__(self, dsn: str, keys: object, active_key: str) -> None:
            assert dsn.endswith(state.database)
            assert keys == {"v1": b"audit-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"}
            assert active_key == "v1"
            events.append("control")

        def load_active(self, observed_scope: SemanticRegistryScope) -> object:
            assert observed_scope == scope
            events.append("load-active")
            return current_pointer

        def list_transitions(
            self,
            observed_scope: SemanticRegistryScope,
            *,
            limit: int,
        ) -> tuple[object, ...]:
            assert observed_scope == scope
            assert limit == 100
            events.append("list-transitions")
            return (rollback_transition,)

    class _ReadConfig:
        @classmethod
        def from_env_file(cls, path: Path) -> str:
            assert path == (tmp_path / ".local/datahub/mcp.env").resolve()
            events.append("reader-config")
            return "read-only-datahub-config"

    class _Versions:
        def __init__(self, config: object) -> None:
            assert config == "read-only-datahub-config"
            events.append("versions")

    class _PrepareRollback:
        def __init__(
            self,
            control: object,
            versions: object,
            observed_scope: SemanticRegistryScope,
        ) -> None:
            assert isinstance(control, _Control)
            assert isinstance(versions, _Versions)
            assert observed_scope == scope

        def execute(self, transition_id: str) -> object:
            assert transition_id == rollback_transition.id
            events.append("prepare-rollback")
            return proposal

    class _PrepareApproval:
        def execute(self, observed_proposal: object, **kwargs: object) -> object:
            assert observed_proposal is proposal
            assert kwargs["actor"] == runtime.STALE_REGISTRY_ACTOR
            assert kwargs["confirmation"] is (
                RegistryActivationConfirmation.ROLLBACK_TO_APPROVED_REGISTRY_VERSION
            )
            events.append("approve")
            return approval

    class _Commit:
        def __init__(self, control: object, versions: object) -> None:
            assert isinstance(control, _Control)
            assert isinstance(versions, _Versions)

        def execute(
            self,
            observed_proposal: object,
            observed_approval: object,
            **kwargs: object,
        ) -> SimpleNamespace:
            assert observed_proposal is proposal
            assert observed_approval is approval
            committed_at = kwargs["committed_at"]
            assert isinstance(committed_at, datetime)
            assert committed_at.tzinfo is not None
            events.append("commit")
            return SimpleNamespace(transition=SimpleNamespace(active_pointer=committed_pointer))

    monkeypatch.setattr(runtime, "PostgresRegistryControlStore", _Control)
    monkeypatch.setattr(runtime, "DataHubRegistryReadConfig", _ReadConfig)
    monkeypatch.setattr(runtime, "DataHubRegistryVersionReader", _Versions)
    monkeypatch.setattr(runtime, "PrepareRegistryRollback", _PrepareRollback)
    monkeypatch.setattr(
        runtime,
        "PrepareRegistryActivationApproval",
        _PrepareApproval,
    )
    monkeypatch.setattr(runtime, "CommitRegistryActivation", _Commit)
    monkeypatch.setattr(
        runtime,
        "_read_stale_scope_witness",
        lambda observed: _stale_witness(
            observed,
            governance_available=False,
        ),
    )

    result = runtime._apply_stale_registry_drift(state_dir, state)
    payload = json.loads(result.to_json())

    assert events == [
        "control",
        "load-active",
        "list-transitions",
        "reader-config",
        "versions",
        "prepare-rollback",
        "approve",
        "commit",
    ]
    assert payload == {
        "drift": {
            "after": 3,
            "before": 2,
            "dimension": "registry_pointer_generation",
        },
        "external_ai_called": False,
        "query_sql_executed": False,
        "scenario": "stale_registry",
        "stale_preview_confirmable": False,
    }


def test_small_fake_runtime_can_serve_on_a_separate_port_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state = _state(cardinality_profile="small")
    state_dir = _state_dir(tmp_path, state)
    monkeypatch.setattr(runtime, "_query_state", lambda *args, **kwargs: state)
    captured: dict[str, object] = {}

    class _ExecCalled(RuntimeError):
        pass

    def fake_execve(
        executable: object,
        arguments: tuple[str, ...],
        environment: dict[str, str],
    ) -> None:
        captured.update(
            executable=executable,
            arguments=arguments,
            environment=environment,
        )
        raise _ExecCalled

    monkeypatch.setattr(os, "chdir", lambda path: captured.update(chdir=path))
    monkeypatch.setattr(os, "execve", fake_execve)

    with pytest.raises(_ExecCalled):
        runtime._serve_streamlit(state_dir, ai_mode="fake", port=8511)

    arguments = captured["arguments"]
    environment = captured["environment"]
    assert isinstance(arguments, tuple)
    assert isinstance(environment, dict)
    assert arguments[arguments.index("--server.port") + 1] == "8511"
    assert environment["SCHEMABRIDGE_LOCAL_WORKSPACE"] == state.local_workspace
    assert environment["SCHEMABRIDGE_QUERY_STUDIO_AI_MODE"] == "fake"
    assert "OPENAI_API_KEY" not in environment
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="port"):
        runtime._serve_streamlit(state_dir, ai_mode="fake", port=1_023)


@pytest.mark.parametrize(
    ("model_snapshot", "prompt_version", "schema_version", "facts_fingerprint"),
    (
        (
            "gpt-5-nano-2025-08-07",
            "m27-openai-prompts-v16",
            "m27-query-studio-v10",
            "4c0b4328c614aa4add1f3ae5efecdb2fab8f2d9bf6616ec784e4734a470676cf",
        ),
        (
            "gpt-5.4-nano-2026-03-17",
            "m27-openai-prompts-v16",
            "m27-query-studio-v10",
            "6a828e54d3a415a0c1335344f2acbd28e823e9f9fe9a58fa819b30c986987a3c",
        ),
        (
            "gpt-5.6-luna",
            "m27-openai-prompts-v16",
            "m27-query-studio-v10",
            "91d310bb8801f21461e272a2b28bf6f9255973c4162e3cc7e7a01950ba7ed40a",
        ),
    ),
)
def test_expected_live_configuration_matches_the_real_key_free_adapter(
    model_snapshot: str,
    prompt_version: str,
    schema_version: str,
    facts_fingerprint: str,
) -> None:
    class _NoProviderClient:
        @property
        def responses(self) -> NoReturn:
            raise AssertionError("provider must not be accessed")

    config = OpenAIResponsesConfig.for_model(
        model_snapshot,
        region=OpenAIRegion(runtime.PINNED_REGION),
    )
    expected = _expected_live_configuration(model_snapshot)
    adapter = OpenAIQueryStudioIntentAdapter(
        OpenAIResponsesBoundary(_NoProviderClient(), config),
        safety_identifier="sb_ai_v1_" + ("a" * 32),
        matcher_version=runtime.MATCHER_VERSION,
        semantic_scope_fingerprint=(expected.public_metadata_semantic_scope_fingerprint),
        public_metadata_registry_fingerprint=(expected.public_metadata_registry_fingerprint),
    )

    assert expected == adapter.configuration
    assert expected.prompt_version == prompt_version
    assert expected.schema_version == schema_version
    assert expected.matcher_version == "m27-deterministic-v9"
    assert expected.orchestration_policy_version == "m27-local-analytical-preflight-v5"
    assert expected.attempt_policy_version == "m27-durable-attempts-v3"
    assert expected.fingerprint == facts_fingerprint


@pytest.mark.parametrize("policy_state", ["absent", "disabled"])
def test_live_evaluation_rejects_non_enabled_policy_before_exec_or_key_access(
    policy_state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state = _state(policy_state=policy_state)
    state_dir = _state_dir(tmp_path, state)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(runtime, "_query_state", lambda *args, **kwargs: state)

    def must_not_run(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("live evaluation reached provider-capable process setup")

    monkeypatch.setattr(runtime, "_runtime_environment", must_not_run)
    monkeypatch.setattr(os, "execve", must_not_run)

    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="exact enabled"):
        runtime._evaluate_live(state_dir)


@pytest.mark.parametrize("model_snapshot", runtime.REVIEWED_MODEL_SNAPSHOTS)
def test_live_evaluation_execs_only_reviewed_evaluator_with_existing_key(
    model_snapshot: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    expected = _expected_live_configuration(model_snapshot)
    state = replace(
        _state(policy_state="enabled"),
        ai_policy_model=model_snapshot,
        ai_policy_configuration_fingerprint=expected.fingerprint,
    )
    state_dir = _state_dir(tmp_path, state)
    monkeypatch.setenv("OPENAI_API_KEY", "existing-process-key")
    monkeypatch.setattr(runtime, "_query_state", lambda *args, **kwargs: state)
    captured: dict[str, object] = {}

    class _ExecCalled(RuntimeError):
        pass

    def fake_execve(
        executable: object,
        arguments: tuple[str, ...],
        environment: dict[str, str],
    ) -> None:
        captured.update(
            executable=executable,
            arguments=arguments,
            environment=environment,
        )
        raise _ExecCalled

    monkeypatch.setattr(os, "chdir", lambda path: captured.update(chdir=path))
    monkeypatch.setattr(os, "execve", fake_execve)
    with pytest.raises(_ExecCalled):
        runtime._evaluate_live(state_dir, model_snapshot=model_snapshot)

    assert captured["chdir"] == tmp_path
    assert captured["arguments"] == (
        str(tmp_path / ".venv/bin/python"),
        str(tmp_path / "scripts/evaluate_query_studio_live.py"),
        "--execute-live",
    )
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["OPENAI_API_KEY"] == "existing-process-key"
    assert environment["SCHEMABRIDGE_QUERY_STUDIO_AI_MODEL"] == model_snapshot
    assert environment["SCHEMABRIDGE_QUERY_STUDIO_AI_REGION"] == runtime.PINNED_REGION
    assert all(
        b"existing-process-key" not in path.read_bytes()
        for path in state_dir.iterdir()
        if path.is_file()
    )


def test_live_evaluation_forwards_only_a_retained_repository_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    expected = _expected_live_configuration(runtime.REVIEWED_MODEL_SNAPSHOTS[0])
    state = replace(
        _state(policy_state="enabled"),
        ai_policy_model=runtime.REVIEWED_MODEL_SNAPSHOTS[0],
        ai_policy_configuration_fingerprint=expected.fingerprint,
    )
    state_dir = _state_dir(tmp_path, state)
    reports = tmp_path / "reports"
    reports.mkdir()
    retained = reports / "retained-campaign.json"
    retained.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "existing-process-key")
    monkeypatch.setattr(runtime, "_query_state", lambda *args, **kwargs: state)
    captured: dict[str, object] = {}

    class _ExecCalled(RuntimeError):
        pass

    def fake_execve(
        executable: object,
        arguments: tuple[str, ...],
        environment: dict[str, str],
    ) -> None:
        captured.update(arguments=arguments, environment=environment)
        raise _ExecCalled

    monkeypatch.setattr(os, "chdir", lambda path: None)
    monkeypatch.setattr(os, "execve", fake_execve)
    with pytest.raises(_ExecCalled):
        runtime._evaluate_live(
            state_dir,
            model_snapshot=runtime.REVIEWED_MODEL_SNAPSHOTS[0],
            resume_json=Path("reports/retained-campaign.json"),
        )

    assert captured["arguments"] == (
        str(tmp_path / ".venv/bin/python"),
        str(tmp_path / "scripts/evaluate_query_studio_live.py"),
        "--execute-live",
        "--resume-json",
        str(retained.resolve()),
    )


def test_live_evaluation_rejects_resume_escape_before_loading_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    outside = tmp_path / "retained-campaign.json"
    outside.write_text("{}", encoding="utf-8")

    def must_not_load(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("state or key access occurred before resume validation")

    monkeypatch.setattr(runtime, "_load_state", must_not_load)
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="repository report JSON"):
        runtime._evaluate_live(
            tmp_path / ".local/m27-browser",
            resume_json=outside,
        )


def test_policy_cli_is_the_only_policy_path_and_injects_exact_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state = _state()
    state_dir = _state_dir(tmp_path, state)
    captured: dict[str, object] = {}

    class _ExecCalled(RuntimeError):
        pass

    def fake_execve(
        executable: object,
        arguments: tuple[str, ...],
        environment: dict[str, str],
    ) -> None:
        captured.update(
            executable=executable,
            arguments=arguments,
            environment=environment,
        )
        raise _ExecCalled

    monkeypatch.setattr(os, "chdir", lambda path: captured.update(chdir=path))
    monkeypatch.setattr(os, "execve", fake_execve)
    with pytest.raises(_ExecCalled):
        runtime._run_ai_policy_cli(
            state_dir,
            ("prepare", "--expected-version", "0", "--external-ai-disabled"),
        )

    arguments = captured["arguments"]
    environment = captured["environment"]
    assert captured["chdir"] == tmp_path
    assert isinstance(arguments, tuple)
    assert isinstance(environment, dict)
    assert arguments[:7] == (
        str(tmp_path / ".venv/bin/python"),
        "-m",
        "schemabridge.entrypoints.ai_policy.main",
        "prepare",
        "--workspace-id",
        state.workspace_id,
        "--expected-version",
    )
    assert environment["SCHEMABRIDGE_LOCAL_ROLES"] == '["platform_admin"]'
    assert "OPENAI_API_KEY" not in environment

    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="scope"):
        runtime._run_ai_policy_cli(
            state_dir,
            ("inspect", "--workspace-id", "workspace-other"),
        )


def test_policy_guide_uses_exclusive_owner_only_proposal_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state_dir = _state_dir(tmp_path, _state())
    guide = runtime._policy_guide(state_dir)

    assert "--proposal-output <owner-only-json-outside-state-dir>" in guide
    assert "--proposal-file <owner-only-json-outside-state-dir>" in guide
    assert "owner-only 0600" in guide
    assert "--endpoint-region global" in guide
    assert (
        "--endpoint-origin-fingerprint "
        "6836a1bf89b42ed7a325b83c7825e0ae3a17048d4d7395d0606ee18f2800f71c" in guide
    )


@pytest.mark.parametrize("model_snapshot", runtime.REVIEWED_MODEL_SNAPSHOTS)
def test_policy_guide_derives_exact_selected_model_configuration(
    model_snapshot: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state_dir = _state_dir(tmp_path, _state())
    guide = runtime._policy_guide(
        state_dir,
        model_snapshot=model_snapshot,
    )
    expected = _expected_live_configuration(model_snapshot)

    assert f"--model-snapshot {model_snapshot}" in guide
    assert f"--configuration-fingerprint {expected.fingerprint}" in guide
    assert f"evaluate-live --ai-model {model_snapshot}" in guide


def test_state_directory_and_cleanup_are_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    local = tmp_path / ".local"
    local.mkdir()

    assert runtime._resolve_state_dir(local / "m27-browser") == (local / "m27-browser").resolve()
    with pytest.raises(runtime.BrowserAcceptanceSetupError):
        runtime._resolve_state_dir(tmp_path / "outside")
    large_state_dir = local / "m27-browser-acceptance"
    monkeypatch.setattr(runtime, "DEFAULT_STATE_DIR", large_state_dir)
    monkeypatch.setattr(
        runtime,
        "DEFAULT_SMALL_STATE_DIR",
        local / "m27-browser-acceptance-small",
    )
    monkeypatch.setattr(
        runtime,
        "DEFAULT_TWO_CONNECTION_STATE_DIR",
        local / "m27-browser-acceptance-two-connections",
    )
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="dedicated state"):
        runtime._prepare(large_state_dir, cardinality_profile="small")
    assert not large_state_dir.exists()
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="dedicated state"):
        runtime._prepare(large_state_dir, cardinality_profile="two_connections")
    assert not large_state_dir.exists()
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="confirmation"):
        runtime._cleanup(local / "missing", "DROP SOMETHING ELSE")


def test_m27_seed_generator_has_one_safe_non_executable_hostile_fixture() -> None:
    from tests.acceptance.test_semantic_change_acceptance import (
        _M27_HOSTILE_FIELD_SEGMENT,
        _M27_HOSTILE_METADATA_BIDI,
        _M27_HOSTILE_METADATA_HTML,
        _M27_HOSTILE_METADATA_QUERY,
        _m27_scaled_unrelated_assets,
    )

    physical_only = _m27_scaled_unrelated_assets(2, target_field_count=7)
    matches = [
        (asset, field)
        for asset in physical_only
        for field in asset.fields
        if field.description is not None and _M27_HOSTILE_METADATA_QUERY in field.description
    ]

    assert len(matches) == 1
    asset, field = matches[0]
    assert field.description is not None
    assert _M27_HOSTILE_METADATA_HTML in field.description
    assert _M27_HOSTILE_METADATA_BIDI in field.description
    assert field.field_path == (_M27_HOSTILE_FIELD_SEGMENT,)
    assert len(_M27_HOSTILE_FIELD_SEGMENT) == 200
    assert len(_M27_HOSTILE_FIELD_SEGMENT.encode("utf-8")) == 200
    assert all(term in field.description.casefold() for term in _M27_HOSTILE_METADATA_QUERY.split())

    candidate = PhysicalDiscoveryCandidate(
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id="workspace-hostile-fixture",
                connection_id=CatalogConnectionId("connection_hostile_fixture"),
                asset_id=asset.asset_id,
            ),
            field_path=field.field_path,
        ),
        generation=1,
        asset_qualified_name=asset.qualified_name,
        native_type=field.native_type,
        definition=field.description,
        metadata_fingerprint=field.metadata_fingerprint,
    )
    assert candidate.status is PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW
    assert type(candidate).model_validate(candidate.model_dump()) == candidate
    assert "candidate_id" not in candidate.model_dump()
    assert "logical_field" not in candidate.model_dump()


def test_m27_support_decoy_is_physical_only_and_matches_frozen_no_match_searches() -> None:
    from tests.acceptance import test_semantic_change_acceptance as acceptance

    physical_only = acceptance._m27_scaled_unrelated_assets(2, target_field_count=7)
    matches = [
        asset
        for asset in physical_only
        if asset.qualified_name == acceptance._M27_SUPPORT_DECOY_ASSET
    ]
    assert len(matches) == 1
    decoy: CatalogSourceAsset = matches[0]
    assert decoy.asset_id == CatalogAssetId("noise.table_00000")
    assert decoy.schema_name == "support"
    assert decoy.display_name == "order_cases"

    corpus_path = (
        Path(__file__).resolve().parents[2] / "demo/ground_truth/query_studio_matching.yml"
    )
    corpus = yaml.safe_load(corpus_path.read_text(encoding="utf-8"))
    frozen_no_matches = {item["id"]: item["text"] for item in corpus["true_negatives"]}
    expected = {
        "order_id": frozen_no_matches["support-order-id"],
        "customer_id": frozen_no_matches["support-customer-id"],
        "product_id": frozen_no_matches["support-product-id"],
    }
    assert dict(acceptance._M27_SUPPORT_DECOY_FIELD_DESCRIPTIONS) == expected

    decoy_fields = {
        field.field_path[0]: field
        for field in decoy.fields
        if len(field.field_path) == 1 and field.field_path[0] in expected
    }
    assert set(decoy_fields) == set(expected)
    for field_name, search_query in expected.items():
        field = decoy_fields[field_name]
        assert field.description == search_query
        searchable_text = " ".join((decoy.qualified_name, field_name, field.description)).casefold()
        assert all(token in searchable_text for token in search_query.casefold().split())
        candidate = PhysicalDiscoveryCandidate(
            locator=CatalogFieldLocator(
                asset=CatalogAssetLocator(
                    workspace_id="workspace-support-decoy",
                    connection_id=CatalogConnectionId("warehouse-primary"),
                    asset_id=decoy.asset_id,
                ),
                field_path=field.field_path,
            ),
            generation=1,
            asset_qualified_name=decoy.qualified_name,
            native_type=field.native_type,
            definition=field.description,
            metadata_fingerprint=field.metadata_fingerprint,
        )
        assert candidate.status is PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW
        assert "candidate_id" not in candidate.model_dump()
        assert "logical_field" not in candidate.model_dump()

    scope = SemanticRegistryScope(
        workspace_id="workspace-support-decoy",
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )
    registry = RecordedGovernedSemanticRegistry(acceptance.MANIFEST, scope).load().registry
    assert all(
        not item.mapping.physical_field.root.startswith(f"{acceptance._M27_SUPPORT_DECOY_ASSET}.")
        for item in registry.mapping_set.mappings
    )


@pytest.mark.scale
def test_m27_seed_generator_produces_exact_physical_and_governed_split() -> None:
    from tests.acceptance.test_semantic_change_acceptance import (
        _M27_HOSTILE_METADATA_QUERY,
        _M27_SUPPORT_DECOY_ASSET,
        _m27_scaled_unrelated_assets,
    )

    governed_mapping_count = 31
    physical_only = _m27_scaled_unrelated_assets(
        5_434 - 7,
        target_field_count=41_028 - governed_mapping_count,
    )

    assert len(physical_only) + 7 == 5_434
    assert sum(len(asset.fields) for asset in physical_only) == 40_997
    assert sum(len(asset.fields) for asset in physical_only) + governed_mapping_count == 41_028
    assert sum(asset.qualified_name == _M27_SUPPORT_DECOY_ASSET for asset in physical_only) == 1
    assert max(len(asset.fields) for asset in physical_only) == 64
    assert any(
        len(field.field_path) > 1 or "dirección" in field.field_path[0]
        for asset in physical_only
        for field in asset.fields
    )
    assert (
        sum(
            field.description is not None and _M27_HOSTILE_METADATA_QUERY in field.description
            for asset in physical_only
            for field in asset.fields
        )
        == 1
    )


def test_m27_small_seed_generator_produces_exact_10_table_75_field_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.acceptance import test_semantic_change_acceptance as acceptance

    monkeypatch.setenv(acceptance._M27_BROWSER_SEED, "1")
    monkeypatch.setenv(acceptance._M27_BROWSER_CARDINALITY_PROFILE, "small")
    connection_count, asset_count, field_count = acceptance._m27_browser_cardinality()
    governed_asset_count = 7
    governed_mapping_count = 31
    physical_only = acceptance._m27_scaled_unrelated_assets(
        asset_count - governed_asset_count,
        target_field_count=field_count - governed_mapping_count,
    )

    assert (connection_count, asset_count, field_count) == (1, 10, 75)
    assert len(physical_only) + governed_asset_count == asset_count
    assert sum(len(asset.fields) for asset in physical_only) + governed_mapping_count == field_count
    assert (
        sum(asset.qualified_name == acceptance._M27_SUPPORT_DECOY_ASSET for asset in physical_only)
        == 1
    )
    assert (
        sum(
            field.description is not None
            and acceptance._M27_HOSTILE_METADATA_QUERY in field.description
            for asset in physical_only
            for field in asset.fields
        )
        == 1
    )
    assert any(len(field.field_path) > 1 for asset in physical_only for field in asset.fields)
    assert any(
        "dirección" in field.field_path[0] for asset in physical_only for field in asset.fields
    )


def test_m27_two_connection_seed_profile_adds_one_exact_homonym_without_widening_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.acceptance import test_semantic_change_acceptance as acceptance

    monkeypatch.setenv(acceptance._M27_BROWSER_SEED, "1")
    monkeypatch.setenv(
        acceptance._M27_BROWSER_CARDINALITY_PROFILE,
        "two_connections",
    )

    assert acceptance._m27_browser_cardinality() == (2, 11, 76)
    assert acceptance._M27_SECONDARY_CONNECTION_ID == runtime.SECONDARY_CONNECTION_ID
    assert (
        f"{runtime.CROSS_CONNECTION_HOMONYM_ASSET}."
        f"{runtime.CROSS_CONNECTION_HOMONYM_FIELD_PATH[0]}"
        == acceptance._M27_CROSS_CONNECTION_PHYSICAL_FIELD
    )
    primary_asset_count = 10
    primary_field_count = 75
    governed_asset_count = 7
    governed_mapping_count = 31
    physical_only = acceptance._m27_scaled_unrelated_assets(
        primary_asset_count - governed_asset_count,
        target_field_count=primary_field_count - governed_mapping_count,
    )
    assert len(physical_only) + governed_asset_count == 10
    assert sum(len(asset.fields) for asset in physical_only) + governed_mapping_count == 75
