from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from tests.m28_browser_support import public_m28_state

from schemabridge.entrypoints.streamlit.connector_acceptance import (
    M28_HOSTILE_METADATA,
    M28BrowserAcceptanceState,
    M28BrowserScenario,
    build_m28_browser_view,
    is_m28_private_environment_key,
)


def test_public_browser_state_round_trips_the_complete_closed_matrix() -> None:
    state = public_m28_state()

    restored = M28BrowserAcceptanceState.from_json(state.to_json())

    assert restored == state
    assert tuple(item.scenario for item in restored.scenarios) == tuple(M28BrowserScenario)
    assert restored.hostile_metadata == M28_HOSTILE_METADATA
    assert restored.source_pair_count == 2
    assert restored.real_preflight_calls == 4
    assert restored.real_preview_calls == 2


def test_only_accepted_tenant_scenarios_can_reveal_the_execution_checkpoint() -> None:
    state = public_m28_state()

    for scenario in M28BrowserScenario:
        pending = build_m28_browser_view(state, scenario, executed=False)
        assert pending.query is not None
        expected = scenario in {
            M28BrowserScenario.TENANT_A_ACCEPTED,
            M28BrowserScenario.TENANT_B_ACCEPTED,
        }
        assert pending.query.can_execute is expected
        assert (pending.query.result is not None) is False
        if expected:
            executed = build_m28_browser_view(state, scenario, executed=True)
            assert executed.query is not None
            assert executed.query.can_execute is False
            assert executed.query.result is not None
        else:
            assert pending.query.execution_blockers


def test_accepted_views_are_distinct_but_share_only_the_public_connection_id() -> None:
    state = public_m28_state()
    alpha = build_m28_browser_view(
        state,
        M28BrowserScenario.TENANT_A_ACCEPTED,
        executed=True,
    ).query
    beta = build_m28_browser_view(
        state,
        M28BrowserScenario.TENANT_B_ACCEPTED,
        executed=True,
    ).query

    assert alpha is not None and beta is not None
    assert alpha.execution_target is not None and beta.execution_target is not None
    assert alpha.execution_target.connection_id == beta.execution_target.connection_id
    assert alpha.execution_target.route_revision != beta.execution_target.route_revision
    assert alpha.execution_target.target_fingerprint != beta.execution_target.target_fingerprint
    assert alpha.cost_budget != beta.cost_budget
    assert alpha.result is not None and beta.result is not None
    assert alpha.result.database_user != beta.result.database_user
    assert alpha.result.rows == ((2,),)
    assert beta.result.rows == ((3,),)


def test_public_state_has_no_private_route_topology_or_query_payload_surface() -> None:
    state = public_m28_state()
    raw = state.to_json().decode("ascii")
    payload = json.loads(raw)
    scenario_keys = {key for scenario in payload["scenarios"] for key in scenario}

    assert "database_name" not in scenario_keys
    assert "server_address" not in scenario_keys
    assert "source_identity_fingerprint" not in scenario_keys
    assert "secret_binding" not in scenario_keys
    assert "dsn" not in scenario_keys
    assert "sql" not in scenario_keys
    assert "parameters" not in scenario_keys
    assert "raw_plan" not in scenario_keys
    for forbidden in (
        "postgresql://",
        "hidden_canary",
        "m28_canary_",
        "password",
        "127.0.0.1",
        "localhost",
    ):
        assert forbidden not in raw.casefold()


def test_json_boundary_rejects_duplicate_keys_unknown_fields_and_tampering() -> None:
    state = public_m28_state()
    payload = state.model_dump(mode="json")

    payload["unexpected"] = True
    with pytest.raises(ValueError, match="invalid"):
        M28BrowserAcceptanceState.from_json(
            json.dumps(payload, separators=(",", ":")).encode("ascii")
        )

    with pytest.raises(ValueError, match="invalid"):
        M28BrowserAcceptanceState.from_json(b'{"schema_version":1,"schema_version":1}')

    tampered = state.model_dump(mode="json")
    tampered["real_preflight_calls"] = 5
    with pytest.raises(ValueError, match="invalid"):
        M28BrowserAcceptanceState.from_json(
            json.dumps(tampered, separators=(",", ":")).encode("ascii")
        )


def test_scenario_model_rejects_execution_for_a_closed_route() -> None:
    state = public_m28_state()
    disabled = state.for_scenario(M28BrowserScenario.ROUTE_DISABLED)
    payload = disabled.model_dump(mode="json")
    payload["execution_allowed"] = True

    with pytest.raises(ValidationError, match="cannot expose execution"):
        type(disabled).model_validate(payload)


@pytest.mark.parametrize("forged", ([["2"]], [[2.0]], [[True]]))
def test_result_rows_reject_coercible_non_integer_values(forged: list[list[object]]) -> None:
    state = public_m28_state()
    result = state.for_scenario(M28BrowserScenario.TENANT_A_ACCEPTED).result
    assert result is not None
    payload = result.model_dump(mode="json")
    payload["rows"] = forged

    with pytest.raises(ValidationError, match="exact integers"):
        type(result).model_validate(payload)


@pytest.mark.parametrize(
    "name",
    (
        "DATABASE_URL",
        "FUTURE_DATABASE_URL",
        "CONTROL_PROFILE_DSN",
        "WAREHOUSE_PASSWORD",
        "NEW_TOKEN",
        "OPENAI_API_KEY",
        "CONNECTOR_SECRET_DIR",
        "REMOTE_CREDENTIAL_FILE",
    ),
)
def test_private_environment_key_detection_is_generic(name: str) -> None:
    assert is_m28_private_environment_key(name)


@pytest.mark.parametrize(
    "name",
    (
        "PATH",
        "LANG",
        "SCHEMABRIDGE_M28_BROWSER_STATE_FILE",
        "SCHEMABRIDGE_M28_BROWSER_SCENARIO",
        "SCHEMABRIDGE_RELEASE_REF",
    ),
)
def test_public_browser_environment_key_is_allowed(name: str) -> None:
    assert not is_m28_private_environment_key(name)
