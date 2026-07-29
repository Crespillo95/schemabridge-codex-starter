from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest


@pytest.fixture(autouse=True)
def reset_streamlit_secret_cache() -> Iterator[None]:
    """Keep AppTest cases isolated from Streamlit's process-global secret cache."""

    st.secrets._reset()
    yield
    st.secrets._reset()


@pytest.mark.acceptance
def test_streamlit_north_star_is_governed_and_reachable_in_three_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("SCHEMABRIDGE_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SCHEMABRIDGE_TEST_DATABASE_URL is required for Streamlit acceptance")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "live")
    monkeypatch.setenv("SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS", "true")
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "ui-acceptance.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"

    app = AppTest.from_file(str(app_path), default_timeout=20).run()
    assert not app.exception
    assert not app.text_input
    assert not app.selectbox

    # Primary interaction 1: deterministic scenario load.
    app.button(key="load-demo").click().run()
    assert not app.exception
    assert app.radio(key="navigation").value == "Query Studio"
    assert app.button(key="confirm-intent")
    assert "approve-execution" not in {button.key for button in app.button}

    # Primary interaction 2: exact typed intent confirmation.
    app.button(key="confirm-intent").click().run()
    assert not app.exception
    assert app.button(key="approve-execution")

    # Primary interaction 3: approval bound to the independently validated plan.
    app.button(key="approve-execution").click().run()
    assert not app.exception

    frames = [item.value for item in app.dataframe]
    frame_columns = [tuple(str(column) for column in frame.columns) for frame in frames]
    result = next(
        (
            frame
            for frame in frames
            if set(frame.columns) == {"registration_date", "secondary_holder_customers"}
        ),
        None,
    )
    assert result is not None, {
        "frame_columns": frame_columns,
        "errors": [str(item.value) for item in app.error],
        "warnings": [str(item.value) for item in app.warning],
    }
    assert list(result["secondary_holder_customers"]) == [2, 1, 1]
    rejections = next(
        frame for frame in frames if set(frame.columns) == {"record", "code", "reason"}
    )
    assert list(rejections["code"]) == [
        "non_integral_identifier",
        "non_finite_identifier",
        "null_join_key",
    ]
    assert "127.5" in rejections.iloc[0]["reason"]
    assert "NaN" in rejections.iloc[1]["reason"]
    assert "NULL" in rejections.iloc[2]["reason"]

    visible = " ".join(
        str(item.value)
        for item in (
            *app.markdown,
            *app.caption,
            *app.info,
            *app.success,
            *app.warning,
            *app.error,
        )
    )
    assert "crm.customers" in visible
    assert "customer_to_account_holder" in visible
    assert "count_distinct → count_distinct" in visible
    assert "Traceback" not in visible
    assert "postgresql://" not in visible
    assert "OPENAI_API_KEY" not in visible


@pytest.mark.acceptance
def test_deployed_recorded_streamlit_path_needs_no_service_or_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DATAHUB_GMS_TOKEN", raising=False)
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "recorded")
    monkeypatch.setenv("SCHEMABRIDGE_RELEASE_REF", "m17-acceptance-release")
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "deployed.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"

    app = AppTest.from_file(str(app_path), default_timeout=20).run()
    assert not app.exception
    assert not app.selectbox
    assert not app.text_input
    overview_captions = " ".join(str(item.value) for item in app.caption)
    assert "Registry synthetic_enterprise" in overview_captions
    assert (
        "fingerprint 0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966"
        in overview_captions
    )

    app.button(key="load-demo").click().run()
    app.button(key="confirm-intent").click().run()
    app.button(key="approve-execution").click().run()
    assert not app.exception

    result = next(
        frame.value
        for frame in app.dataframe
        if set(frame.value.columns) == {"registration_date", "secondary_holder_customers"}
    )
    assert list(result["secondary_holder_customers"]) == [2, 1, 1]
    visible = " ".join(
        str(item.value)
        for item in (
            *app.markdown,
            *app.caption,
            *app.info,
            *app.success,
            *app.warning,
            *app.error,
        )
    )
    assert "Recorded synthetic PostgreSQL observation" in visible
    assert "m17-acceptance-release" in visible
    assert any(
        metric.label == "Reader" and metric.value == "schemabridge_reader (recorded observation)"
        for metric in app.metric
    )
    assert "Live DataHub" not in visible or "Recorded catalog" in visible
    assert "postgresql://" not in visible

    app.button(key="reset-demo").click().run()
    assert not app.exception
    assert app.button(key="confirm-intent")
    assert "approve-execution" not in {button.key for button in app.button}


@pytest.mark.acceptance
def test_server_configured_live_execution_fails_without_recorded_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "live")
    monkeypatch.setenv("SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS", "true")
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "live-failure.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"

    app = AppTest.from_file(str(app_path), default_timeout=20).run()
    assert not app.selectbox
    app.button(key="load-demo").click().run()

    assert not app.exception
    assert any("integration_configuration_unavailable" in str(item.value) for item in app.error)
    assert not any(
        set(frame.value.columns) == {"registration_date", "secondary_holder_customers"}
        for frame in app.dataframe
    )


@pytest.mark.acceptance
def test_local_auditor_has_read_only_ui_and_no_spoofable_actor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "recorded")
    monkeypatch.setenv("SCHEMABRIDGE_LOCAL_ROLES", '["auditor"]')
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "auditor.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"

    app = AppTest.from_file(str(app_path), default_timeout=20).run()

    assert not app.exception
    assert app.button(key="load-demo").disabled
    assert "start-request" not in {button.key for button in app.button}
    assert not app.text_input
    assert not app.selectbox
    visible = " ".join(str(item.value) for item in (*app.caption, *app.markdown))
    assert "Roles · auditor" in visible
    assert "do not allow workflow creation" in visible


@pytest.mark.acceptance
def test_production_anonymous_session_stops_at_oidc_login_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_dir = tmp_path / ".streamlit"
    secrets_dir.mkdir()
    (secrets_dir / "secrets.toml").write_text(
        """
[auth]
redirect_uri = "https://app.example.test/oauth2callback"
cookie_secret = "6KzTEe9Wt7JfM2xP8vAc4nQ1sRg5yUhB" # gitleaks:allow -- synthetic fixture

[auth.corporate-oidc]
client_id = "schemabridge"
client_secret = "G7nYw4rQ9tVz6mXp2sKa8cHd" # gitleaks:allow -- synthetic fixture
server_metadata_url = "https://identity.example.test/.well-known/openid-configuration"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHEMABRIDGE_ENVIRONMENT", "production")
    monkeypatch.setenv("SCHEMABRIDGE_AUTH_MODE", "oidc")
    monkeypatch.setenv("SCHEMABRIDGE_PUBLICATION_MODE", "disabled")
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "disabled")
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_AUDIENCE", "schemabridge")
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_PROVIDER", "corporate-oidc")
    monkeypatch.setenv(
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS",
        '{"schema-analysts":["analyst"]}',
    )
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_ALLOWED_TENANTS", '["tenant-a"]')
    monkeypatch.setenv(
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY",
        "acceptance-pseudonymization-key-at-least-32-bytes",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONTROL_DATABASE_URL",
        "postgresql://control_runtime:control_password@control.example.test/control"
        "?sslmode=verify-full",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY",
        "acceptance-control-audit-signing-key-with-diversity",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY",
        "acceptance-identity-migration-key-with-diversity",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY",
        "acceptance-query-studio-signing-key-with-diversity",
    )
    trust_root = tmp_path / "trust"
    trust_root.mkdir()
    ca_bundle = trust_root / "ca.crt"
    ca_bundle.write_text("synthetic test trust anchor", encoding="utf-8")
    ca_bundle.chmod(0o600)
    identity_root = tmp_path / "identity"
    identity_root.mkdir()
    token_file = identity_root / "token"
    token_file.write_text("synthetic projected identity", encoding="utf-8")
    token_file.chmod(0o400)
    monkeypatch.setenv("SCHEMABRIDGE_CONNECTOR_SECRET_MODE", "remote")
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL",
        "https://secrets.example.test",
    )
    monkeypatch.setenv("SCHEMABRIDGE_CONNECTOR_SECRET_ROLE", "schemabridge-preflight")
    monkeypatch.setenv("SCHEMABRIDGE_CONNECTOR_SECRET_KV_MOUNT", "tenant-connectors")
    monkeypatch.setenv("SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY", "preflight")
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONNECTOR_SECRET_CA_BUNDLE",
        str(ca_bundle.resolve()),
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_TOKEN_FILE",
        str(token_file.resolve()),
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_ROOT",
        str(identity_root.resolve()),
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_AUDIENCE",
        "schemabridge-secret-manager",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE",
        "schemabridge-registry-reader",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF",
        "registry.reader.primary",
    )
    monkeypatch.setenv("SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION", "17")
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "production.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"

    app = AppTest.from_file(str(app_path), default_timeout=20).run()

    assert not app.exception
    assert app.button(key="login")
    assert "load-demo" not in {button.key for button in app.button}
    assert not app.radio
    visible = " ".join(str(item.value) for item in (*app.caption, *app.markdown))
    assert "OIDC authentication is required" in visible
    assert "crm.customers" not in visible


@pytest.mark.acceptance
def test_production_missing_streamlit_auth_secrets_fails_closed_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHEMABRIDGE_ENVIRONMENT", "production")
    monkeypatch.setenv("SCHEMABRIDGE_AUTH_MODE", "oidc")
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_AUDIENCE", "schemabridge")
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_PROVIDER", "corporate-oidc")
    monkeypatch.setenv(
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS",
        '{"schema-analysts":["analyst"]}',
    )
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_ALLOWED_TENANTS", '["tenant-a"]')
    monkeypatch.setenv(
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY",
        "acceptance-pseudonymization-key-at-least-32-bytes",
    )
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "production.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"

    app = AppTest.from_file(str(app_path), default_timeout=20).run()

    assert not app.exception
    visible = " ".join(str(item.value) for item in (*app.error, *app.caption, *app.markdown))
    assert "runtime_configuration_invalid" in visible
    assert "Traceback" not in visible
    assert "load-demo" not in {button.key for button in app.button}


@pytest.mark.acceptance
def test_invalid_runtime_configuration_never_renders_rejected_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_marker = "must-not-render-runtime-value"
    monkeypatch.setenv("SCHEMABRIDGE_ENVIRONMENT", "production")
    monkeypatch.setenv("SCHEMABRIDGE_AUTH_MODE", "local-demo")
    monkeypatch.setenv("OPENAI_API_KEY", sensitive_marker)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://user:{sensitive_marker}@host/database")
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "invalid.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"

    app = AppTest.from_file(str(app_path), default_timeout=20).run()

    assert not app.exception
    visible = " ".join(str(item.value) for item in (*app.error, *app.caption, *app.markdown))
    assert "runtime_configuration_invalid" in visible
    assert sensitive_marker not in visible
    assert "input_value" not in visible
    assert "load-demo" not in {button.key for button in app.button}
