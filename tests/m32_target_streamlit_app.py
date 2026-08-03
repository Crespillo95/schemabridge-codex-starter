"""Synthetic managed-target Streamlit scenario for M32 acceptance tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import streamlit as st
from tests.m32_target_support import (
    MutableTargetResolver,
    current_semantic_gate,
    execution_target,
    target_bound_registry,
)

from schemabridge.bootstrap import build_natural_sql_runtime
from schemabridge.config import Settings
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.entrypoints.streamlit.natural_sql import render_copyable_natural_sql

_RESOLVER_KEY = "_m32_target_streamlit_resolver"

registry = target_bound_registry()
scope = registry.load().scope
if _RESOLVER_KEY not in st.session_state:
    st.session_state[_RESOLVER_KEY] = MutableTargetResolver(
        execution_target(workspace_id=scope.workspace_id)
    )
resolver = st.session_state[_RESOLVER_KEY]

if st.button("Rotar destino sintético", key="rotate-synthetic-target"):
    resolver.target = execution_target(
        workspace_id=scope.workspace_id,
        route_revision=2,
        marker="streamlit-rotated",
    )

now = datetime.now(UTC)
principal = AuthenticatedPrincipal(
    actor_id="m32-target-streamlit-analyst",
    workspace_id=scope.workspace_id,
    roles=frozenset({IdentityRole.ANALYST}),
    authentication_method=AuthenticationMethod.LOCAL_DEMO,
    authenticated_at=now,
    expires_at=now + timedelta(hours=1),
)
settings = Settings.model_validate(
    {
        "_env_file": None,
        "OPENAI_API_KEY": None,
        "DATABASE_URL": None,
        "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE": "fake",
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": (
            "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        ),
        "SCHEMABRIDGE_MAX_QUERY_TABLES": 3,
        "SCHEMABRIDGE_MAX_QUERY_ROWS": 100,
        "SCHEMABRIDGE_STATEMENT_TIMEOUT_MS": 5_000,
    }
)
runtime = build_natural_sql_runtime(
    principal=principal,
    settings=settings,
    registry=registry,
    target_resolver=resolver,
    semantic_gate=current_semantic_gate(),
)
render_copyable_natural_sql(runtime)
