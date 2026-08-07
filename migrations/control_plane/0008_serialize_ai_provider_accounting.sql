-- M27 additive serialization and audit hardening for provider accounting.
--
-- Migration 0007 is already deployed evidence and remains immutable.  This
-- migration validates the additional audit derivations, places the v7
-- implementations behind lock-ordering wrappers, and reasserts exact ACLs.

LOCK TABLE schemabridge_control.ai_provider_attempt_reservations
    IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE schemabridge_control.ai_provider_usage_audit
    IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM schemabridge_control.ai_provider_attempt_reservations
            AS reservation
        CROSS JOIN LATERAL (
            SELECT encode(
                sha256(
                    convert_to(
                        CASE
                            WHEN reservation.outcome_code = 'expired_crash'
                            THEN concat_ws(
                                '|',
                                'ai_attempt_settlement_v1',
                                reservation.reservation_id,
                                reservation.fencing_token::text,
                                reservation.outcome_code,
                                reservation.estimated_input_tokens::text,
                                reservation.estimated_output_tokens::text,
                                reservation.duration_ms::text
                            )
                            ELSE concat_ws(
                                '|',
                                'ai_attempt_settlement_v1',
                                reservation.reservation_id,
                                reservation.fencing_token::text,
                                reservation.outcome_code,
                                coalesce(
                                    reservation.observed_input_tokens::text,
                                    'unknown'
                                ),
                                coalesce(
                                    reservation.observed_output_tokens::text,
                                    'unknown'
                                ),
                                reservation.charged_input_tokens::text,
                                reservation.charged_output_tokens::text,
                                reservation.duration_ms::text
                            )
                        END,
                        'UTF8'
                    )
                ),
                'hex'
            )::char(64) AS settlement_fingerprint
        ) AS derived
        WHERE reservation.status IN ('settled', 'expired')
          AND (
                reservation.settlement_fingerprint
                    IS DISTINCT FROM derived.settlement_fingerprint
                OR (
                    reservation.outcome_code = 'expired_crash'
                    AND (
                        reservation.status <> 'expired'
                        OR reservation.observed_input_tokens IS NOT NULL
                        OR reservation.observed_output_tokens IS NOT NULL
                        OR reservation.duration_ms <> 0
                    )
                )
                OR (
                    reservation.outcome_code <> 'expired_crash'
                    AND reservation.status <> 'settled'
                )
                OR NOT EXISTS (
                SELECT 1
                FROM schemabridge_control.ai_provider_usage_audit AS audit
                WHERE audit.reservation_id = reservation.reservation_id
                  AND audit.audit_id = 'aia_' || encode(
                        sha256(
                            convert_to(
                                reservation.reservation_id
                                    || derived.settlement_fingerprint,
                                'UTF8'
                            )
                        ),
                        'hex'
                  )
                  AND audit.workspace_scope_digest = encode(
                        sha256(
                            convert_to(
                                'query_studio_scope_v1|'
                                    || reservation.workspace_id,
                                'UTF8'
                            )
                        ),
                        'hex'
                  )
                  AND audit.actor_digest = reservation.actor_digest
                  AND audit.request_id = reservation.request_id
                  AND audit.stage = reservation.stage
                  AND audit.attempt_number = reservation.attempt_number
                  AND audit.model_snapshot = reservation.model_snapshot
                  AND audit.endpoint_region = reservation.endpoint_region
                  AND audit.configuration_fingerprint
                        = reservation.configuration_fingerprint
                  AND audit.request_fingerprint
                        = reservation.request_fingerprint
                  AND audit.semantic_scope_fingerprint
                        = reservation.semantic_scope_fingerprint
                  AND audit.semantic_payload_fingerprint
                        = reservation.semantic_payload_fingerprint
                  AND audit.input_tokens
                        = reservation.charged_input_tokens
                  AND audit.output_tokens
                        = reservation.charged_output_tokens
                  AND audit.duration_ms = reservation.duration_ms
                  AND audit.outcome_code = reservation.outcome_code
                  AND audit.occurred_at = reservation.settled_at
                  AND audit.retain_until = reservation.settled_at
                        + make_interval(
                            secs => reservation.audit_retention_seconds
                        )
          )
          )
    ) THEN
        RAISE EXCEPTION
            'historical AI settlement audit derivation is incompatible with schema v8'
            USING ERRCODE = '23514';
    END IF;
END;
$$;

ALTER FUNCTION schemabridge_control.expire_ai_provider_attempts(
    varchar,
    integer
)
    RENAME TO expire_ai_provider_attempts_v7_core;

ALTER FUNCTION schemabridge_control.reserve_ai_provider_attempt(
    varchar,
    varchar,
    char,
    varchar,
    smallint,
    char,
    char,
    char,
    char,
    char,
    integer,
    integer,
    varchar
)
    RENAME TO reserve_ai_provider_attempt_v7_core;

ALTER FUNCTION schemabridge_control.settle_ai_provider_attempt(
    varchar,
    varchar,
    varchar,
    bigint,
    varchar,
    integer,
    integer,
    integer
)
    RENAME TO settle_ai_provider_attempt_v7_core;

ALTER FUNCTION schemabridge_control.expire_ai_provider_attempts_v7_core(
    varchar,
    integer
)
    SECURITY INVOKER;

ALTER FUNCTION schemabridge_control.reserve_ai_provider_attempt_v7_core(
    varchar,
    varchar,
    char,
    varchar,
    smallint,
    char,
    char,
    char,
    char,
    char,
    integer,
    integer,
    varchar
)
    SECURITY INVOKER;

ALTER FUNCTION schemabridge_control.settle_ai_provider_attempt_v7_core(
    varchar,
    varchar,
    varchar,
    bigint,
    varchar,
    integer,
    integer,
    integer
)
    SECURITY INVOKER;

CREATE FUNCTION schemabridge_control.expire_ai_provider_attempts(
    p_workspace_id varchar,
    p_limit integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    reserved_count integer := 0;
    expired_count integer;
    active_usage_day date;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'AI reservation expiry role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_limit NOT BETWEEN 1 AND 1000
    ) THEN
        RAISE EXCEPTION 'AI reservation expiry input is invalid'
            USING ERRCODE = '22023';
    END IF;

    -- Every provider transition uses reservation -> admission state -> daily
    -- usage.  Locking every active reservation is bounded by the tenant
    -- concurrency policy and prevents the v7 core from discovering a row
    -- after the accounting locks have already been acquired.
    PERFORM 1
    FROM schemabridge_control.ai_provider_attempt_reservations AS reservation
    WHERE reservation.workspace_id = p_workspace_id
      AND reservation.status = 'reserved'
    ORDER BY
        reservation.lease_expires_at,
        reservation.fencing_token,
        reservation.reservation_id
    FOR UPDATE;
    GET DIAGNOSTICS reserved_count = ROW_COUNT;

    IF reserved_count > 0 THEN
        PERFORM 1
        FROM schemabridge_control.ai_provider_admission_state AS state
        WHERE state.workspace_id = p_workspace_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'AI admission state is unavailable'
                USING ERRCODE = '55000';
        END IF;

        FOR active_usage_day IN
            SELECT DISTINCT (
                reservation.created_at AT TIME ZONE 'UTC'
            )::date
            FROM schemabridge_control.ai_provider_attempt_reservations
                AS reservation
            WHERE reservation.workspace_id = p_workspace_id
              AND reservation.status = 'reserved'
            ORDER BY 1
        LOOP
            PERFORM 1
            FROM schemabridge_control.ai_provider_daily_usage AS usage
            WHERE usage.workspace_id = p_workspace_id
              AND usage.usage_date = active_usage_day
            FOR UPDATE;
        END LOOP;
    END IF;

    SELECT schemabridge_control.expire_ai_provider_attempts_v7_core(
        p_workspace_id,
        p_limit
    )
    INTO expired_count;
    RETURN expired_count;
END;
$$;

CREATE FUNCTION schemabridge_control.reserve_ai_provider_attempt(
    p_workspace_id varchar,
    p_request_id varchar,
    p_actor_digest char,
    p_stage varchar,
    p_attempt_number smallint,
    p_idempotency_digest char,
    p_request_fingerprint char,
    p_semantic_scope_fingerprint char,
    p_semantic_payload_fingerprint char,
    p_configuration_fingerprint char,
    p_estimated_input_tokens integer,
    p_estimated_output_tokens integer,
    p_lease_capability varchar
)
RETURNS TABLE (
    admission_outcome varchar(32),
    request_replayed boolean,
    reservation_id varchar(80),
    status varchar(16),
    fencing_token bigint,
    lease_expires_at timestamptz,
    policy_version bigint,
    model_snapshot varchar(80),
    endpoint_region varchar(16),
    configuration_fingerprint char(64)
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    policy_record schemabridge_control.tenant_ai_policies%ROWTYPE;
    existing_reservation_id varchar(80);
    lock_observed_at timestamptz;
    usage_day date;
    target_window_started_at timestamptz;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'AI reservation role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_request_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR p_actor_digest !~ '^[0-9a-f]{64}$'
        OR p_stage NOT IN ('expansion', 'interpretation')
        OR p_attempt_number NOT BETWEEN 1 AND 2
        OR p_idempotency_digest !~ '^[0-9a-f]{64}$'
        OR p_request_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_semantic_scope_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_semantic_payload_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_configuration_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_estimated_input_tokens NOT BETWEEN 1 AND 1000000
        OR p_estimated_output_tokens NOT BETWEEN 1 AND 1000000
        OR p_lease_capability IS NULL
        OR length(p_lease_capability) NOT BETWEEN 32 AND 256
        OR octet_length(p_lease_capability) > 1024
    ) THEN
        RAISE EXCEPTION 'AI reservation input is invalid'
            USING ERRCODE = '22023';
    END IF;

    -- Policy is the first mutable row in admission.  Existing idempotency
    -- rows follow it; settlement and expiry never wait on policy, so this
    -- ordering cannot form a reservation/policy cycle.
    SELECT *
    INTO policy_record
    FROM schemabridge_control.tenant_ai_policies AS policy
    WHERE policy.workspace_id = p_workspace_id
    FOR UPDATE;

    SELECT reservation.reservation_id
    INTO existing_reservation_id
    FROM schemabridge_control.ai_provider_attempt_reservations AS reservation
    WHERE reservation.workspace_id = p_workspace_id
      AND reservation.idempotency_digest = p_idempotency_digest
    FOR UPDATE;

    IF (
        existing_reservation_id IS NULL
        AND policy_record.workspace_id IS NOT NULL
        AND policy_record.external_ai_enabled
        AND policy_record.provider_governance_accepted
        AND policy_record.configuration_fingerprint
            = p_configuration_fingerprint
    ) THEN
        -- Expiry locks active reservations before the two accounting rows.
        PERFORM schemabridge_control.expire_ai_provider_attempts(
            p_workspace_id,
            1000
        );

        PERFORM 1
        FROM schemabridge_control.ai_provider_admission_state AS state
        WHERE state.workspace_id = p_workspace_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'AI admission state is unavailable'
                USING ERRCODE = '55000';
        END IF;

        -- The core timestamps request-window, usage-day, and lease semantics
        -- at invocation.  Invoke it only after state and any existing current
        -- daily row are locked, so waiting cannot create an expired lease.
        lock_observed_at := clock_timestamp();
        usage_day := (lock_observed_at AT TIME ZONE 'UTC')::date;
        target_window_started_at := date_trunc('minute', lock_observed_at);
        PERFORM 1
        FROM schemabridge_control.ai_provider_daily_usage AS usage
        WHERE usage.workspace_id = p_workspace_id
          AND usage.usage_date = usage_day
        FOR UPDATE;

        -- A same-workspace reserve is already serialized by policy/state.
        -- Locking an existing actor bucket also prevents maintenance from
        -- delaying the timestamping core after its lease clock is captured.
        PERFORM 1
        FROM schemabridge_control.ai_provider_request_windows AS request_window
        WHERE request_window.workspace_id = p_workspace_id
          AND request_window.actor_digest = p_actor_digest
          AND request_window.window_started_at = target_window_started_at
        FOR UPDATE;
    END IF;

    RETURN QUERY
    SELECT *
    FROM schemabridge_control.reserve_ai_provider_attempt_v7_core(
        p_workspace_id,
        p_request_id,
        p_actor_digest,
        p_stage,
        p_attempt_number,
        p_idempotency_digest,
        p_request_fingerprint,
        p_semantic_scope_fingerprint,
        p_semantic_payload_fingerprint,
        p_configuration_fingerprint,
        p_estimated_input_tokens,
        p_estimated_output_tokens,
        p_lease_capability
    );
END;
$$;

CREATE FUNCTION schemabridge_control.settle_ai_provider_attempt(
    p_workspace_id varchar,
    p_reservation_id varchar,
    p_lease_capability varchar,
    p_fencing_token bigint,
    p_outcome_code varchar,
    p_observed_input_tokens integer,
    p_observed_output_tokens integer,
    p_duration_ms integer
)
RETURNS TABLE (
    reservation_id varchar(80),
    settlement_replayed boolean,
    status varchar(16),
    outcome_code varchar(32),
    charged_input_tokens integer,
    charged_output_tokens integer,
    settled_at timestamptz,
    audit_id varchar(80)
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    reservation_record
        schemabridge_control.ai_provider_attempt_reservations%ROWTYPE;
    locked_at timestamptz;
    provided_capability_digest char(64);
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'AI settlement role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_reservation_id !~ '^air_[0-9a-f]{64}$'
        OR p_lease_capability IS NULL
        OR length(p_lease_capability) NOT BETWEEN 32 AND 256
        OR octet_length(p_lease_capability) > 1024
        OR p_fencing_token < 1
        OR p_outcome_code NOT IN (
            'succeeded',
            'timeout',
            'rate_limited',
            'provider_unavailable',
            'refusal',
            'missing_output',
            'invalid_output'
        )
        OR (
            (p_observed_input_tokens IS NULL)
                <> (p_observed_output_tokens IS NULL)
        )
        OR (
            p_outcome_code = 'succeeded'
            AND (
                p_observed_input_tokens IS NULL
                OR p_observed_input_tokens < 1
                OR p_observed_output_tokens < 1
            )
        )
        OR (
            p_observed_input_tokens IS NOT NULL
            AND p_observed_input_tokens NOT BETWEEN 0 AND 1000000000
        )
        OR (
            p_observed_output_tokens IS NOT NULL
            AND p_observed_output_tokens NOT BETWEEN 0 AND 1000000000
        )
        OR p_duration_ms NOT BETWEEN 0 AND 3600000
    ) THEN
        RAISE EXCEPTION 'AI settlement input is invalid'
            USING ERRCODE = '22023';
    END IF;
    provided_capability_digest := encode(
        sha256(convert_to(p_lease_capability, 'UTF8')),
        'hex'
    )::char(64);

    SELECT *
    INTO reservation_record
    FROM schemabridge_control.ai_provider_attempt_reservations AS reservation
    WHERE reservation.workspace_id = p_workspace_id
      AND reservation.reservation_id = p_reservation_id
    FOR UPDATE;

    -- This clock is intentionally captured after FOR UPDATE returns.  A
    -- caller that waited beyond lease expiry cannot settle using a stale
    -- timestamp captured before the wait.
    locked_at := clock_timestamp();
    IF (
        reservation_record.reservation_id IS NOT NULL
        AND reservation_record.status = 'reserved'
    ) THEN
        IF (
            reservation_record.capability_digest
                <> provided_capability_digest
            OR reservation_record.fencing_token <> p_fencing_token
        ) THEN
            RAISE EXCEPTION 'AI reservation ownership is stale'
                USING ERRCODE = '55000';
        END IF;
        IF (
            p_outcome_code = 'succeeded'
            AND (
                p_observed_input_tokens
                    > reservation_record.estimated_input_tokens
                OR p_observed_output_tokens
                    > reservation_record.estimated_output_tokens
            )
        ) THEN
            RAISE EXCEPTION 'AI settlement usage exceeds its reservation'
                USING ERRCODE = '22023';
        END IF;
        IF (
            reservation_record.lease_expires_at IS NULL
            OR reservation_record.lease_expires_at <= locked_at
        ) THEN
            RAISE EXCEPTION 'AI reservation lease expired'
                USING ERRCODE = '55000';
        END IF;

        PERFORM 1
        FROM schemabridge_control.ai_provider_admission_state AS state
        WHERE state.workspace_id = reservation_record.workspace_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'AI admission state is unavailable'
                USING ERRCODE = '55000';
        END IF;

        PERFORM 1
        FROM schemabridge_control.ai_provider_daily_usage AS usage
        WHERE usage.workspace_id = reservation_record.workspace_id
          AND usage.usage_date = (
                reservation_record.created_at AT TIME ZONE 'UTC'
          )::date
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'AI reservation accounting is inconsistent'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    RETURN QUERY
    SELECT *
    FROM schemabridge_control.settle_ai_provider_attempt_v7_core(
        p_workspace_id,
        p_reservation_id,
        p_lease_capability,
        p_fencing_token,
        p_outcome_code,
        p_observed_input_tokens,
        p_observed_output_tokens,
        p_duration_ms
    );
END;
$$;

ALTER FUNCTION schemabridge_control.expire_ai_provider_attempts(
    varchar,
    integer
)
    OWNER TO schemabridge_migrator;

ALTER FUNCTION schemabridge_control.reserve_ai_provider_attempt(
    varchar,
    varchar,
    char,
    varchar,
    smallint,
    char,
    char,
    char,
    char,
    char,
    integer,
    integer,
    varchar
)
    OWNER TO schemabridge_migrator;

ALTER FUNCTION schemabridge_control.settle_ai_provider_attempt(
    varchar,
    varchar,
    varchar,
    bigint,
    varchar,
    integer,
    integer,
    integer
)
    OWNER TO schemabridge_migrator;

REVOKE ALL ON FUNCTION
    schemabridge_control.expire_ai_provider_attempts_v7_core(
        varchar,
        integer
    ),
    schemabridge_control.reserve_ai_provider_attempt_v7_core(
        varchar,
        varchar,
        char,
        varchar,
        smallint,
        char,
        char,
        char,
        char,
        char,
        integer,
        integer,
        varchar
    ),
    schemabridge_control.settle_ai_provider_attempt_v7_core(
        varchar,
        varchar,
        varchar,
        bigint,
        varchar,
        integer,
        integer,
        integer
    )
    FROM PUBLIC,
         schemabridge_runtime,
         schemabridge_api,
         schemabridge_worker,
         schemabridge_catalog,
         schemabridge_reconciler;

REVOKE ALL ON FUNCTION
    schemabridge_control.expire_ai_provider_attempts(
        varchar,
        integer
    ),
    schemabridge_control.reserve_ai_provider_attempt(
        varchar,
        varchar,
        char,
        varchar,
        smallint,
        char,
        char,
        char,
        char,
        char,
        integer,
        integer,
        varchar
    ),
    schemabridge_control.settle_ai_provider_attempt(
        varchar,
        varchar,
        varchar,
        bigint,
        varchar,
        integer,
        integer,
        integer
    )
    FROM PUBLIC,
         schemabridge_runtime,
         schemabridge_api,
         schemabridge_worker,
         schemabridge_catalog,
         schemabridge_reconciler;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.expire_ai_provider_attempts(
        varchar,
        integer
    ),
    schemabridge_control.reserve_ai_provider_attempt(
        varchar,
        varchar,
        char,
        varchar,
        smallint,
        char,
        char,
        char,
        char,
        char,
        integer,
        integer,
        varchar
    ),
    schemabridge_control.settle_ai_provider_attempt(
        varchar,
        varchar,
        varchar,
        bigint,
        varchar,
        integer,
        integer,
        integer
    )
    TO schemabridge_runtime;
