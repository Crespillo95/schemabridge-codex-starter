#!/usr/bin/env python3
"""Prepare and serve the real M27 Query Studio browser-acceptance runtime.

The helper reuses the complete M26 PostgreSQL/DataHub acceptance lifecycle,
retains only one dedicated schema-v8 control database, and adds the exact M27
physical scale generation.  External-AI policy remains an explicit
``schemabridge-ai-policy`` CLI operation.  No provider call is made here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn

import psycopg
from psycopg import sql

from schemabridge.adapters.catalog.postgres_governed_search import (
    PostgresGovernedBindingFactsSearch,
)
from schemabridge.adapters.catalog.postgres_physical_discovery import (
    PostgresPhysicalFieldDiscovery,
)
from schemabridge.adapters.catalog.postgres_refresh import (
    PostgresCatalogRefreshStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.postgres_pool import (
    ControlPoolSettings,
    PostgresControlPool,
)
from schemabridge.adapters.control_plane.postgres_query_studio_ai import (
    PostgresQueryStudioAiControl,
)
from schemabridge.adapters.control_plane.postgres_registry_control import (
    PostgresRegistryControlStore,
)
from schemabridge.adapters.evaluation.query_studio_live_attestation import (
    CANONICAL_CAMPAIGN_PATH,
    CampaignLedgerAttestation,
    build_campaign_ledger_attestation,
    load_campaign_ledger_attestation,
    load_canonical_campaign_evidence,
    read_campaign_ledger_snapshot,
    verify_campaign_ledger_attestation,
    write_campaign_ledger_attestation,
)
from schemabridge.adapters.identity.local import LocalDemoPrincipalFactory
from schemabridge.adapters.language.openai_boundary import (
    OpenAIModelSnapshot,
    OpenAIRegion,
    OpenAIResponsesConfig,
    OpenAIStage,
)
from schemabridge.adapters.language.openai_query_studio import (
    openai_provider_contract_fingerprint,
    openai_public_metadata_policy_fingerprint,
)
from schemabridge.adapters.semantic_registry.datahub import (
    DataHubRegistryReadConfig,
)
from schemabridge.adapters.semantic_registry.datahub_control import (
    DataHubRegistryVersionReader,
)
from schemabridge.application.registry_control import (
    CommitRegistryActivation,
    PrepareRegistryActivationApproval,
    PrepareRegistryRollback,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogRefreshCommand,
    CatalogRefreshMode,
    CatalogSourcePage,
)
from schemabridge.domain.identity import IdentityRole
from schemabridge.domain.query_studio import (
    EXTERNAL_AI_ATTEMPT_POLICY_VERSION,
    QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
    DescriptionQuery,
    GovernedBindingFactsRequest,
    GovernedFieldBinding,
    PhysicalDiscoveryCandidate,
    PhysicalDiscoveryStatus,
    PhysicalFieldDiscoveryPage,
    PhysicalFieldDiscoveryRequest,
    ProviderConfigurationFacts,
    QueryStudioScopeSnapshot,
)
from schemabridge.domain.query_studio_matching import (
    GOVERNED_DESCRIPTION_MATCHER_VERSION,
)
from schemabridge.domain.registry_control import (
    RegistryActivationConfirmation,
)
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    semantic_registry_scope_fingerprint,
)
from schemabridge.entrypoints.streamlit.query_studio_acceptance import (
    M27_BROWSER_SCENARIO_ENV,
    M27BrowserScenario,
)

ROOT: Final = Path(__file__).resolve().parents[1]
MIGRATIONS: Final = ROOT / "migrations/control_plane"
DEFAULT_STATE_DIR: Final = ROOT / ".local/m27-browser-acceptance"
DEFAULT_SMALL_STATE_DIR: Final = ROOT / ".local/m27-browser-acceptance-small"
DEFAULT_TWO_CONNECTION_STATE_DIR: Final = ROOT / ".local/m27-browser-acceptance-two-connections"
DEFAULT_ADMIN_DSN: Final = "postgresql://postgres:postgres@127.0.0.1:55434/postgres"
STATE_FILE: Final = "state.json"
AUDIT_KEY_FILE: Final = "control-audit.key"
IDENTITY_KEY_FILE: Final = "identity-migration.key"
QUERY_STUDIO_KEY_FILE: Final = "query-studio.key"
PSEUDONYM_KEY_FILE: Final = "pseudonymization.key"
DROP_CONFIRMATION: Final = "DROP M27 BROWSER ACCEPTANCE DATABASE"
STALE_CATALOG_CONFIRMATION: Final = "APPLY M27 STALE CATALOG DRIFT"
STALE_REGISTRY_CONFIRMATION: Final = "APPLY M27 STALE REGISTRY DRIFT"
LOCAL_SUBJECT: Final = "m27-browser-reader"
POLICY_SUBJECT: Final = "m27-browser-policy-admin"
STALE_CATALOG_ACTOR: Final = "sb_m27_browser_stale_catalog"
STALE_REGISTRY_ACTOR: Final = "sb_m27_browser_stale_registry"
STALE_CATALOG_INDEXER: Final = "m27-browser-stale-indexer"
CATALOG_SCOPE: Final = "synthetic-demo"
REGISTRY_ID: Final = "synthetic_enterprise"
CONTROL_SCHEMA_VERSION: Final = 8
EXPECTED_ASSET_COUNT: Final = 5_434
EXPECTED_FIELD_COUNT: Final = 41_028
EXPECTED_SMALL_ASSET_COUNT: Final = 10
EXPECTED_SMALL_FIELD_COUNT: Final = 75
EXPECTED_TWO_CONNECTION_COUNT: Final = 2
EXPECTED_TWO_CONNECTION_ASSET_COUNT: Final = 11
EXPECTED_TWO_CONNECTION_FIELD_COUNT: Final = 76
EXPECTED_GOVERNED_MAPPING_COUNT: Final = 31
EXPECTED_CATALOG_GENERATION: Final = 9
EXPECTED_REGISTRY_GENERATION: Final = 2
EXPECTED_REGISTRY_VERSION: Final = 2
EXPECTED_EVIDENCE_REVISION: Final = 4
POSTFLIGHT_BUDGET_MS: Final = 5_000
HOSTILE_METADATA_QUERY: Final = "hostile metadata fixture"
CROSS_CONNECTION_HOMONYM_QUERY: Final = "Stable definition for sales.orders.order_id"
CROSS_CONNECTION_HOMONYM_ASSET: Final = "sales.orders"
CROSS_CONNECTION_HOMONYM_FIELD_PATH: Final = ("order_id",)
PRIMARY_CONNECTION_ID: Final = "warehouse-primary"
SECONDARY_CONNECTION_ID: Final = "warehouse-shadow"
EXPECTED_SECONDARY_CATALOG_GENERATION: Final = 1
REVIEWED_MODEL_SNAPSHOTS: Final = (
    OpenAIModelSnapshot.GPT_5_NANO_2025_08_07.value,
    OpenAIModelSnapshot.GPT_5_4_NANO_2026_03_17.value,
    OpenAIModelSnapshot.GPT_5_6_LUNA.value,
)
PINNED_MODEL: Final = OpenAIModelSnapshot.GPT_5_NANO_2025_08_07.value
PINNED_REGION: Final = "global"
LIVE_EVIDENCE_KEY_VERSION: Final = "v1"
MATCHER_VERSION: Final = GOVERNED_DESCRIPTION_MATCHER_VERSION
_DATABASE_NAME: Final = re.compile(r"^schemabridge_m27_browser_[0-9a-f]{12}$")
_WORKSPACE_LABEL: Final = re.compile(r"^m27-browser-[0-9a-f]{12}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_POLICY_STATES: Final = frozenset({"absent", "disabled", "enabled"})
_CARDINALITY_PROFILES: Final = {
    "large": (1, EXPECTED_ASSET_COUNT, EXPECTED_FIELD_COUNT),
    "small": (1, EXPECTED_SMALL_ASSET_COUNT, EXPECTED_SMALL_FIELD_COUNT),
    "two_connections": (
        EXPECTED_TWO_CONNECTION_COUNT,
        EXPECTED_TWO_CONNECTION_ASSET_COUNT,
        EXPECTED_TWO_CONNECTION_FIELD_COUNT,
    ),
}
_POSTFLIGHT_PAGE_SIZES: Final = (1, 17, 50)
_POSTFLIGHT_BUDGET_NS: Final = POSTFLIGHT_BUDGET_MS * 1_000_000
_HOSTILE_FIELD_PREFIX: Final = "hostile_metadata_fixture_"
_POST_SEED_ANALYZE_TABLES: Final = (
    "catalog_connections",
    "catalog_generations",
    "catalog_assets",
    "catalog_fields",
)
_STATE_SECRET_FILES: Final = frozenset(
    {
        AUDIT_KEY_FILE,
        IDENTITY_KEY_FILE,
        QUERY_STUDIO_KEY_FILE,
        PSEUDONYM_KEY_FILE,
    }
)
_STATE_KEYS: Final = frozenset(
    {
        "schema_version",
        "database",
        "local_workspace",
        "local_subject",
        "workspace_id",
        "created_at",
        "control_schema_version",
        "registry_generation",
        "registry_version",
        "registry_fingerprint",
        "evidence_head_revision",
        "evidence_baseline_revision",
        "catalog_generation",
        "governed_catalog_generation_vector_fingerprint",
        "physical_catalog_generation_vector_fingerprint",
        "connection_count",
        "asset_count",
        "field_count",
        "governed_mapping_count",
        "ai_policy_state",
        "ai_policy_version",
        "ai_policy_model",
        "ai_policy_region",
        "ai_policy_configuration_fingerprint",
    }
)
_ALLOWED_PARENT_ENVIRONMENT: Final = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "TMPDIR",
)


class M27StaleScenario(StrEnum):
    """Closed real-drift scenarios applied after a baseline preview is issued."""

    CATALOG = "stale_catalog"
    REGISTRY = "stale_registry"


class BrowserAcceptanceSetupError(RuntimeError):
    """One bounded M27 setup/runtime failure without secret-bearing detail."""


@dataclass(frozen=True, slots=True)
class BrowserAcceptanceStaleResult:
    """Safe facts proving one old browser token cannot reach query SQL."""

    scenario: M27StaleScenario
    drift_dimension: str
    before_generation: int
    after_generation: int

    def __post_init__(self) -> None:
        expected_dimension = {
            M27StaleScenario.CATALOG: "catalog_generation",
            M27StaleScenario.REGISTRY: "registry_pointer_generation",
        }[self.scenario]
        if (
            self.drift_dimension != expected_dimension
            or isinstance(self.before_generation, bool)
            or isinstance(self.after_generation, bool)
            or self.before_generation < 1
            or self.after_generation != self.before_generation + 1
        ):
            raise ValueError("M27 stale browser result is invalid")

    def to_json(self) -> str:
        return json.dumps(
            {
                "drift": {
                    "after": self.after_generation,
                    "before": self.before_generation,
                    "dimension": self.drift_dimension,
                },
                "external_ai_called": False,
                "query_sql_executed": False,
                "scenario": self.scenario.value,
                "stale_preview_confirmable": False,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class _BrowserAcceptanceStaleWitness:
    """Current database facts needed to prove before/after drift boundaries."""

    active_catalog_generation: int
    physical_catalog_generation_vector_fingerprint: str
    connection_count: int
    asset_count: int
    field_count: int
    registry_generation: int | None
    registry_version: int | None
    registry_fingerprint: str | None
    governed_catalog_generation_vector_fingerprint: str | None
    governed_mapping_count: int | None


@dataclass(frozen=True, slots=True)
class BrowserAcceptancePostflightMetrics:
    """Ephemeral integer timings; these facts are never written to retained state."""

    cold_physical_empty_ms: int
    physical_hostile_exact_ms: int
    governed_traversal_ms: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 0 <= value < POSTFLIGHT_BUDGET_MS
            for value in (
                self.cold_physical_empty_ms,
                self.physical_hostile_exact_ms,
                self.governed_traversal_ms,
            )
        ):
            raise ValueError("M27 postflight timing is invalid")


@dataclass(frozen=True, slots=True)
class BrowserAcceptanceState:
    """Exact non-secret retained acceptance facts."""

    database: str
    local_workspace: str
    local_subject: str
    workspace_id: str
    created_at: str
    control_schema_version: int
    registry_generation: int
    registry_version: int
    registry_fingerprint: str
    evidence_head_revision: int
    evidence_baseline_revision: int
    catalog_generation: int
    governed_catalog_generation_vector_fingerprint: str
    physical_catalog_generation_vector_fingerprint: str
    connection_count: int
    asset_count: int
    field_count: int
    governed_mapping_count: int
    ai_policy_state: str
    ai_policy_version: int | None
    ai_policy_model: str | None
    ai_policy_region: str | None
    ai_policy_configuration_fingerprint: str | None
    postflight_metrics: BrowserAcceptancePostflightMetrics | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not _DATABASE_NAME.fullmatch(self.database):
            raise ValueError("invalid M27 browser acceptance database")
        if not _WORKSPACE_LABEL.fullmatch(self.local_workspace):
            raise ValueError("invalid M27 browser acceptance workspace")
        if self.local_subject != LOCAL_SUBJECT:
            raise ValueError("invalid M27 browser acceptance subject")
        if not self.workspace_id.startswith("sb_workspace_v1_"):
            raise ValueError("invalid M27 pseudonymous workspace")
        try:
            created_at = datetime.fromisoformat(self.created_at)
        except ValueError as error:
            raise ValueError("invalid M27 creation time") from error
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("invalid M27 creation timezone")
        if self.control_schema_version != CONTROL_SCHEMA_VERSION:
            raise ValueError("M27 control schema is not current")
        if (
            self.registry_generation != EXPECTED_REGISTRY_GENERATION
            or self.registry_version != EXPECTED_REGISTRY_VERSION
            or self.evidence_head_revision != EXPECTED_EVIDENCE_REVISION
            or self.evidence_baseline_revision != EXPECTED_EVIDENCE_REVISION
            or self.catalog_generation != EXPECTED_CATALOG_GENERATION
        ):
            raise ValueError("M27 governed registry evidence is incomplete")
        if (
            _SHA256.fullmatch(self.registry_fingerprint) is None
            or _SHA256.fullmatch(self.governed_catalog_generation_vector_fingerprint) is None
            or _SHA256.fullmatch(self.physical_catalog_generation_vector_fingerprint) is None
        ):
            raise ValueError("M27 governed fingerprints are invalid")
        if self.governed_mapping_count != EXPECTED_GOVERNED_MAPPING_COUNT:
            raise ValueError("M27 dynamic-cardinality evidence is not exact")
        _cardinality_profile_for_counts(
            self.connection_count,
            self.asset_count,
            self.field_count,
        )
        if self.ai_policy_state not in _POLICY_STATES:
            raise ValueError("M27 tenant AI policy state is invalid")
        policy_values = (
            self.ai_policy_version,
            self.ai_policy_model,
            self.ai_policy_region,
            self.ai_policy_configuration_fingerprint,
        )
        if self.ai_policy_state == "absent":
            if any(value is not None for value in policy_values):
                raise ValueError("absent M27 tenant AI policy contains facts")
        elif (
            self.ai_policy_version is None
            or self.ai_policy_version < 1
            or self.ai_policy_model is None
            or self.ai_policy_region is None
            or self.ai_policy_configuration_fingerprint is None
            or _SHA256.fullmatch(self.ai_policy_configuration_fingerprint) is None
        ):
            raise ValueError("configured M27 tenant AI policy is incomplete")
        if self.postflight_metrics is not None and not isinstance(
            self.postflight_metrics,
            BrowserAcceptancePostflightMetrics,
        ):
            raise ValueError("M27 postflight metrics are invalid")

    @property
    def cardinality_profile(self) -> str:
        """Return the exact retained acceptance fixture represented by the state."""

        return _cardinality_profile_for_counts(
            self.connection_count,
            self.asset_count,
            self.field_count,
        )

    def to_json(self) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "database": self.database,
                    "local_workspace": self.local_workspace,
                    "local_subject": self.local_subject,
                    "workspace_id": self.workspace_id,
                    "created_at": self.created_at,
                    "control_schema_version": self.control_schema_version,
                    "registry_generation": self.registry_generation,
                    "registry_version": self.registry_version,
                    "registry_fingerprint": self.registry_fingerprint,
                    "evidence_head_revision": self.evidence_head_revision,
                    "evidence_baseline_revision": self.evidence_baseline_revision,
                    "catalog_generation": self.catalog_generation,
                    "governed_catalog_generation_vector_fingerprint": (
                        self.governed_catalog_generation_vector_fingerprint
                    ),
                    "physical_catalog_generation_vector_fingerprint": (
                        self.physical_catalog_generation_vector_fingerprint
                    ),
                    "connection_count": self.connection_count,
                    "asset_count": self.asset_count,
                    "field_count": self.field_count,
                    "governed_mapping_count": self.governed_mapping_count,
                    "ai_policy_state": self.ai_policy_state,
                    "ai_policy_version": self.ai_policy_version,
                    "ai_policy_model": self.ai_policy_model,
                    "ai_policy_region": self.ai_policy_region,
                    "ai_policy_configuration_fingerprint": (
                        self.ai_policy_configuration_fingerprint
                    ),
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )

    @classmethod
    def from_json(cls, raw: bytes) -> BrowserAcceptanceState:
        try:
            payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
            if (
                not isinstance(payload, dict)
                or set(payload) != _STATE_KEYS
                or payload.get("schema_version") != 1
            ):
                raise ValueError
            return cls(
                database=str(payload["database"]),
                local_workspace=str(payload["local_workspace"]),
                local_subject=str(payload["local_subject"]),
                workspace_id=str(payload["workspace_id"]),
                created_at=str(payload["created_at"]),
                control_schema_version=_exact_int(payload["control_schema_version"]),
                registry_generation=_exact_int(payload["registry_generation"]),
                registry_version=_exact_int(payload["registry_version"]),
                registry_fingerprint=str(payload["registry_fingerprint"]),
                evidence_head_revision=_exact_int(payload["evidence_head_revision"]),
                evidence_baseline_revision=_exact_int(payload["evidence_baseline_revision"]),
                catalog_generation=_exact_int(payload["catalog_generation"]),
                governed_catalog_generation_vector_fingerprint=str(
                    payload["governed_catalog_generation_vector_fingerprint"]
                ),
                physical_catalog_generation_vector_fingerprint=str(
                    payload["physical_catalog_generation_vector_fingerprint"]
                ),
                connection_count=_exact_int(payload["connection_count"]),
                asset_count=_exact_int(payload["asset_count"]),
                field_count=_exact_int(payload["field_count"]),
                governed_mapping_count=_exact_int(payload["governed_mapping_count"]),
                ai_policy_state=str(payload["ai_policy_state"]),
                ai_policy_version=_optional_exact_int(payload["ai_policy_version"]),
                ai_policy_model=_optional_string(payload["ai_policy_model"]),
                ai_policy_region=_optional_string(payload["ai_policy_region"]),
                ai_policy_configuration_fingerprint=_optional_string(
                    payload["ai_policy_configuration_fingerprint"]
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise BrowserAcceptanceSetupError("M27 browser acceptance state is invalid.") from error


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate M27 state key")
        value[key] = item
    return value


def _exact_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("M27 state integer is invalid")
    return value


def _optional_exact_int(value: object) -> int | None:
    return None if value is None else _exact_int(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("M27 state string is invalid")
    return value


def _cardinality_for_profile(profile: str) -> tuple[int, int]:
    try:
        _connection_count, asset_count, field_count = _CARDINALITY_PROFILES[profile]
        return asset_count, field_count
    except KeyError as error:
        raise BrowserAcceptanceSetupError("M27 browser cardinality profile is invalid.") from error


def _connection_count_for_profile(profile: str) -> int:
    try:
        connection_count, _asset_count, _field_count = _CARDINALITY_PROFILES[profile]
        return connection_count
    except KeyError as error:
        raise BrowserAcceptanceSetupError("M27 browser cardinality profile is invalid.") from error


def _cardinality_profile_for_counts(
    connection_count: int,
    asset_count: int,
    field_count: int,
) -> str:
    for profile, cardinality in _CARDINALITY_PROFILES.items():
        if cardinality == (connection_count, asset_count, field_count):
            return profile
    raise ValueError("M27 dynamic-cardinality evidence is not exact")


def _write_owner_only(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise BrowserAcceptanceSetupError(
            "An owner-only M27 acceptance file could not be created."
        ) from error


def _read_owner_only(path: Path, *, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or not 0 < metadata.st_size <= maximum
        ):
            raise OSError
        return path.read_bytes()
    except OSError as error:
        raise BrowserAcceptanceSetupError(
            "An owner-only M27 acceptance file is unavailable."
        ) from error


def _read_secret_text(path: Path) -> str:
    try:
        value = _read_owner_only(path, maximum=16 * 1024).decode("ascii").strip()
    except UnicodeError as error:
        raise BrowserAcceptanceSetupError(
            "An owner-only M27 acceptance secret is invalid."
        ) from error
    if len(value) < 32 or len(set(value)) < 8:
        raise BrowserAcceptanceSetupError("An owner-only M27 acceptance secret is invalid.")
    return value


def _resolve_state_dir(raw: Path) -> Path:
    allowed = (ROOT / ".local").resolve()
    resolved = raw.expanduser().resolve()
    if resolved == allowed or allowed not in resolved.parents:
        raise BrowserAcceptanceSetupError(
            "M27 browser state must be a dedicated directory under .local."
        )
    return resolved


def _principal(
    local_workspace: str,
    *,
    subject: str,
    roles: frozenset[IdentityRole],
) -> str:
    return (
        LocalDemoPrincipalFactory(
            workspace=local_workspace,
            subject=subject,
            roles=roles,
        )
        .create(now=datetime.now(UTC))
        .workspace_id
    )


def _reviewed_model_snapshot(model_snapshot: str) -> str:
    if model_snapshot not in REVIEWED_MODEL_SNAPSHOTS:
        raise BrowserAcceptanceSetupError(
            "M27 Query Studio model is outside the reviewed qualification order."
        )
    return model_snapshot


def _expected_live_configuration(
    model_snapshot: str = PINNED_MODEL,
    *,
    semantic_scope_fingerprint: str,
    registry_fingerprint: str,
) -> ProviderConfigurationFacts:
    """Derive the exact key-free managed OpenAI configuration used by bootstrap."""

    reviewed_model = _reviewed_model_snapshot(model_snapshot)
    config = OpenAIResponsesConfig.for_model(
        reviewed_model,
        region=OpenAIRegion(PINNED_REGION),
    )
    exact_behavior_fingerprint = openai_provider_contract_fingerprint(config)
    return ProviderConfigurationFacts.create(
        adapter="openai_responses_structured",
        model_snapshot=config.model.value,
        reasoning_effort=config.reasoning_effort.value,
        endpoint_region=config.region.value,
        prompt_version=config.prompt_version,
        schema_version=config.schema_version,
        matcher_version=MATCHER_VERSION,
        orchestration_policy_version=QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
        attempt_policy_version=EXTERNAL_AI_ATTEMPT_POLICY_VERSION,
        managed_config_fingerprint=config.fingerprint(stage=OpenAIStage.INTERPRETATION),
        provider_contract_fingerprint=exact_behavior_fingerprint,
        public_metadata_policy_fingerprint=openai_public_metadata_policy_fingerprint(
            config,
            semantic_scope_fingerprint=semantic_scope_fingerprint,
            registry_fingerprint=registry_fingerprint,
        ),
        public_metadata_semantic_scope_fingerprint=semantic_scope_fingerprint,
        public_metadata_registry_fingerprint=registry_fingerprint,
        external_ai=True,
    )


def _expected_live_endpoint_origin_fingerprint(
    model_snapshot: str = PINNED_MODEL,
) -> str:
    """Derive the exact non-secret origin fingerprint expected by tenant policy."""

    return OpenAIResponsesConfig.for_model(
        _reviewed_model_snapshot(model_snapshot),
        region=OpenAIRegion(PINNED_REGION),
    ).endpoint_origin_fingerprint


def _expected_live_configuration_for_state(
    state: BrowserAcceptanceState,
    model_snapshot: str = PINNED_MODEL,
) -> ProviderConfigurationFacts:
    scope = SemanticRegistryScope(
        workspace_id=state.workspace_id,
        catalog_scope=CATALOG_SCOPE,
        registry_id=REGISTRY_ID,
    )
    return _expected_live_configuration(
        model_snapshot,
        semantic_scope_fingerprint=semantic_registry_scope_fingerprint(scope),
        registry_fingerprint=state.registry_fingerprint,
    )


def _runtime_dsn(database: str) -> str:
    return _role_dsn("schemabridge_runtime", database)


def _migrator_dsn(database: str) -> str:
    return _role_dsn("schemabridge_migrator", database)


def _role_dsn(role: str, database: str) -> str:
    if not _DATABASE_NAME.fullmatch(database):
        raise BrowserAcceptanceSetupError("M27 browser acceptance database identity is invalid.")
    if role not in {
        "schemabridge_api",
        "schemabridge_catalog",
        "schemabridge_migrator",
        "schemabridge_runtime",
    }:
        raise BrowserAcceptanceSetupError("M27 browser acceptance role is invalid.")
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _validate_catalog_witnesses(
    *,
    expected_scope: SemanticRegistryScope,
    physical_scope: SemanticRegistryScope,
    governed_scope: SemanticRegistryScope,
    expected_physical_generations: frozenset[int],
    physical_generations: frozenset[int],
    governed_generations: frozenset[int],
    physical_vector: str,
    governed_vector: str,
) -> None:
    """Require both vector domains to witness the exact profile generations/scope."""

    if (
        physical_scope != expected_scope
        or governed_scope != expected_scope
        or physical_generations != expected_physical_generations
        or governed_generations != frozenset({EXPECTED_CATALOG_GENERATION})
        or _SHA256.fullmatch(physical_vector) is None
        or _SHA256.fullmatch(governed_vector) is None
    ):
        raise BrowserAcceptanceSetupError(
            "The retained M27 physical and governed catalog witnesses differ."
        )


def _bounded_postflight_milliseconds(
    elapsed_ns: int,
    *,
    operation: str,
) -> int:
    if type(elapsed_ns) is not int or elapsed_ns < 0 or elapsed_ns >= _POSTFLIGHT_BUDGET_NS:
        raise BrowserAcceptanceSetupError(
            f"The retained M27 {operation} exceeded its local 5000 ms budget."
        )
    return elapsed_ns // 1_000_000


def _validate_non_executable_physical_candidate(
    candidate: PhysicalDiscoveryCandidate,
    *,
    scope: SemanticRegistryScope,
) -> None:
    if (
        candidate.status is not PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW
        or candidate.generation != EXPECTED_CATALOG_GENERATION
        or candidate.locator.asset.workspace_id != scope.workspace_id
    ):
        raise BrowserAcceptanceSetupError(
            "The retained M27 physical result crossed the non-executable boundary."
        )
    payload = candidate.model_dump(mode="json")
    if "candidate_id" in payload or "logical_field" in payload:
        raise BrowserAcceptanceSetupError(
            "The retained M27 physical result crossed the non-executable boundary."
        )


def _validate_cross_connection_homonym_witness(
    page: PhysicalFieldDiscoveryPage,
    *,
    governed_items: tuple[GovernedFieldBinding, ...],
    scope: SemanticRegistryScope,
) -> frozenset[int]:
    """Prove one equal physical name is distinct and non-executable per connection."""

    matches = tuple(
        item
        for item in page.items
        if item.asset_qualified_name == CROSS_CONNECTION_HOMONYM_ASSET
        and item.locator.field_path == CROSS_CONNECTION_HOMONYM_FIELD_PATH
    )
    expected_connections = frozenset(
        {
            PRIMARY_CONNECTION_ID,
            SECONDARY_CONNECTION_ID,
        }
    )
    if (
        len(matches) != 2
        or frozenset(item.locator.asset.connection_id.root for item in matches)
        != expected_connections
        or frozenset(item.generation for item in matches)
        != frozenset(
            {
                EXPECTED_CATALOG_GENERATION,
                EXPECTED_SECONDARY_CATALOG_GENERATION,
            }
        )
        or any(item.locator.asset.workspace_id != scope.workspace_id for item in matches)
        or any(item.definition != CROSS_CONNECTION_HOMONYM_QUERY for item in matches)
        or any(item.status is not PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW for item in matches)
        or any(
            {"candidate_id", "logical_field"}.intersection(
                item.model_dump(mode="json"),
            )
            for item in matches
        )
    ):
        raise BrowserAcceptanceSetupError(
            "The retained M27 two-connection homonym witness is incomplete."
        )
    governed_matches = tuple(
        item
        for item in governed_items
        if item.physical_field.root
        == f"{CROSS_CONNECTION_HOMONYM_ASSET}.{CROSS_CONNECTION_HOMONYM_FIELD_PATH[0]}"
    )
    if (
        len(governed_matches) != 1
        or governed_matches[0].locator.asset.connection_id.root != PRIMARY_CONNECTION_ID
        or governed_matches[0].catalog_generation != EXPECTED_CATALOG_GENERATION
    ):
        raise BrowserAcceptanceSetupError(
            "The retained M27 cross-connection homonym entered the governed lane."
        )
    return frozenset(item.generation for item in matches)


def _validate_physical_postflight_page(
    page: PhysicalFieldDiscoveryPage,
    *,
    scope: SemanticRegistryScope,
    exact_hostile: bool,
) -> None:
    # The concrete adapter reads at most ``page_size + 1`` rows and exposes the
    # sentinel only as a continuation. Requiring one item at page size one keeps
    # this witness at one returned row plus at most one sentinel.
    if (
        page.page_size != 1
        or len(page.items) != 1
        or (page.next_cursor is None) is not exact_hostile
    ):
        raise BrowserAcceptanceSetupError(
            "The retained M27 physical postflight page is incomplete."
        )
    candidate = page.items[0]
    _validate_non_executable_physical_candidate(candidate, scope=scope)
    if exact_hostile and (
        candidate.definition is None
        or HOSTILE_METADATA_QUERY not in candidate.definition.casefold()
        or not candidate.locator.field_path[-1].startswith(_HOSTILE_FIELD_PREFIX)
    ):
        raise BrowserAcceptanceSetupError(
            "The retained M27 hostile metadata witness is unavailable."
        )


def _validate_postflight_governed_scope(
    observed: QueryStudioScopeSnapshot,
    *,
    state: BrowserAcceptanceState,
    scope: SemanticRegistryScope,
) -> None:
    if (
        observed.scope != scope
        or observed.pointer_generation != state.registry_generation
        or observed.registry_version != state.registry_version
        or observed.registry_fingerprint != state.registry_fingerprint
        or observed.evidence_head_revision != state.evidence_head_revision
        or observed.evidence_baseline_revision != state.evidence_baseline_revision
        or observed.catalog_generation_vector_fingerprint
        != state.governed_catalog_generation_vector_fingerprint
    ):
        raise BrowserAcceptanceSetupError("The retained M27 governed postflight scope changed.")


def _run_physical_postflight(
    *,
    state: BrowserAcceptanceState,
    scope: SemanticRegistryScope,
    runtime_dsn: str,
) -> tuple[int, int]:
    reader = PostgresPhysicalFieldDiscovery.from_signing_key(
        dsn=runtime_dsn,
        cursor_signing_key=hashlib.sha256(
            f"m27-browser-postflight\0{state.database}".encode()
        ).digest(),
    )
    empty_started = time.monotonic_ns()
    empty_page = reader.search(PhysicalFieldDiscoveryRequest(scope=scope, page_size=1))
    empty_ms = _bounded_postflight_milliseconds(
        time.monotonic_ns() - empty_started,
        operation="cold physical empty search",
    )
    _validate_physical_postflight_page(
        empty_page,
        scope=scope,
        exact_hostile=False,
    )

    hostile_started = time.monotonic_ns()
    hostile_page = reader.search(
        PhysicalFieldDiscoveryRequest(
            scope=scope,
            query=DescriptionQuery(HOSTILE_METADATA_QUERY),
            page_size=1,
        )
    )
    hostile_ms = _bounded_postflight_milliseconds(
        time.monotonic_ns() - hostile_started,
        operation="exact hostile metadata search",
    )
    _validate_physical_postflight_page(
        hostile_page,
        scope=scope,
        exact_hostile=True,
    )
    return empty_ms, hostile_ms


def _run_governed_postflight(
    *,
    state: BrowserAcceptanceState,
    scope: SemanticRegistryScope,
    runtime_dsn: str,
) -> int:
    pool = PostgresControlPool(
        ControlPoolSettings(
            dsn=runtime_dsn,
            application_name="schemabridge-control-runtime",
            min_size=1,
            max_size=2,
            max_waiting=4,
            acquisition_timeout_seconds=5,
            startup_timeout_seconds=15,
            statement_timeout_ms=POSTFLIGHT_BUDGET_MS,
        )
    )
    orders: dict[int, tuple[str, ...]] = {}
    with pool:
        reader = PostgresGovernedBindingFactsSearch(
            runtime_dsn,
            connection_provider=pool,
        )
        started = time.monotonic_ns()
        for page_size in _POSTFLIGHT_PAGE_SIZES:
            request = GovernedBindingFactsRequest(
                scope=scope,
                page_size=page_size,
            )
            binding_ids: list[str] = []
            page_count = 0
            while True:
                page_count += 1
                if page_count > state.governed_mapping_count + 1:
                    raise BrowserAcceptanceSetupError(
                        "The retained M27 governed traversal did not terminate."
                    )
                page = reader.search(request)
                _validate_postflight_governed_scope(
                    page.scope,
                    state=state,
                    scope=scope,
                )
                page_binding_ids = tuple(item.binding_id for item in page.items)
                if (
                    page.page_size != page_size
                    or type(page.rows_read) is not int
                    or page.rows_read < len(page.items)
                    or page.rows_read > page_size + 1
                    or (page.next_key is not None and page.rows_read != page_size + 1)
                    or (page.next_key is None and page.rows_read != len(page.items))
                    or len(page.items) > page_size
                    or len(page_binding_ids) != len(set(page_binding_ids))
                    or any(
                        item.catalog_generation != EXPECTED_CATALOG_GENERATION
                        for item in page.items
                    )
                    or any(binding_id in binding_ids for binding_id in page_binding_ids)
                ):
                    raise BrowserAcceptanceSetupError(
                        "The retained M27 governed traversal is invalid."
                    )
                binding_ids.extend(page_binding_ids)
                if len(binding_ids) > state.governed_mapping_count:
                    raise BrowserAcceptanceSetupError(
                        "The retained M27 governed traversal exceeded its exact population."
                    )
                if page.next_key is None:
                    break
                if not page.items or page.next_key == request.after:
                    raise BrowserAcceptanceSetupError(
                        "The retained M27 governed traversal cursor did not progress."
                    )
                request = GovernedBindingFactsRequest(
                    scope=scope,
                    page_size=page_size,
                    after=page.next_key,
                    expected_scope=page.scope,
                )
            if len(binding_ids) != state.governed_mapping_count or len(binding_ids) != len(
                set(binding_ids)
            ):
                raise BrowserAcceptanceSetupError(
                    "The retained M27 governed traversal population is incomplete."
                )
            orders[page_size] = tuple(binding_ids)
        elapsed_ms = _bounded_postflight_milliseconds(
            time.monotonic_ns() - started,
            operation="governed 1/17/50 traversal",
        )
    if not (
        orders[1] == orders[17] == orders[50] and len(orders[1]) == state.governed_mapping_count
    ):
        raise BrowserAcceptanceSetupError("The retained M27 governed traversal order changed.")
    return elapsed_ms


def _run_catalog_postflight(
    state: BrowserAcceptanceState,
) -> BrowserAcceptancePostflightMetrics:
    scope = SemanticRegistryScope(
        workspace_id=state.workspace_id,
        catalog_scope=CATALOG_SCOPE,
        registry_id=REGISTRY_ID,
    )
    runtime_dsn = _runtime_dsn(state.database)
    try:
        empty_ms, hostile_ms = _run_physical_postflight(
            state=state,
            scope=scope,
            runtime_dsn=runtime_dsn,
        )
    except BrowserAcceptanceSetupError:
        raise
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The retained M27 physical postflight is unavailable."
        ) from error
    try:
        governed_ms = _run_governed_postflight(
            state=state,
            scope=scope,
            runtime_dsn=runtime_dsn,
        )
    except BrowserAcceptanceSetupError:
        raise
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The retained M27 governed postflight is unavailable."
        ) from error
    return BrowserAcceptancePostflightMetrics(
        cold_physical_empty_ms=empty_ms,
        physical_hostile_exact_ms=hostile_ms,
        governed_traversal_ms=governed_ms,
    )


def _query_state(
    database: str,
    local_workspace: str,
    local_subject: str,
    *,
    created_at: str,
) -> BrowserAcceptanceState:
    workspace_id = _principal(
        local_workspace,
        subject=local_subject,
        roles=frozenset({IdentityRole.ANALYST}),
    )
    scope = SemanticRegistryScope(
        workspace_id=workspace_id,
        catalog_scope=CATALOG_SCOPE,
        registry_id=REGISTRY_ID,
    )
    try:
        inspection = PostgresControlPlaneMigrator(
            _migrator_dsn(database),
            MIGRATIONS,
        ).require_current()
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The retained M27 migration evidence is unavailable."
        ) from error
    try:
        with psycopg.connect(_runtime_dsn(database)) as connection:
            governance = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.load_query_studio_governance_scope(
                    %s::varchar, %s::varchar, %s::varchar
                )
                """,
                (scope.workspace_id, scope.catalog_scope, scope.registry_id),
            ).fetchone()
            physical = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.load_physical_discovery_scope(
                    %s::varchar, %s::varchar, %s::varchar
                )
                """,
                (scope.workspace_id, scope.catalog_scope, scope.registry_id),
            ).fetchone()
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The retained M27 scope evidence is unavailable."
        ) from error
    runtime_dsn = _runtime_dsn(database)
    try:
        policy = PostgresQueryStudioAiControl(runtime_dsn).load_policy(workspace_id)
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The retained M27 tenant AI policy evidence is unavailable."
        ) from error
    try:
        physical_reader = PostgresPhysicalFieldDiscovery.from_signing_key(
            dsn=runtime_dsn,
            cursor_signing_key=hashlib.sha256(f"m27-browser-witness\0{database}".encode()).digest(),
        )
        physical_cardinality = physical_reader.inspect_cardinality(scope)
        physical_page = physical_reader.search(
            PhysicalFieldDiscoveryRequest(scope=scope, page_size=1)
        )
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The retained M27 physical witness is unavailable."
        ) from error
    try:
        governed_page = PostgresGovernedBindingFactsSearch(runtime_dsn).search(
            GovernedBindingFactsRequest(
                scope=scope,
                page_size=EXPECTED_GOVERNED_MAPPING_COUNT,
            )
        )
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The retained M27 governed witness is unavailable."
        ) from error
    if governance is None or len(governance) != 10 or physical is None or len(physical) != 4:
        raise BrowserAcceptanceSetupError("The retained M27 PostgreSQL evidence is incomplete.")
    governance_vector = str(governance[8]).strip()
    physical_vector = str(physical[0]).strip()
    if (
        physical_cardinality.catalog_generation_vector_fingerprint != physical_vector
        or physical_cardinality.connection_count != int(physical[1])
        or physical_cardinality.asset_count != int(physical[2])
        or physical_cardinality.field_count != int(physical[3])
        or len(physical_page.items) != 1
        or len(governed_page.items) != EXPECTED_GOVERNED_MAPPING_COUNT
        or governed_page.next_key is not None
        or governed_page.scope.catalog_generation_vector_fingerprint != governance_vector
    ):
        raise BrowserAcceptanceSetupError(
            "The retained M27 catalog witness population is incomplete."
        )
    try:
        cardinality_profile = _cardinality_profile_for_counts(
            int(physical[1]),
            int(physical[2]),
            int(physical[3]),
        )
    except (TypeError, ValueError, IndexError) as error:
        raise BrowserAcceptanceSetupError(
            "The retained M27 dynamic-cardinality evidence is incomplete."
        ) from error
    physical_generations = frozenset(item.generation for item in physical_page.items)
    if cardinality_profile == "two_connections":
        try:
            homonym_page = physical_reader.search(
                PhysicalFieldDiscoveryRequest(
                    scope=scope,
                    query=DescriptionQuery(CROSS_CONNECTION_HOMONYM_QUERY),
                    page_size=50,
                )
            )
            physical_generations = _validate_cross_connection_homonym_witness(
                homonym_page,
                governed_items=governed_page.items,
                scope=scope,
            )
        except BrowserAcceptanceSetupError:
            raise
        except Exception as error:
            raise BrowserAcceptanceSetupError(
                "The retained M27 two-connection homonym witness is unavailable."
            ) from error
    _validate_catalog_witnesses(
        expected_scope=scope,
        physical_scope=physical_cardinality.scope,
        governed_scope=governed_page.scope.scope,
        expected_physical_generations=(
            frozenset(
                {
                    EXPECTED_CATALOG_GENERATION,
                    EXPECTED_SECONDARY_CATALOG_GENERATION,
                }
            )
            if cardinality_profile == "two_connections"
            else frozenset({EXPECTED_CATALOG_GENERATION})
        ),
        physical_generations=physical_generations,
        governed_generations=frozenset(item.catalog_generation for item in governed_page.items),
        physical_vector=physical_vector,
        governed_vector=governance_vector,
    )
    policy_state = (
        "absent" if policy is None else ("enabled" if policy.external_ai_enabled else "disabled")
    )
    try:
        return BrowserAcceptanceState(
            database=database,
            local_workspace=local_workspace,
            local_subject=local_subject,
            workspace_id=workspace_id,
            created_at=created_at,
            control_schema_version=inspection.current_version or 0,
            registry_generation=int(governance[0]),
            registry_version=int(governance[1]),
            registry_fingerprint=str(governance[2]).strip(),
            evidence_head_revision=int(governance[5]),
            evidence_baseline_revision=int(governance[6]),
            catalog_generation=EXPECTED_CATALOG_GENERATION,
            governed_catalog_generation_vector_fingerprint=governance_vector,
            physical_catalog_generation_vector_fingerprint=physical_vector,
            connection_count=int(physical[1]),
            asset_count=int(physical[2]),
            field_count=int(physical[3]),
            governed_mapping_count=int(governance[9]),
            ai_policy_state=policy_state,
            ai_policy_version=None if policy is None else policy.version,
            ai_policy_model=None if policy is None else policy.model_snapshot,
            ai_policy_region=None if policy is None else policy.endpoint_region,
            ai_policy_configuration_fingerprint=(
                None if policy is None else policy.configuration_fingerprint
            ),
        )
    except (TypeError, ValueError, IndexError) as error:
        raise BrowserAcceptanceSetupError(
            "The retained M27 PostgreSQL evidence is incomplete."
        ) from error


def _minimal_parent_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in _ALLOWED_PARENT_ENVIRONMENT if key in os.environ}
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _acceptance_environment(
    database: str,
    local_workspace: str,
    local_subject: str,
    audit_key: str,
    *,
    cardinality_profile: str = "large",
) -> dict[str, str]:
    _cardinality_for_profile(cardinality_profile)
    environment = _minimal_parent_environment()
    environment.update(
        {
            "SCHEMABRIDGE_TEST_M26_ACCEPTANCE_DATABASE": database,
            "SCHEMABRIDGE_TEST_M26_RETAIN_DATABASE": "1",
            "SCHEMABRIDGE_TEST_M26_LOCAL_WORKSPACE": local_workspace,
            "SCHEMABRIDGE_TEST_BROWSER_LOCAL_SUBJECT": local_subject,
            "SCHEMABRIDGE_TEST_BROWSER_CONTROL_AUDIT_SIGNING_KEY": audit_key,
            "SCHEMABRIDGE_TEST_M27_BROWSER_SEED": "1",
            "SCHEMABRIDGE_TEST_M27_CARDINALITY_PROFILE": cardinality_profile,
        }
    )
    return environment


def _drop_database(database: str) -> None:
    if not _DATABASE_NAME.fullmatch(database):
        raise BrowserAcceptanceSetupError("M27 browser acceptance database identity is invalid.")
    admin_dsn = os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        DEFAULT_ADMIN_DSN,
    )
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
    except psycopg.Error as error:
        raise BrowserAcceptanceSetupError(
            "The dedicated M27 browser database could not be removed."
        ) from error


def _analyze_catalog_statistics(database: str) -> None:
    """Stabilize the retained post-seed witness without changing catalog rows."""

    statement = sql.SQL("ANALYZE {}").format(
        sql.SQL(", ").join(
            sql.Identifier("schemabridge_control", table) for table in _POST_SEED_ANALYZE_TABLES
        )
    )
    try:
        with psycopg.connect(_migrator_dsn(database), autocommit=True) as connection:
            connection.execute(statement)
    except psycopg.Error as error:
        raise BrowserAcceptanceSetupError(
            "The retained M27 catalog statistics could not be prepared."
        ) from error


def _remove_exact_state_files(state_dir: Path, expected: set[str]) -> None:
    actual = {item.name for item in state_dir.iterdir()}
    if actual != expected or any(not item.is_file() for item in state_dir.iterdir()):
        raise BrowserAcceptanceSetupError(
            "The M27 state directory contains unexpected material and was retained."
        )
    for name in sorted(expected):
        (state_dir / name).unlink()
    state_dir.rmdir()


def _prepare(
    state_dir: Path,
    *,
    cardinality_profile: str = "large",
) -> BrowserAcceptanceState:
    _cardinality_for_profile(cardinality_profile)
    state_dir = _resolve_state_dir(state_dir)
    if cardinality_profile != "large" and state_dir == DEFAULT_STATE_DIR.resolve():
        example = (
            DEFAULT_SMALL_STATE_DIR
            if cardinality_profile == "small"
            else DEFAULT_TWO_CONNECTION_STATE_DIR
        )
        raise BrowserAcceptanceSetupError(
            "The alternate M27 fixture requires a dedicated state directory, for example "
            f"{example}."
        )
    try:
        state_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError as error:
        raise BrowserAcceptanceSetupError(
            "The dedicated M27 browser state directory must not already exist."
        ) from error
    identifier = secrets.token_hex(6)
    database = f"schemabridge_m27_browser_{identifier}"
    local_workspace = f"m27-browser-{identifier}"
    created_at = datetime.now(UTC).isoformat()
    for filename in sorted(_STATE_SECRET_FILES):
        _write_owner_only(
            state_dir / filename,
            (secrets.token_urlsafe(48) + "\n").encode("ascii"),
        )
    audit_key = _read_secret_text(state_dir / AUDIT_KEY_FILE)
    command = (
        str(ROOT / ".venv/bin/python"),
        "-m",
        "pytest",
        "-m",
        "acceptance",
        "tests/acceptance/test_semantic_change_acceptance.py",
        "-q",
    )
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=_acceptance_environment(
            database,
            local_workspace,
            LOCAL_SUBJECT,
            audit_key,
            cardinality_profile=cardinality_profile,
        ),
        check=False,
    )
    if completed.returncode != 0:
        _drop_database(database)
        _remove_exact_state_files(state_dir, set(_STATE_SECRET_FILES))
        raise BrowserAcceptanceSetupError("The real M27 PostgreSQL/DataHub seed did not pass.")
    try:
        _analyze_catalog_statistics(database)
        state = _query_state(
            database,
            local_workspace,
            LOCAL_SUBJECT,
            created_at=created_at,
        )
        if state.cardinality_profile != cardinality_profile:
            raise BrowserAcceptanceSetupError(
                "The retained M27 cardinality differs from the requested fixture."
            )
        _write_owner_only(state_dir / STATE_FILE, state.to_json())
    except Exception:
        _drop_database(database)
        _remove_exact_state_files(state_dir, set(_STATE_SECRET_FILES))
        raise
    return state


def _load_state(state_dir: Path) -> tuple[Path, BrowserAcceptanceState]:
    resolved = _resolve_state_dir(state_dir)
    raw = _read_owner_only(resolved / STATE_FILE, maximum=64 * 1024)
    return resolved, BrowserAcceptanceState.from_json(raw)


def _runtime_environment(
    state_dir: Path,
    state: BrowserAcceptanceState,
    *,
    ai_mode: str,
    model_snapshot: str = PINNED_MODEL,
    browser_scenario: M27BrowserScenario | None = None,
) -> dict[str, str]:
    if ai_mode not in {"fake", "live"}:
        raise BrowserAcceptanceSetupError("M27 Query Studio AI mode is invalid.")
    reviewed_model = _reviewed_model_snapshot(model_snapshot)
    environment = _minimal_parent_environment()
    environment.update(
        {
            "SCHEMABRIDGE_COMPONENT": "web",
            "SCHEMABRIDGE_ENVIRONMENT": "development",
            "SCHEMABRIDGE_AUTH_MODE": "local-demo",
            "SCHEMABRIDGE_LOCAL_WORKSPACE": state.local_workspace,
            "SCHEMABRIDGE_LOCAL_SUBJECT": state.local_subject,
            "SCHEMABRIDGE_LOCAL_ROLES": '["analyst"]',
            "SCHEMABRIDGE_CATALOG_MODE": "recorded",
            "SCHEMABRIDGE_REGISTRY_MODE": "live",
            "SCHEMABRIDGE_PUBLICATION_MODE": "fake",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
            "SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS": "true",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION": "active",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_ID": REGISTRY_ID,
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_CATALOG_SCOPE": CATALOG_SCOPE,
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH": str(
                (ROOT / ".local/datahub/mcp.env").resolve()
            ),
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA": "schemabridge_control",
            "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION": str(CONTROL_SCHEMA_VERSION),
            "SCHEMABRIDGE_CONTROL_DATABASE_URL": _runtime_dsn(state.database),
            "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": _read_secret_text(state_dir / AUDIT_KEY_FILE),
            "SCHEMABRIDGE_CONTROL_AUDIT_KEY_VERSION": "v1",
            "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": _read_secret_text(state_dir / IDENTITY_KEY_FILE),
            "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY_VERSION": "v1",
            "SCHEMABRIDGE_IDENTITY_POLICY_VERSION": "v1",
            "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": _read_secret_text(
                state_dir / QUERY_STUDIO_KEY_FILE
            ),
            "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": _read_secret_text(state_dir / PSEUDONYM_KEY_FILE),
            "SCHEMABRIDGE_PSEUDONYMIZATION_KEY_VERSION": "v1",
            "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE": ai_mode,
            "SCHEMABRIDGE_QUERY_STUDIO_AI_MODEL": reviewed_model,
            "SCHEMABRIDGE_QUERY_STUDIO_AI_REGION": PINNED_REGION,
            "SCHEMABRIDGE_RELEASE_REF": "m27-browser-acceptance",
            "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
        }
    )
    if ai_mode == "live":
        if browser_scenario is not None:
            raise BrowserAcceptanceSetupError(
                "Synthetic M27 browser scenarios cannot run with external AI."
            )
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key is None or not api_key:
            raise BrowserAcceptanceSetupError(
                "Live M27 Query Studio requires the existing OPENAI_API_KEY."
            )
        environment["OPENAI_API_KEY"] = api_key
    elif browser_scenario is not None:
        if not isinstance(browser_scenario, M27BrowserScenario):
            raise BrowserAcceptanceSetupError("M27 browser scenario is invalid.")
        environment[M27_BROWSER_SCENARIO_ENV] = browser_scenario.value
    return environment


def _policy_environment(
    state_dir: Path,
    state: BrowserAcceptanceState,
) -> dict[str, str]:
    environment = _minimal_parent_environment()
    environment.update(
        {
            "SCHEMABRIDGE_COMPONENT": "web",
            "SCHEMABRIDGE_ENVIRONMENT": "development",
            "SCHEMABRIDGE_AUTH_MODE": "local-demo",
            "SCHEMABRIDGE_LOCAL_WORKSPACE": state.local_workspace,
            "SCHEMABRIDGE_LOCAL_SUBJECT": POLICY_SUBJECT,
            "SCHEMABRIDGE_LOCAL_ROLES": '["platform_admin"]',
            "SCHEMABRIDGE_CATALOG_MODE": "recorded",
            "SCHEMABRIDGE_REGISTRY_MODE": "recorded",
            "SCHEMABRIDGE_PUBLICATION_MODE": "fake",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION": "fixed",
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION": str(CONTROL_SCHEMA_VERSION),
            "SCHEMABRIDGE_CONTROL_DATABASE_URL": _runtime_dsn(state.database),
            "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL": _migrator_dsn(state.database),
            "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": _read_secret_text(state_dir / AUDIT_KEY_FILE),
            "SCHEMABRIDGE_CONTROL_AUDIT_KEY_VERSION": "v1",
            "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": _read_secret_text(state_dir / IDENTITY_KEY_FILE),
            "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY_VERSION": "v1",
            "SCHEMABRIDGE_IDENTITY_POLICY_VERSION": "v1",
            "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE": "disabled",
            "SCHEMABRIDGE_RELEASE_REF": "m27-browser-acceptance",
        }
    )
    return environment


def _serve_streamlit(
    state_dir: Path,
    *,
    ai_mode: str,
    port: int = 8510,
    model_snapshot: str = PINNED_MODEL,
    browser_scenario: M27BrowserScenario = M27BrowserScenario.BASELINE,
) -> NoReturn:
    if isinstance(port, bool) or not isinstance(port, int) or not 1_024 <= port <= 65_535:
        raise BrowserAcceptanceSetupError("M27 Streamlit port is invalid.")
    reviewed_model = _reviewed_model_snapshot(model_snapshot)
    if not isinstance(browser_scenario, M27BrowserScenario):
        raise BrowserAcceptanceSetupError("M27 browser scenario is invalid.")
    if ai_mode == "live" and browser_scenario is not M27BrowserScenario.BASELINE:
        raise BrowserAcceptanceSetupError(
            "Synthetic M27 browser scenarios require key-free fake mode."
        )
    resolved, state = _load_state(state_dir)
    current = _query_state(
        state.database,
        state.local_workspace,
        state.local_subject,
        created_at=state.created_at,
    )
    if ai_mode == "live":
        _require_live_policy(current, model_snapshot=reviewed_model)
    executable = ROOT / ".venv/bin/streamlit"
    app = (
        ROOT / "scripts/m27_query_studio_scenario_app.py"
        if ai_mode == "fake"
        else ROOT / "src/schemabridge/entrypoints/streamlit/app.py"
    )
    arguments = (
        str(executable),
        "run",
        str(app),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    )
    os.chdir(ROOT)
    os.execve(
        executable,
        arguments,
        _runtime_environment(
            resolved,
            current,
            ai_mode=ai_mode,
            model_snapshot=reviewed_model,
            browser_scenario=(browser_scenario if ai_mode == "fake" else None),
        ),
    )


def _require_live_policy(
    state: BrowserAcceptanceState,
    *,
    model_snapshot: str = PINNED_MODEL,
) -> ProviderConfigurationFacts:
    expected = _expected_live_configuration_for_state(state, model_snapshot)
    if (
        state.ai_policy_state != "enabled"
        or state.ai_policy_model != expected.model_snapshot
        or state.ai_policy_region != expected.endpoint_region
        or state.ai_policy_configuration_fingerprint != expected.fingerprint
    ):
        raise BrowserAcceptanceSetupError(
            "Live M27 Query Studio requires the exact enabled tenant AI policy; "
            "use the helper's ai-policy CLI first."
        )
    return expected


def _evaluate_live(
    state_dir: Path,
    *,
    model_snapshot: str = PINNED_MODEL,
    resume_json: Path | None = None,
) -> NoReturn:
    resolved_resume = _resolve_live_resume_report(resume_json)
    reviewed_model = _reviewed_model_snapshot(model_snapshot)
    resolved, state = _load_state(state_dir)
    current = _query_state(
        state.database,
        state.local_workspace,
        state.local_subject,
        created_at=state.created_at,
    )
    _require_live_policy(current, model_snapshot=reviewed_model)
    executable = ROOT / ".venv/bin/python"
    evaluator = ROOT / "scripts/evaluate_query_studio_live.py"
    arguments = (
        str(executable),
        str(evaluator),
        "--execute-live",
        *(() if resolved_resume is None else ("--resume-json", str(resolved_resume))),
    )
    os.chdir(ROOT)
    os.execve(
        executable,
        arguments,
        _runtime_environment(
            resolved,
            current,
            ai_mode="live",
            model_snapshot=reviewed_model,
        ),
    )


def _attestation_signing_key(state_dir: Path) -> bytes:
    return _read_secret_text(state_dir / AUDIT_KEY_FILE).encode("utf-8")


def _attestation_summary(
    attestation: CampaignLedgerAttestation,
    path: Path,
    *,
    verified: bool,
) -> str:
    resolved = path.resolve()
    history = (ROOT / "reports/m27-query-studio-live-history").resolve()
    if (
        not resolved.is_relative_to(history)
        or resolved.parent != history
        or not resolved.is_file()
        or resolved.is_symlink()
    ):
        raise BrowserAcceptanceSetupError("The M27 campaign ledger attestation path is invalid.")
    return json.dumps(
        {
            "attestation_schema_version": attestation.schema_version,
            "attestation_file_sha256": resolved.stem.rsplit("-", 1)[-1],
            "attestation_path": str(resolved.relative_to(ROOT)),
            "campaign_report_sha256": (attestation.campaign.logical_report_sha256),
            "campaign_ordered_execution_witness_digest_sha256": (
                attestation.campaign.ordered_execution_witness_digest_sha256
            ),
            "ledger_digest_sha256": attestation.ledger.ledger_digest_sha256,
            "ordinal_binding_digest_sha256": (attestation.ledger.ordinal_binding_digest_sha256),
            "provider_attempts": attestation.ledger.reservation_count,
            "unique_compatible_matching_count": (
                attestation.ledger.unique_compatible_matching_count
            ),
            "verified": verified,
        },
        sort_keys=True,
    )


def _attest_live(state_dir: Path) -> str:
    """Create one provider-free attestation from the retained read-only ledger."""

    try:
        resolved, state = _load_state(state_dir)
        signing_key = _attestation_signing_key(resolved)
        campaign = load_canonical_campaign_evidence(
            ROOT,
            CANONICAL_CAMPAIGN_PATH,
            signing_key=signing_key,
            signing_key_version=LIVE_EVIDENCE_KEY_VERSION,
        )
        snapshot = read_campaign_ledger_snapshot(
            _migrator_dsn(state.database),
            state.workspace_id,
        )
        attestation = build_campaign_ledger_attestation(campaign, snapshot)
        path = write_campaign_ledger_attestation(
            attestation,
            ROOT,
            signing_key=signing_key,
            signing_key_version=LIVE_EVIDENCE_KEY_VERSION,
        )
        return _attestation_summary(attestation, path, verified=True)
    except BrowserAcceptanceSetupError:
        raise
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The M27 campaign ledger attestation failed closed."
        ) from error


def _verify_live_attestation(
    state_dir: Path,
    attestation_path: Path,
) -> str:
    """Authenticate the file and recompute it from a fresh read-only snapshot."""

    try:
        resolved, state = _load_state(state_dir)
        signing_key = _attestation_signing_key(resolved)
        persisted = load_campaign_ledger_attestation(
            ROOT,
            attestation_path,
            signing_key=signing_key,
            signing_key_version=LIVE_EVIDENCE_KEY_VERSION,
        )
        campaign = load_canonical_campaign_evidence(
            ROOT,
            CANONICAL_CAMPAIGN_PATH,
            signing_key=signing_key,
            signing_key_version=LIVE_EVIDENCE_KEY_VERSION,
        )
        snapshot = read_campaign_ledger_snapshot(
            _migrator_dsn(state.database),
            state.workspace_id,
        )
        verified = verify_campaign_ledger_attestation(
            persisted,
            campaign,
            snapshot,
        )
        configured = attestation_path if attestation_path.is_absolute() else ROOT / attestation_path
        return _attestation_summary(
            verified,
            configured,
            verified=True,
        )
    except BrowserAcceptanceSetupError:
        raise
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The M27 campaign ledger attestation verification failed closed."
        ) from error


def _resolve_live_resume_report(configured: Path | None) -> Path | None:
    """Allow resumption only from one existing JSON report below this repository."""

    if configured is None:
        return None
    candidate = configured if configured.is_absolute() else ROOT / configured
    resolved = candidate.resolve()
    reports_root = (ROOT / "reports").resolve()
    if (
        not resolved.is_relative_to(reports_root)
        or resolved.suffix != ".json"
        or not resolved.is_file()
    ):
        raise BrowserAcceptanceSetupError(
            "M27 live evaluation resume must be an existing repository report JSON."
        )
    return resolved


def _run_ai_policy_cli(
    state_dir: Path,
    policy_arguments: Sequence[str],
) -> NoReturn:
    resolved, state = _load_state(state_dir)
    if not policy_arguments or policy_arguments[0] not in {"inspect", "prepare", "apply"}:
        raise BrowserAcceptanceSetupError(
            "M27 AI policy requires an inspect, prepare, or apply CLI command."
        )
    if "--actor" in policy_arguments or "--workspace-id" in policy_arguments:
        raise BrowserAcceptanceSetupError(
            "M27 AI policy scope and actor are supplied by the retained runtime."
        )
    command = policy_arguments[0]
    remainder = tuple(policy_arguments[1:])
    if command in {"inspect", "prepare"}:
        remainder = ("--workspace-id", state.workspace_id, *remainder)
    executable = ROOT / ".venv/bin/python"
    os.chdir(ROOT)
    os.execve(
        executable,
        (
            str(executable),
            "-m",
            "schemabridge.entrypoints.ai_policy.main",
            command,
            *remainder,
        ),
        _policy_environment(resolved, state),
    )


def _read_stale_scope_witness(
    state: BrowserAcceptanceState,
) -> _BrowserAcceptanceStaleWitness:
    scope_params = (state.workspace_id, CATALOG_SCOPE, REGISTRY_ID)
    try:
        with psycopg.connect(_role_dsn("schemabridge_api", state.database)) as connection:
            active_catalog = connection.execute(
                """
                SELECT active_generation
                FROM schemabridge_control.catalog_connections
                WHERE workspace_id = %s
                  AND catalog_scope = %s
                  AND connection_id = %s
                  AND status = 'enabled'
                """,
                (state.workspace_id, CATALOG_SCOPE, PRIMARY_CONNECTION_ID),
            ).fetchone()
        with psycopg.connect(_runtime_dsn(state.database)) as connection:
            physical = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.load_physical_discovery_scope(
                    %s::varchar, %s::varchar, %s::varchar
                )
                """,
                scope_params,
            ).fetchone()
            governance = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.load_query_studio_governance_scope(
                    %s::varchar, %s::varchar, %s::varchar
                )
                """,
                scope_params,
            ).fetchone()
        if (
            active_catalog is None
            or len(active_catalog) != 1
            or active_catalog[0] is None
            or physical is None
            or len(physical) != 4
            or (governance is not None and len(governance) != 10)
        ):
            raise ValueError
        return _BrowserAcceptanceStaleWitness(
            active_catalog_generation=int(active_catalog[0]),
            physical_catalog_generation_vector_fingerprint=str(physical[0]).strip(),
            connection_count=int(physical[1]),
            asset_count=int(physical[2]),
            field_count=int(physical[3]),
            registry_generation=None if governance is None else int(governance[0]),
            registry_version=None if governance is None else int(governance[1]),
            registry_fingerprint=(None if governance is None else str(governance[2]).strip()),
            governed_catalog_generation_vector_fingerprint=(
                None if governance is None else str(governance[8]).strip()
            ),
            governed_mapping_count=(None if governance is None else int(governance[9])),
        )
    except BrowserAcceptanceSetupError:
        raise
    except (psycopg.Error, TypeError, ValueError, IndexError) as error:
        raise BrowserAcceptanceSetupError(
            "The controlled M27 stale-preview witness is unavailable."
        ) from error


def _require_stale_baseline(
    state: BrowserAcceptanceState,
    witness: _BrowserAcceptanceStaleWitness,
) -> None:
    if (
        witness.active_catalog_generation != state.catalog_generation
        or witness.physical_catalog_generation_vector_fingerprint
        != state.physical_catalog_generation_vector_fingerprint
        or witness.connection_count != state.connection_count
        or witness.asset_count != state.asset_count
        or witness.field_count != state.field_count
        or witness.registry_generation != state.registry_generation
        or witness.registry_version != state.registry_version
        or witness.registry_fingerprint != state.registry_fingerprint
        or witness.governed_catalog_generation_vector_fingerprint
        != state.governed_catalog_generation_vector_fingerprint
        or witness.governed_mapping_count != state.governed_mapping_count
    ):
        raise BrowserAcceptanceSetupError(
            "The M27 stale-preview scenario requires the untouched retained baseline."
        )


def _apply_stale_catalog_drift(
    state: BrowserAcceptanceState,
) -> BrowserAcceptanceStaleResult:
    requested_at = datetime.fromisoformat(state.created_at)
    idempotency_digest = hashlib.sha256(
        (
            "m27-browser-stale-catalog-v1"
            f"\0{state.database}\0{state.workspace_id}\0{state.catalog_generation}"
        ).encode()
    ).hexdigest()
    lease_capability = secrets.token_urlsafe(48)
    expected_generation = state.catalog_generation + 1
    try:
        requested = PostgresCatalogRefreshStore(
            _role_dsn("schemabridge_api", state.database),
            application_name="schemabridge-control-api",
        ).request(
            CatalogRefreshCommand(
                workspace_id=state.workspace_id,
                connection_id=CatalogConnectionId(PRIMARY_CONNECTION_ID),
                mode=CatalogRefreshMode.DELTA,
                requested_by=STALE_CATALOG_ACTOR,
                requested_at=requested_at,
                idempotency_digest=idempotency_digest,
            )
        )
        catalog = PostgresCatalogRefreshStore(
            _role_dsn("schemabridge_catalog", state.database),
            application_name="schemabridge-control-catalog",
        )
        leased = catalog.claim_next(
            indexer_id=STALE_CATALOG_INDEXER,
            lease_capability=lease_capability,
            lease_duration=timedelta(minutes=5),
        )
        if (
            leased is None
            or leased.refresh_id != requested.refresh.refresh_id
            or leased.lease is None
        ):
            raise BrowserAcceptanceSetupError(
                "The controlled M27 catalog drift did not acquire its exact refresh."
            )
        staging = catalog.begin_staging(
            state.workspace_id,
            leased.refresh_id,
            indexer_id=leased.lease.indexer_id,
            lease_capability=lease_capability,
            fencing_token=leased.lease.fencing_token,
        )
        if (
            staging.base_generation != state.catalog_generation
            or staging.target_generation != expected_generation
        ):
            raise BrowserAcceptanceSetupError(
                "The controlled M27 catalog drift did not clone the retained generation."
            )
        catalog.persist_page(
            state.workspace_id,
            leased.refresh_id,
            indexer_id=leased.lease.indexer_id,
            lease_capability=lease_capability,
            fencing_token=leased.lease.fencing_token,
            page=CatalogSourcePage.create(
                mode=CatalogRefreshMode.DELTA,
                sequence=1,
                changes=(),
                next_checkpoint=None,
                source_complete=True,
            ),
        )
        completed = catalog.complete(
            state.workspace_id,
            leased.refresh_id,
            indexer_id=leased.lease.indexer_id,
            lease_capability=lease_capability,
            fencing_token=leased.lease.fencing_token,
            expected_base_generation=state.catalog_generation,
        )
        if completed.target_generation != expected_generation:
            raise BrowserAcceptanceSetupError(
                "The controlled M27 catalog drift did not promote one generation."
            )

        witness = _read_stale_scope_witness(state)
        if (
            witness.active_catalog_generation != expected_generation
            or witness.physical_catalog_generation_vector_fingerprint
            == state.physical_catalog_generation_vector_fingerprint
            or witness.connection_count != state.connection_count
            or witness.asset_count != state.asset_count
            or witness.field_count != state.field_count
            or witness.registry_generation != state.registry_generation
            or witness.registry_version != state.registry_version
            or witness.registry_fingerprint != state.registry_fingerprint
            or witness.governed_catalog_generation_vector_fingerprint
            != state.governed_catalog_generation_vector_fingerprint
            or witness.governed_mapping_count != state.governed_mapping_count
        ):
            raise BrowserAcceptanceSetupError(
                "The controlled M27 catalog drift witness is incomplete."
            )
        page = PostgresGovernedBindingFactsSearch(_runtime_dsn(state.database)).search(
            GovernedBindingFactsRequest(
                scope=SemanticRegistryScope(
                    workspace_id=state.workspace_id,
                    catalog_scope=CATALOG_SCOPE,
                    registry_id=REGISTRY_ID,
                ),
                page_size=EXPECTED_GOVERNED_MAPPING_COUNT,
            )
        )
        if (
            len(page.items) != state.governed_mapping_count
            or page.next_key is not None
            or any(item.catalog_generation != expected_generation for item in page.items)
        ):
            raise BrowserAcceptanceSetupError(
                "The controlled M27 catalog drift did not change the signed shortlist facts."
            )
    except BrowserAcceptanceSetupError:
        raise
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The controlled M27 catalog drift could not be applied."
        ) from error
    return BrowserAcceptanceStaleResult(
        scenario=M27StaleScenario.CATALOG,
        drift_dimension="catalog_generation",
        before_generation=state.catalog_generation,
        after_generation=expected_generation,
    )


def _apply_stale_registry_drift(
    state_dir: Path,
    state: BrowserAcceptanceState,
) -> BrowserAcceptanceStaleResult:
    scope = SemanticRegistryScope(
        workspace_id=state.workspace_id,
        catalog_scope=CATALOG_SCOPE,
        registry_id=REGISTRY_ID,
    )
    approved_at = datetime.now(UTC)
    try:
        control = PostgresRegistryControlStore(
            _runtime_dsn(state.database),
            {"v1": _read_secret_text(state_dir / AUDIT_KEY_FILE).encode("ascii")},
            "v1",
        )
        current = control.load_active(scope)
        if (
            current is None
            or current.generation != state.registry_generation
            or current.registry_version != state.registry_version
            or current.registry_fingerprint != state.registry_fingerprint
        ):
            raise BrowserAcceptanceSetupError(
                "The controlled M27 registry drift did not find the retained pointer."
            )
        rollback_targets = tuple(
            transition
            for transition in control.list_transitions(scope, limit=100)
            if transition.active_pointer.generation == 1
            and transition.active_pointer.registry_version == 1
            and transition.active_pointer.scope == scope
        )
        if len(rollback_targets) != 1:
            raise BrowserAcceptanceSetupError(
                "The controlled M27 registry rollback target is not exact."
            )
        versions = DataHubRegistryVersionReader(
            DataHubRegistryReadConfig.from_env_file((ROOT / ".local/datahub/mcp.env").resolve())
        )
        proposal = PrepareRegistryRollback(control, versions, scope).execute(rollback_targets[0].id)
        approval = PrepareRegistryActivationApproval().execute(
            proposal,
            actor=STALE_REGISTRY_ACTOR,
            approved_at=approved_at,
            confirmation=(RegistryActivationConfirmation.ROLLBACK_TO_APPROVED_REGISTRY_VERSION),
        )
        committed = CommitRegistryActivation(control, versions).execute(
            proposal,
            approval,
            committed_at=approved_at + timedelta(seconds=1),
        )
        active = committed.transition.active_pointer
        if (
            active.generation != state.registry_generation + 1
            or active.registry_version != 1
            or active.registry_fingerprint == state.registry_fingerprint
            or active.scope != scope
        ):
            raise BrowserAcceptanceSetupError(
                "The controlled M27 registry rollback did not change the pointer."
            )
        witness = _read_stale_scope_witness(state)
        if (
            witness.active_catalog_generation != state.catalog_generation
            or witness.physical_catalog_generation_vector_fingerprint
            != state.physical_catalog_generation_vector_fingerprint
            or witness.connection_count != state.connection_count
            or witness.asset_count != state.asset_count
            or witness.field_count != state.field_count
            or witness.registry_generation is not None
            or witness.registry_version is not None
            or witness.registry_fingerprint is not None
            or witness.governed_catalog_generation_vector_fingerprint is not None
            or witness.governed_mapping_count is not None
        ):
            raise BrowserAcceptanceSetupError(
                "The controlled M27 registry drift remained confirmable."
            )
    except BrowserAcceptanceSetupError:
        raise
    except Exception as error:
        raise BrowserAcceptanceSetupError(
            "The controlled M27 registry drift could not be applied."
        ) from error
    return BrowserAcceptanceStaleResult(
        scenario=M27StaleScenario.REGISTRY,
        drift_dimension="registry_pointer_generation",
        before_generation=state.registry_generation,
        after_generation=state.registry_generation + 1,
    )


def _apply_stale_scenario(
    state_dir: Path,
    scenario: M27StaleScenario,
    confirmation: str,
) -> BrowserAcceptanceStaleResult:
    if not isinstance(scenario, M27StaleScenario):
        raise BrowserAcceptanceSetupError("The M27 stale-preview scenario is invalid.")
    expected_confirmation = {
        M27StaleScenario.CATALOG: STALE_CATALOG_CONFIRMATION,
        M27StaleScenario.REGISTRY: STALE_REGISTRY_CONFIRMATION,
    }[scenario]
    if confirmation != expected_confirmation:
        raise BrowserAcceptanceSetupError("The exact M27 stale-preview confirmation is required.")
    resolved, state = _load_state(state_dir)
    _require_stale_baseline(state, _read_stale_scope_witness(state))
    if scenario is M27StaleScenario.CATALOG:
        return _apply_stale_catalog_drift(state)
    return _apply_stale_registry_drift(resolved, state)


def _status(state_dir: Path) -> BrowserAcceptanceState:
    _resolved, state = _load_state(state_dir)
    postflight_metrics = _run_catalog_postflight(state)
    current = _query_state(
        state.database,
        state.local_workspace,
        state.local_subject,
        created_at=state.created_at,
    )
    return replace(current, postflight_metrics=postflight_metrics)


def _cleanup(state_dir: Path, confirmation: str) -> None:
    if confirmation != DROP_CONFIRMATION:
        raise BrowserAcceptanceSetupError("The exact M27 cleanup confirmation is required.")
    resolved, state = _load_state(state_dir)
    expected = {STATE_FILE, *_STATE_SECRET_FILES}
    actual = {item.name for item in resolved.iterdir()}
    if actual != expected or any(not item.is_file() for item in resolved.iterdir()):
        raise BrowserAcceptanceSetupError(
            "The M27 state directory contains unexpected material and was retained."
        )
    _drop_database(state.database)
    _remove_exact_state_files(resolved, expected)


def _safe_summary(
    state: BrowserAcceptanceState,
    *,
    model_snapshot: str = PINNED_MODEL,
) -> str:
    expected_live_configuration = _expected_live_configuration_for_state(
        state,
        model_snapshot,
    )
    payload: dict[str, object] = {
        "database": state.database,
        "cardinality_profile": state.cardinality_profile,
        "control_schema_version": state.control_schema_version,
        "registry_generation": state.registry_generation,
        "registry_version": state.registry_version,
        "evidence_head_revision": state.evidence_head_revision,
        "evidence_baseline_revision": state.evidence_baseline_revision,
        "catalog_generation": state.catalog_generation,
        "connection_count": state.connection_count,
        "physical_asset_count": state.asset_count,
        "physical_field_count": state.field_count,
        "governed_mapping_count": state.governed_mapping_count,
        "expected_live_model": expected_live_configuration.model_snapshot,
        "expected_live_configuration_fingerprint": (expected_live_configuration.fingerprint),
        "external_ai_policy": {
            "state": state.ai_policy_state,
            "version": state.ai_policy_version,
            "model": state.ai_policy_model,
            "region": state.ai_policy_region,
            "matches_expected_configuration": (
                state.ai_policy_state == "enabled"
                and state.ai_policy_model == expected_live_configuration.model_snapshot
                and state.ai_policy_region == expected_live_configuration.endpoint_region
                and state.ai_policy_configuration_fingerprint
                == expected_live_configuration.fingerprint
            ),
        },
    }
    if state.postflight_metrics is not None:
        payload["postflight_ms"] = {
            "cold_physical_empty": state.postflight_metrics.cold_physical_empty_ms,
            "physical_hostile_exact": state.postflight_metrics.physical_hostile_exact_ms,
            "governed_traversal": state.postflight_metrics.governed_traversal_ms,
        }
    return json.dumps(
        payload,
        sort_keys=True,
    )


def _policy_guide(
    state_dir: Path,
    *,
    model_snapshot: str = PINNED_MODEL,
) -> str:
    reviewed_model = _reviewed_model_snapshot(model_snapshot)
    _, state = _load_state(state_dir)
    executable = ROOT / ".venv/bin/python"
    helper = ROOT / "scripts/m27_browser_acceptance_runtime.py"
    prefix = f"{executable} {helper} --state-dir {state_dir}"
    expected_configuration_fingerprint = _expected_live_configuration_for_state(
        state,
        reviewed_model,
    ).fingerprint
    expected_endpoint_origin_fingerprint = _expected_live_endpoint_origin_fingerprint(
        reviewed_model
    )
    return "\n".join(
        (
            "Tenant external AI remains an explicit CLI-only operator decision.",
            f"Inspect (read-only): {prefix} ai-policy inspect",
            (
                "Prepare (read-only): "
                f"{prefix} ai-policy prepare --expected-version <n> "
                "--external-ai-enabled --provider-governance-accepted "
                "--provider-governance-fingerprint <sha256> "
                f"--model-snapshot {reviewed_model} --endpoint-region {PINNED_REGION} "
                "--endpoint-origin-fingerprint "
                f"{expected_endpoint_origin_fingerprint} "
                f"--configuration-fingerprint {expected_configuration_fingerprint} "
                "--proposal-output <owner-only-json-outside-state-dir>"
            ),
            (
                "The policy CLI exclusively creates that proposal as owner-only 0600; "
                "review every value, then apply the same file:"
            ),
            (
                f"{prefix} ai-policy apply "
                "--proposal-file <owner-only-json-outside-state-dir> "
                "--expected-proposal-fingerprint <sha256> "
                "--confirm 'APPLY TENANT AI POLICY'"
            ),
            "No policy helper command writes policy by SQL or invokes the provider.",
            (
                "After applying the exact policy, run the explicit synthetic live evaluation: "
                f"{prefix} evaluate-live --ai-model {reviewed_model}"
            ),
            (
                "Resume retained cheapest-first evidence only after a newer exact policy: "
                f"{prefix} evaluate-live --ai-model {reviewed_model} "
                "--resume-json reports/<retained-campaign>.json"
            ),
        )
    )


def _add_reviewed_model_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ai-model",
        choices=REVIEWED_MODEL_SNAPSHOTS,
        default=PINNED_MODEL,
        help="exact reviewed Query Studio snapshot; defaults to the current pinned model",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate the real M27 Query Studio browser-acceptance runtime.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    prepare = subcommands.add_parser("prepare")
    prepare.add_argument(
        "--cardinality-profile",
        choices=tuple(sorted(_CARDINALITY_PROFILES)),
        default="large",
    )
    status = subcommands.add_parser("status")
    _add_reviewed_model_argument(status)
    streamlit = subcommands.add_parser("streamlit")
    streamlit.add_argument("--ai-mode", choices=("fake", "live"), default="fake")
    streamlit.add_argument("--port", type=int, default=8510)
    streamlit.add_argument(
        "--scenario",
        choices=tuple(item.value for item in M27BrowserScenario),
        default=M27BrowserScenario.BASELINE.value,
        help="closed key-free synthetic browser state; fake mode only",
    )
    _add_reviewed_model_argument(streamlit)
    evaluate_live = subcommands.add_parser("evaluate-live")
    _add_reviewed_model_argument(evaluate_live)
    evaluate_live.add_argument(
        "--resume-json",
        type=Path,
        help="retained campaign JSON below reports/; requires a newer exact policy",
    )
    subcommands.add_parser(
        "attest-live",
        help="provider-free campaign-to-ledger attestation from the migrator reader",
    )
    verify_live = subcommands.add_parser(
        "verify-live-attestation",
        help="authenticate and recompute one retained campaign-to-ledger attestation",
    )
    verify_live.add_argument("--attestation-json", type=Path, required=True)
    policy = subcommands.add_parser("ai-policy")
    policy.add_argument("policy_arguments", nargs=argparse.REMAINDER)
    policy_guide = subcommands.add_parser("policy-guide")
    _add_reviewed_model_argument(policy_guide)
    stale = subcommands.add_parser(
        "stale",
        help="apply one real controlled drift after issuing a baseline fake preview",
    )
    stale.add_argument(
        "scenario",
        choices=tuple(item.value for item in M27StaleScenario),
    )
    stale.add_argument("--confirm", required=True)
    cleanup = subcommands.add_parser("cleanup")
    cleanup.add_argument("--confirm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            print(
                _safe_summary(
                    _prepare(
                        arguments.state_dir,
                        cardinality_profile=str(arguments.cardinality_profile),
                    )
                )
            )
        elif arguments.command == "status":
            print(
                _safe_summary(
                    _status(arguments.state_dir),
                    model_snapshot=str(arguments.ai_model),
                )
            )
        elif arguments.command == "streamlit":
            _serve_streamlit(
                arguments.state_dir,
                ai_mode=str(arguments.ai_mode),
                port=int(arguments.port),
                model_snapshot=str(arguments.ai_model),
                browser_scenario=M27BrowserScenario(str(arguments.scenario)),
            )
        elif arguments.command == "evaluate-live":
            _evaluate_live(
                arguments.state_dir,
                model_snapshot=str(arguments.ai_model),
                resume_json=arguments.resume_json,
            )
        elif arguments.command == "attest-live":
            print(_attest_live(arguments.state_dir))
        elif arguments.command == "verify-live-attestation":
            print(
                _verify_live_attestation(
                    arguments.state_dir,
                    arguments.attestation_json,
                )
            )
        elif arguments.command == "ai-policy":
            _run_ai_policy_cli(
                arguments.state_dir,
                tuple(str(value) for value in arguments.policy_arguments),
            )
        elif arguments.command == "policy-guide":
            print(
                _policy_guide(
                    _resolve_state_dir(arguments.state_dir),
                    model_snapshot=str(arguments.ai_model),
                )
            )
        elif arguments.command == "stale":
            print(
                _apply_stale_scenario(
                    arguments.state_dir,
                    M27StaleScenario(str(arguments.scenario)),
                    str(arguments.confirm),
                ).to_json()
            )
        elif arguments.command == "cleanup":
            _cleanup(arguments.state_dir, str(arguments.confirm))
            print("M27 browser acceptance database and owner-only state removed.")
        else:
            raise BrowserAcceptanceSetupError("The M27 browser acceptance command is invalid.")
    except BrowserAcceptanceSetupError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
