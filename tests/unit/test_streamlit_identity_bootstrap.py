from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from schemabridge import bootstrap
from schemabridge.adapters.storage.identity_resolving import (
    IdentityResolvingWorkflowAccessStore,
    IdentityResolvingWorkflowDraftStore,
)
from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.ports.identity_rotation import (
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.bootstrap import (
    build_agent_workflow_orchestrator,
    build_query_recipe_preparer,
    build_streamlit_principal,
    build_streamlit_runtime_options,
    build_streamlit_ui_service,
    build_workflow_access_store,
    build_workflow_draft_store,
)
from schemabridge.config import Settings
from schemabridge.domain.identity import AuthenticationMethod, IdentityRole
from schemabridge.domain.identity_rotation import IdentityAuthorizationScope
from schemabridge.domain.workflows import AgentWorkflowDraft

NOW = datetime(2026, 7, 23, 16, 0, tzinfo=UTC)


def _opaque(kind: str, version: str, label: str) -> str:
    digest = hashlib.sha256(f"{kind}:{version}:{label}".encode()).hexdigest()
    return f"sb_{kind}_{version}_{digest}"


class _Resolver:
    def __init__(self, workspace_id: str, actor_id: str, *, initialized: bool = True) -> None:
        self.workspace_id = workspace_id
        self.actor_id = actor_id
        self.initialized = initialized

    def resolve_workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        if not self.initialized or workspace_id != self.workspace_id:
            raise self._unknown()
        return (workspace_id,)

    def resolve_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        if not self.initialized or workspace_id != self.workspace_id or actor_id != self.actor_id:
            raise self._unknown()
        return (
            IdentityAuthorizationScope(
                workspace_id=workspace_id,
                actor_id=actor_id,
                key_version="v2",
            ),
        )

    @staticmethod
    def _unknown() -> IdentityRotationStoreError:
        return IdentityRotationStoreError(
            IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
            "unknown identity",
        )


class _DraftStore:
    def load(self, workflow_id: str) -> AgentWorkflowDraft | None:
        del workflow_id
        return None

    def save(
        self,
        draft: AgentWorkflowDraft,
        *,
        expected_revision: int | None,
    ) -> None:
        del draft, expected_revision


def _production_settings(tmp_path: Path) -> Settings:
    connector_secret_directory = tmp_path / "connector-secrets"
    connector_secret_directory.mkdir(mode=0o700)
    return Settings.model_validate(
        {
            "SCHEMABRIDGE_ENVIRONMENT": "production",
            "SCHEMABRIDGE_AUTH_MODE": "oidc",
            "SCHEMABRIDGE_CATALOG_MODE": "live",
            "SCHEMABRIDGE_PUBLICATION_MODE": "fake",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
            "SCHEMABRIDGE_OIDC_ISSUER": "https://identity.example.test",
            "SCHEMABRIDGE_OIDC_AUDIENCE": "schemabridge",
            "SCHEMABRIDGE_OIDC_PROVIDER": "corporate-oidc",
            "SCHEMABRIDGE_OIDC_ROLE_CLAIM": "groups",
            "SCHEMABRIDGE_OIDC_TENANT_CLAIM": "tenant_id",
            "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS": {
                "data-team": ("analyst", "publisher"),
            },
            "SCHEMABRIDGE_OIDC_ALLOWED_TENANTS": ("tenant-a",),
            "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": (
                "unit-test-pseudonymization-key-at-least-32-bytes"
            ),
            "DATABASE_URL": (
                "postgresql://source_reader:source_password@source.example.test/source"
                "?sslmode=verify-full"
            ),
            "SCHEMABRIDGE_CONTROL_DATABASE_URL": (
                "postgresql://control_runtime:control_password@control.example.test/control"
                "?sslmode=verify-full"
            ),
            "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
                "unit-test-control-audit-signing-key-with-diversity"
            ),
            "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": (
                "unit-test-identity-migration-key-with-diversity"
            ),
            "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": (
                "unit-test-query-studio-signing-key-with-diversity"
            ),
            "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": connector_secret_directory,
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "production.db",
        }
    )


def _claims() -> dict[str, object]:
    return {
        "iss": "https://identity.example.test",
        "sub": "provider-subject",
        "aud": "schemabridge",
        "iat": (NOW - timedelta(minutes=1)).timestamp(),
        "nbf": (NOW - timedelta(minutes=1)).timestamp(),
        "exp": (NOW + timedelta(minutes=5)).timestamp(),
        "tenant_id": "tenant-a",
        "groups": ["data-team"],
        "email": "not-retained@example.test",
    }


def test_runtime_options_expose_only_server_selected_non_secret_modes(tmp_path: Path) -> None:
    options = build_streamlit_runtime_options(_production_settings(tmp_path))

    assert options.profile == "production"
    assert options.auth_mode == "oidc"
    assert options.catalog_kind == "live"
    assert options.registry_kind == "live"
    assert options.publication_kind == "fake"
    assert options.execution_kind == "recorded"
    assert options.oidc_provider == "corporate-oidc"
    assert options.oidc_audience == "schemabridge"
    assert "secret" not in repr(options).casefold()


def test_managed_web_rejects_operator_database_credentials(tmp_path: Path) -> None:
    settings = _production_settings(tmp_path).model_copy(
        update={
            "control_migrator_database_url": SecretStr(
                "postgresql://migrator:must-not-enter-web@control.example.test/control"
                "?sslmode=verify-full"
            )
        }
    )

    with pytest.raises(RuntimeError, match="must not receive"):
        build_streamlit_runtime_options(settings)


def test_managed_web_rejects_restore_database_credentials(tmp_path: Path) -> None:
    settings = _production_settings(tmp_path).model_copy(
        update={
            "control_restore_database_url": SecretStr(
                "postgresql://restore:must-not-enter-web@restore.example.test/control_restore"
                "?sslmode=verify-full"
            )
        }
    )

    with pytest.raises(RuntimeError, match="must not receive"):
        build_streamlit_runtime_options(settings)


def test_managed_web_rejects_operator_identity_and_roles(tmp_path: Path) -> None:
    settings = _production_settings(tmp_path).model_copy(
        update={
            "control_operator_actor_id": _opaque("actor", "v2", "operator"),
            "control_operator_roles": ("publisher",),
        }
    )

    with pytest.raises(RuntimeError, match="must not receive"):
        build_streamlit_runtime_options(settings)


def test_composition_maps_authenticated_claims_and_discards_pii(tmp_path: Path) -> None:
    principal = build_streamlit_principal(
        claims=_claims(),
        now=NOW,
        settings=_production_settings(tmp_path),
    )

    assert principal.authentication_method is AuthenticationMethod.OIDC
    assert principal.roles == frozenset({IdentityRole.ANALYST, IdentityRole.PUBLISHER})
    assert "provider-subject" not in repr(principal)
    assert "tenant-a" not in repr(principal)
    assert "not-retained@example.test" not in repr(principal)


def test_composition_rejects_a_validly_shaped_but_unallowlisted_tenant(
    tmp_path: Path,
) -> None:
    with pytest.raises(AuthenticationBoundaryError) as failure:
        build_streamlit_principal(
            claims={**_claims(), "tenant_id": "tenant-not-authorized"},
            now=NOW,
            settings=_production_settings(tmp_path),
        )

    assert failure.value.code == "tenant_not_allowed"
    assert str(failure.value) == "The authenticated session was rejected."
    assert "tenant-not-authorized" not in str(failure.value)
    assert "tenant-not-authorized" not in repr(failure.value)


def test_managed_ui_rejects_browser_or_caller_mode_override_before_composition(
    tmp_path: Path,
) -> None:
    settings = _production_settings(tmp_path)
    principal = build_streamlit_principal(claims=_claims(), now=NOW, settings=settings)

    with pytest.raises(ValueError, match="fixed by the production deployment profile"):
        build_streamlit_ui_service(
            catalog_kind="recorded",
            settings=settings,
            principal=principal,
        )


def test_local_identity_cannot_be_used_to_select_live_publication(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "local.db",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
        }
    )

    with pytest.raises(ValueError, match="live publication requires"):
        build_streamlit_ui_service(
            publication_kind="live",
            settings=settings,
        )


def test_oidc_ui_composition_requires_an_authenticated_principal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires authenticated Streamlit claims"):
        build_streamlit_ui_service(settings=_production_settings(tmp_path))


def test_missing_authlib_fails_closed_before_login_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", lambda _name: None)

    with pytest.raises(RuntimeError, match=r"install schemabridge\[ui\]"):
        build_streamlit_runtime_options(_production_settings(tmp_path))


def test_postgres_oidc_store_builders_compose_identity_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _production_settings(tmp_path)
    workspace_id = _opaque("workspace", "v2", "workspace")
    actor_id = _opaque("actor", "v2", "owner")
    resolver = _Resolver(workspace_id, actor_id)
    raw_access = object()
    active_draft = _DraftStore()

    monkeypatch.setattr(
        bootstrap,
        "_build_postgres_identity_rotation_store",
        lambda _settings: resolver,
    )
    monkeypatch.setattr(
        bootstrap,
        "_build_postgres_workflow_access_store",
        lambda _settings: raw_access,
    )
    monkeypatch.setattr(
        bootstrap,
        "_build_postgres_workflow_draft_store",
        lambda _settings, *, workspace_id, owner_actor_id: active_draft,
    )

    access = build_workflow_access_store(
        workspace_id=workspace_id,
        owner_actor_id=actor_id,
        settings=settings,
    )
    drafts = build_workflow_draft_store(
        workspace_id=workspace_id,
        owner_actor_id=actor_id,
        settings=settings,
    )

    assert isinstance(access, IdentityResolvingWorkflowAccessStore)
    assert access.store is raw_access
    assert access.resolver is resolver
    assert isinstance(drafts, IdentityResolvingWorkflowDraftStore)
    assert drafts.active_store is active_draft
    assert drafts.resolver is resolver


def test_postgres_oidc_store_builders_fail_closed_for_unknown_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _production_settings(tmp_path)
    workspace_id = _opaque("workspace", "v2", "unknown-workspace")
    actor_id = _opaque("actor", "v2", "unknown-owner")
    resolver = _Resolver(workspace_id, actor_id, initialized=False)

    monkeypatch.setattr(
        bootstrap,
        "_build_postgres_identity_rotation_store",
        lambda _settings: resolver,
    )
    monkeypatch.setattr(
        bootstrap,
        "_build_postgres_workflow_access_store",
        lambda _settings: object(),
    )
    monkeypatch.setattr(
        bootstrap,
        "_build_postgres_workflow_draft_store",
        lambda _settings, *, workspace_id, owner_actor_id: _DraftStore(),
    )

    with pytest.raises(DatabaseConfigurationError, match="not initialized"):
        build_workflow_access_store(
            workspace_id=workspace_id,
            owner_actor_id=actor_id,
            settings=settings,
        )
    with pytest.raises(DatabaseConfigurationError, match="not initialized"):
        build_workflow_draft_store(
            workspace_id=workspace_id,
            owner_actor_id=actor_id,
            settings=settings,
        )


def test_local_demo_postgres_preserves_exact_store_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_DATABASE_URL": (
                "postgresql://runtime:password@control.example.test/control"
            ),
            "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
                "unit-test-control-audit-signing-key-with-diversity"
            ),
            "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": (
                "unit-test-identity-migration-key-with-diversity"
            ),
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "local-postgres.db",
        }
    )
    raw_access = object()
    raw_draft = _DraftStore()

    monkeypatch.setattr(
        bootstrap,
        "_build_postgres_workflow_access_store",
        lambda _settings: raw_access,
    )
    monkeypatch.setattr(
        bootstrap,
        "_build_postgres_workflow_draft_store",
        lambda _settings, *, workspace_id, owner_actor_id: raw_draft,
    )

    def forbidden_identity_builder(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("local-demo PostgreSQL must not resolve OIDC lineage")

    monkeypatch.setattr(bootstrap, "build_identity_rotation_store", forbidden_identity_builder)

    assert build_workflow_access_store(settings=settings) is raw_access
    assert (
        build_workflow_draft_store(
            workspace_id="local-workspace",
            owner_actor_id="local-owner",
            settings=settings,
        )
        is raw_draft
    )


def test_recipe_preparer_uses_the_central_workflow_draft_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "local.db",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
        }
    )
    drafts = _DraftStore()
    calls: list[tuple[str | None, str | None]] = []

    def fake_builder(
        *,
        workspace_id: str | None = None,
        owner_actor_id: str | None = None,
        settings: Settings | None = None,
    ) -> _DraftStore:
        assert settings is not None
        calls.append((workspace_id, owner_actor_id))
        return drafts

    monkeypatch.setattr(bootstrap, "build_workflow_draft_store", fake_builder)
    preparer = build_query_recipe_preparer(
        settings=settings,
        workspace_id="workspace",
        owner_actor_id="owner",
    )

    assert preparer.store is drafts
    assert calls == [("workspace", "owner")]


def test_orchestrator_and_ui_use_the_central_workflow_store_builders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "local.db",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
        }
    )
    drafts = _DraftStore()
    access = object()

    monkeypatch.setattr(
        bootstrap,
        "build_workflow_draft_store",
        lambda **_kwargs: drafts,
    )
    orchestrator = build_agent_workflow_orchestrator(
        execution_kind="recorded",
        settings=settings,
    )
    assert orchestrator.store is drafts

    monkeypatch.setattr(
        bootstrap,
        "build_workflow_access_store",
        lambda **_kwargs: access,
    )
    service = build_streamlit_ui_service(settings=settings)
    assert service.access_store is access
