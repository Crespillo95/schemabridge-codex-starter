-- M27 additive hardening for successful provider-usage settlement.
--
-- Historical rows are evidence.  The migration locks both affected relations,
-- validates them, and aborts instead of repairing or rewriting incompatible
-- history.

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
        WHERE reservation.status IN ('settled', 'expired')
          AND (
                (
                    reservation.outcome_code = 'succeeded'
                    AND (
                        reservation.status <> 'settled'
                        OR reservation.observed_input_tokens IS NULL
                        OR reservation.observed_output_tokens IS NULL
                        OR reservation.observed_input_tokens NOT BETWEEN
                            1 AND reservation.estimated_input_tokens
                        OR reservation.observed_output_tokens NOT BETWEEN
                            1 AND reservation.estimated_output_tokens
                        OR reservation.charged_input_tokens
                            IS DISTINCT FROM
                                reservation.observed_input_tokens
                        OR reservation.charged_output_tokens
                            IS DISTINCT FROM
                                reservation.observed_output_tokens
                    )
                )
                OR
                (
                    reservation.outcome_code <> 'succeeded'
                    AND (
                        reservation.charged_input_tokens
                            IS DISTINCT FROM
                                reservation.estimated_input_tokens
                        OR reservation.charged_output_tokens
                            IS DISTINCT FROM
                                reservation.estimated_output_tokens
                    )
                )
          )
    ) THEN
        RAISE EXCEPTION
            'historical AI reservation usage is incompatible with schema v7'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM schemabridge_control.ai_provider_usage_audit AS audit
        WHERE audit.input_tokens < 1
           OR audit.output_tokens < 1
    ) THEN
        RAISE EXCEPTION
            'historical AI usage audit is incompatible with schema v7'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM schemabridge_control.ai_provider_attempt_reservations
            AS reservation
        WHERE reservation.status IN ('settled', 'expired')
          AND NOT EXISTS (
                SELECT 1
                FROM schemabridge_control.ai_provider_usage_audit AS audit
                WHERE audit.reservation_id = reservation.reservation_id
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
          )
    ) THEN
        RAISE EXCEPTION
            'historical AI settlement audit is inconsistent with its reservation'
            USING ERRCODE = '23514';
    END IF;
END;
$$;

ALTER TABLE schemabridge_control.ai_provider_attempt_reservations
    ADD CONSTRAINT ai_attempt_terminal_usage_accounting_v7
    CHECK (
        status = 'reserved'
        OR
        (
            outcome_code = 'succeeded'
            AND status = 'settled'
            AND observed_input_tokens IS NOT NULL
            AND observed_output_tokens IS NOT NULL
            AND observed_input_tokens BETWEEN 1 AND estimated_input_tokens
            AND observed_output_tokens BETWEEN 1 AND estimated_output_tokens
            AND charged_input_tokens = observed_input_tokens
            AND charged_output_tokens = observed_output_tokens
        )
        OR
        (
            outcome_code <> 'succeeded'
            AND charged_input_tokens = estimated_input_tokens
            AND charged_output_tokens = estimated_output_tokens
        )
    );

ALTER TABLE schemabridge_control.ai_provider_usage_audit
    ADD CONSTRAINT ai_provider_usage_audit_positive_charges_v7
    CHECK (
        input_tokens BETWEEN 1 AND 1000000000
        AND output_tokens BETWEEN 1 AND 1000000000
    );

CREATE OR REPLACE FUNCTION
    schemabridge_control.settle_ai_provider_attempt(
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
    observed_at timestamptz := clock_timestamp();
    provided_capability_digest char(64);
    charge_input integer;
    charge_output integer;
    generated_settlement_fingerprint char(64);
    generated_audit_id varchar(80);
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
    IF reservation_record.reservation_id IS NULL THEN
        RAISE EXCEPTION 'AI reservation is unavailable'
            USING ERRCODE = '02000';
    END IF;
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

    IF p_outcome_code = 'succeeded' THEN
        charge_input := p_observed_input_tokens;
        charge_output := p_observed_output_tokens;
    ELSE
        charge_input := reservation_record.estimated_input_tokens;
        charge_output := reservation_record.estimated_output_tokens;
    END IF;
    generated_settlement_fingerprint := encode(
        sha256(
            convert_to(
                concat_ws(
                    '|',
                    'ai_attempt_settlement_v1',
                    reservation_record.reservation_id,
                    p_fencing_token::text,
                    p_outcome_code,
                    coalesce(p_observed_input_tokens::text, 'unknown'),
                    coalesce(p_observed_output_tokens::text, 'unknown'),
                    charge_input::text,
                    charge_output::text,
                    p_duration_ms::text
                ),
                'UTF8'
            )
        ),
        'hex'
    )::char(64);
    generated_audit_id := 'aia_' || encode(
        sha256(
            convert_to(
                reservation_record.reservation_id
                    || generated_settlement_fingerprint,
                'UTF8'
            )
        ),
        'hex'
    );

    IF reservation_record.status <> 'reserved' THEN
        IF (
            reservation_record.status = 'settled'
            AND reservation_record.settlement_fingerprint
                = generated_settlement_fingerprint
        ) THEN
            RETURN QUERY
            SELECT
                reservation_record.reservation_id,
                true,
                reservation_record.status,
                reservation_record.outcome_code,
                reservation_record.charged_input_tokens,
                reservation_record.charged_output_tokens,
                reservation_record.settled_at,
                generated_audit_id;
            RETURN;
        END IF;
        RAISE EXCEPTION 'AI reservation is no longer current'
            USING ERRCODE = '55000';
    END IF;
    IF (
        reservation_record.lease_expires_at IS NULL
        OR reservation_record.lease_expires_at <= observed_at
    ) THEN
        RAISE EXCEPTION 'AI reservation lease expired'
            USING ERRCODE = '55000';
    END IF;

    UPDATE schemabridge_control.ai_provider_attempt_reservations
        AS current_reservation
    SET status = 'settled',
        lease_expires_at = NULL,
        outcome_code = p_outcome_code,
        observed_input_tokens = p_observed_input_tokens,
        observed_output_tokens = p_observed_output_tokens,
        charged_input_tokens = charge_input,
        charged_output_tokens = charge_output,
        duration_ms = p_duration_ms,
        settlement_fingerprint = generated_settlement_fingerprint,
        settled_at = observed_at
    WHERE current_reservation.reservation_id
            = reservation_record.reservation_id
      AND current_reservation.status = 'reserved'
      AND current_reservation.fencing_token = p_fencing_token;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'AI reservation transition lost ownership'
            USING ERRCODE = '55000';
    END IF;

    UPDATE schemabridge_control.ai_provider_daily_usage AS usage
    SET reserved_input_tokens = usage.reserved_input_tokens
            - reservation_record.estimated_input_tokens,
        reserved_output_tokens = usage.reserved_output_tokens
            - reservation_record.estimated_output_tokens,
        charged_input_tokens = usage.charged_input_tokens + charge_input,
        charged_output_tokens = usage.charged_output_tokens + charge_output,
        updated_at = observed_at
    WHERE usage.workspace_id = reservation_record.workspace_id
      AND usage.usage_date = (
            reservation_record.created_at AT TIME ZONE 'UTC'
        )::date
      AND usage.reserved_input_tokens
            >= reservation_record.estimated_input_tokens
      AND usage.reserved_output_tokens
            >= reservation_record.estimated_output_tokens;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'AI reservation accounting is inconsistent'
            USING ERRCODE = '55000';
    END IF;

    UPDATE schemabridge_control.ai_provider_admission_state
    SET active_attempt_count = active_attempt_count - 1,
        updated_at = observed_at
    WHERE workspace_id = reservation_record.workspace_id
      AND active_attempt_count >= 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'AI reservation concurrency is inconsistent'
            USING ERRCODE = '55000';
    END IF;

    INSERT INTO schemabridge_control.ai_provider_usage_audit (
        audit_id,
        reservation_id,
        workspace_scope_digest,
        actor_digest,
        request_id,
        stage,
        attempt_number,
        model_snapshot,
        endpoint_region,
        configuration_fingerprint,
        request_fingerprint,
        semantic_scope_fingerprint,
        semantic_payload_fingerprint,
        input_tokens,
        output_tokens,
        duration_ms,
        outcome_code,
        occurred_at,
        retain_until
    ) VALUES (
        generated_audit_id,
        reservation_record.reservation_id,
        encode(
            sha256(
                convert_to(
                    'query_studio_scope_v1|'
                        || reservation_record.workspace_id,
                    'UTF8'
                )
            ),
            'hex'
        ),
        reservation_record.actor_digest,
        reservation_record.request_id,
        reservation_record.stage,
        reservation_record.attempt_number,
        reservation_record.model_snapshot,
        reservation_record.endpoint_region,
        reservation_record.configuration_fingerprint,
        reservation_record.request_fingerprint,
        reservation_record.semantic_scope_fingerprint,
        reservation_record.semantic_payload_fingerprint,
        charge_input,
        charge_output,
        p_duration_ms,
        p_outcome_code,
        observed_at,
        observed_at + make_interval(
            secs => reservation_record.audit_retention_seconds
        )
    );

    RETURN QUERY
    SELECT
        reservation_record.reservation_id,
        false,
        'settled'::varchar(16),
        p_outcome_code::varchar(32),
        charge_input,
        charge_output,
        observed_at,
        generated_audit_id;
END;
$$;

REVOKE ALL ON FUNCTION
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
    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
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
