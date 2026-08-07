CREATE TABLE schemabridge_control.tenant_ai_policies (
    workspace_id varchar(200) PRIMARY KEY
        CHECK (
            length(trim(workspace_id)) BETWEEN 1 AND 200
            AND octet_length(workspace_id) <= 200
        ),
    external_ai_enabled boolean NOT NULL DEFAULT false,
    provider_governance_accepted boolean NOT NULL DEFAULT false,
    provider_governance_fingerprint char(64)
        CHECK (
            provider_governance_fingerprint IS NULL
            OR provider_governance_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    provider_governance_accepted_at timestamptz,
    model_snapshot varchar(80) NOT NULL
        CHECK (
            model_snapshot IN (
                'gpt-5-nano-2025-08-07',
                'gpt-5.4-nano-2026-03-17',
                'gpt-5.6-luna'
            )
        ),
    endpoint_region varchar(16) NOT NULL
        CHECK (endpoint_region IN ('global', 'eu', 'us')),
    endpoint_origin_fingerprint char(64) NOT NULL
        CHECK (endpoint_origin_fingerprint ~ '^[0-9a-f]{64}$'),
    configuration_fingerprint char(64) NOT NULL
        CHECK (configuration_fingerprint ~ '^[0-9a-f]{64}$'),
    requests_per_minute integer NOT NULL
        CHECK (requests_per_minute BETWEEN 1 AND 10000),
    request_window_seconds integer NOT NULL DEFAULT 60
        CHECK (request_window_seconds = 60),
    daily_input_token_limit bigint NOT NULL
        CHECK (daily_input_token_limit BETWEEN 1 AND 1000000000),
    daily_output_token_limit bigint NOT NULL
        CHECK (daily_output_token_limit BETWEEN 1 AND 1000000000),
    concurrent_attempt_limit integer NOT NULL
        CHECK (concurrent_attempt_limit BETWEEN 1 AND 1000),
    reservation_lease_seconds integer NOT NULL
        CHECK (reservation_lease_seconds BETWEEN 10 AND 300),
    audit_retention_seconds integer NOT NULL
        CHECK (audit_retention_seconds BETWEEN 2592000 AND 315360000),
    version bigint NOT NULL CHECK (version >= 1),
    updated_by varchar(200) NOT NULL
        CHECK (
            updated_by ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
            AND octet_length(updated_by) <= 200
        ),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (updated_at >= created_at),
    CONSTRAINT tenant_ai_policy_enablement_shape
        CHECK (
            (
                external_ai_enabled
                AND provider_governance_accepted
                AND provider_governance_fingerprint IS NOT NULL
                AND provider_governance_accepted_at IS NOT NULL
            )
            OR
            (
                NOT external_ai_enabled
                AND (
                    (
                        provider_governance_accepted
                        AND provider_governance_fingerprint IS NOT NULL
                        AND provider_governance_accepted_at IS NOT NULL
                    )
                    OR
                    (
                        NOT provider_governance_accepted
                        AND provider_governance_fingerprint IS NULL
                        AND provider_governance_accepted_at IS NULL
                    )
                )
            )
        )
);

CREATE TABLE schemabridge_control.tenant_ai_policy_revisions (
    workspace_id varchar(200) NOT NULL,
    version bigint NOT NULL CHECK (version >= 1),
    external_ai_enabled boolean NOT NULL,
    provider_governance_accepted boolean NOT NULL,
    provider_governance_fingerprint char(64),
    provider_governance_accepted_at timestamptz,
    model_snapshot varchar(80) NOT NULL,
    endpoint_region varchar(16) NOT NULL,
    endpoint_origin_fingerprint char(64) NOT NULL,
    configuration_fingerprint char(64) NOT NULL,
    requests_per_minute integer NOT NULL,
    daily_input_token_limit bigint NOT NULL,
    daily_output_token_limit bigint NOT NULL,
    concurrent_attempt_limit integer NOT NULL,
    reservation_lease_seconds integer NOT NULL,
    audit_retention_seconds integer NOT NULL,
    updated_by varchar(200) NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, version),
    CONSTRAINT tenant_ai_policy_revision_policy_fk
        FOREIGN KEY (workspace_id)
        REFERENCES schemabridge_control.tenant_ai_policies (workspace_id),
    CONSTRAINT tenant_ai_policy_revision_shape
        CHECK (
            model_snapshot IN (
                'gpt-5-nano-2025-08-07',
                'gpt-5.4-nano-2026-03-17',
                'gpt-5.6-luna'
            )
            AND endpoint_region IN ('global', 'eu', 'us')
            AND endpoint_origin_fingerprint ~ '^[0-9a-f]{64}$'
            AND configuration_fingerprint ~ '^[0-9a-f]{64}$'
            AND requests_per_minute BETWEEN 1 AND 10000
            AND daily_input_token_limit BETWEEN 1 AND 1000000000
            AND daily_output_token_limit BETWEEN 1 AND 1000000000
            AND concurrent_attempt_limit BETWEEN 1 AND 1000
            AND reservation_lease_seconds BETWEEN 10 AND 300
            AND audit_retention_seconds BETWEEN 2592000 AND 315360000
            AND updated_by ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
            AND (
                (
                    provider_governance_accepted
                    AND provider_governance_fingerprint
                        ~ '^[0-9a-f]{64}$'
                    AND provider_governance_accepted_at IS NOT NULL
                )
                OR
                (
                    NOT provider_governance_accepted
                    AND provider_governance_fingerprint IS NULL
                    AND provider_governance_accepted_at IS NULL
                    AND NOT external_ai_enabled
                )
            )
        )
);

CREATE TABLE schemabridge_control.ai_provider_request_windows (
    workspace_id varchar(200) NOT NULL,
    actor_digest char(64) NOT NULL
        CHECK (actor_digest ~ '^[0-9a-f]{64}$'),
    window_started_at timestamptz NOT NULL,
    window_expires_at timestamptz NOT NULL,
    request_count integer NOT NULL CHECK (request_count >= 1),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, actor_digest, window_started_at),
    CONSTRAINT ai_request_window_policy_fk
        FOREIGN KEY (workspace_id)
        REFERENCES schemabridge_control.tenant_ai_policies (workspace_id),
    CHECK (window_expires_at > window_started_at),
    CHECK (updated_at >= created_at)
);

CREATE INDEX ai_provider_request_windows_expiry_idx
    ON schemabridge_control.ai_provider_request_windows (
        window_expires_at,
        workspace_id,
        actor_digest
    );

CREATE TABLE schemabridge_control.ai_provider_daily_usage (
    workspace_id varchar(200) NOT NULL,
    usage_date date NOT NULL,
    reserved_input_tokens bigint NOT NULL DEFAULT 0
        CHECK (reserved_input_tokens >= 0),
    reserved_output_tokens bigint NOT NULL DEFAULT 0
        CHECK (reserved_output_tokens >= 0),
    charged_input_tokens bigint NOT NULL DEFAULT 0
        CHECK (charged_input_tokens >= 0),
    charged_output_tokens bigint NOT NULL DEFAULT 0
        CHECK (charged_output_tokens >= 0),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, usage_date),
    CONSTRAINT ai_daily_usage_policy_fk
        FOREIGN KEY (workspace_id)
        REFERENCES schemabridge_control.tenant_ai_policies (workspace_id)
);

CREATE TABLE schemabridge_control.ai_provider_admission_state (
    workspace_id varchar(200) PRIMARY KEY,
    active_attempt_count integer NOT NULL DEFAULT 0
        CHECK (active_attempt_count >= 0),
    next_fencing_token bigint NOT NULL DEFAULT 0
        CHECK (next_fencing_token >= 0),
    updated_at timestamptz NOT NULL,
    CONSTRAINT ai_admission_state_policy_fk
        FOREIGN KEY (workspace_id)
        REFERENCES schemabridge_control.tenant_ai_policies (workspace_id)
);

CREATE TABLE schemabridge_control.ai_provider_attempt_reservations (
    reservation_id varchar(80) PRIMARY KEY
        CHECK (reservation_id ~ '^air_[0-9a-f]{64}$'),
    workspace_id varchar(200) NOT NULL,
    request_id varchar(200) NOT NULL
        CHECK (request_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'),
    actor_digest char(64) NOT NULL
        CHECK (actor_digest ~ '^[0-9a-f]{64}$'),
    stage varchar(24) NOT NULL
        CHECK (stage IN ('expansion', 'interpretation')),
    attempt_number smallint NOT NULL
        CHECK (attempt_number BETWEEN 1 AND 2),
    idempotency_digest char(64) NOT NULL
        CHECK (idempotency_digest ~ '^[0-9a-f]{64}$'),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    semantic_scope_fingerprint char(64) NOT NULL
        CHECK (semantic_scope_fingerprint ~ '^[0-9a-f]{64}$'),
    semantic_payload_fingerprint char(64) NOT NULL
        CHECK (semantic_payload_fingerprint ~ '^[0-9a-f]{64}$'),
    configuration_fingerprint char(64) NOT NULL
        CHECK (configuration_fingerprint ~ '^[0-9a-f]{64}$'),
    policy_version bigint NOT NULL CHECK (policy_version >= 1),
    model_snapshot varchar(80) NOT NULL
        CHECK (
            model_snapshot IN (
                'gpt-5-nano-2025-08-07',
                'gpt-5.4-nano-2026-03-17',
                'gpt-5.6-luna'
            )
        ),
    endpoint_region varchar(16) NOT NULL
        CHECK (endpoint_region IN ('global', 'eu', 'us')),
    estimated_input_tokens integer NOT NULL
        CHECK (estimated_input_tokens BETWEEN 1 AND 1000000),
    estimated_output_tokens integer NOT NULL
        CHECK (estimated_output_tokens BETWEEN 1 AND 1000000),
    audit_retention_seconds integer NOT NULL
        CHECK (audit_retention_seconds BETWEEN 2592000 AND 315360000),
    status varchar(16) NOT NULL
        CHECK (status IN ('reserved', 'settled', 'expired')),
    capability_digest char(64) NOT NULL
        CHECK (capability_digest ~ '^[0-9a-f]{64}$'),
    fencing_token bigint NOT NULL CHECK (fencing_token >= 1),
    lease_acquired_at timestamptz NOT NULL,
    lease_expires_at timestamptz,
    outcome_code varchar(32)
        CHECK (
            outcome_code IS NULL
            OR outcome_code IN (
                'succeeded',
                'timeout',
                'rate_limited',
                'provider_unavailable',
                'refusal',
                'missing_output',
                'invalid_output',
                'expired_crash'
            )
        ),
    observed_input_tokens integer
        CHECK (
            observed_input_tokens IS NULL
            OR observed_input_tokens BETWEEN 0 AND 1000000000
        ),
    observed_output_tokens integer
        CHECK (
            observed_output_tokens IS NULL
            OR observed_output_tokens BETWEEN 0 AND 1000000000
        ),
    charged_input_tokens integer
        CHECK (
            charged_input_tokens IS NULL
            OR charged_input_tokens BETWEEN 0 AND 1000000000
        ),
    charged_output_tokens integer
        CHECK (
            charged_output_tokens IS NULL
            OR charged_output_tokens BETWEEN 0 AND 1000000000
        ),
    duration_ms integer
        CHECK (duration_ms IS NULL OR duration_ms BETWEEN 0 AND 3600000),
    settlement_fingerprint char(64)
        CHECK (
            settlement_fingerprint IS NULL
            OR settlement_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    created_at timestamptz NOT NULL,
    settled_at timestamptz,
    retain_until timestamptz NOT NULL,
    UNIQUE (workspace_id, request_id, stage, attempt_number),
    UNIQUE (workspace_id, idempotency_digest),
    CONSTRAINT ai_attempt_policy_fk
        FOREIGN KEY (workspace_id)
        REFERENCES schemabridge_control.tenant_ai_policies (workspace_id),
    CONSTRAINT ai_attempt_policy_version_fk
        FOREIGN KEY (workspace_id, policy_version)
        REFERENCES schemabridge_control.tenant_ai_policy_revisions (
            workspace_id,
            version
        ),
    CHECK (retain_until >= created_at + interval '30 days'),
    CONSTRAINT ai_attempt_lifecycle_shape
        CHECK (
            (
                status = 'reserved'
                AND lease_expires_at IS NOT NULL
                AND lease_expires_at > lease_acquired_at
                AND outcome_code IS NULL
                AND observed_input_tokens IS NULL
                AND observed_output_tokens IS NULL
                AND charged_input_tokens IS NULL
                AND charged_output_tokens IS NULL
                AND duration_ms IS NULL
                AND settlement_fingerprint IS NULL
                AND settled_at IS NULL
            )
            OR
            (
                status IN ('settled', 'expired')
                AND lease_expires_at IS NULL
                AND outcome_code IS NOT NULL
                AND charged_input_tokens IS NOT NULL
                AND charged_output_tokens IS NOT NULL
                AND duration_ms IS NOT NULL
                AND settlement_fingerprint IS NOT NULL
                AND settled_at IS NOT NULL
                AND (
                    (
                        outcome_code = 'succeeded'
                        AND observed_input_tokens IS NOT NULL
                        AND observed_output_tokens IS NOT NULL
                    )
                    OR outcome_code <> 'succeeded'
                )
                AND (
                    (
                        observed_input_tokens IS NULL
                        AND observed_output_tokens IS NULL
                    )
                    OR
                    (
                        observed_input_tokens IS NOT NULL
                        AND observed_output_tokens IS NOT NULL
                    )
                )
            )
        )
);

CREATE INDEX ai_provider_attempt_active_idx
    ON schemabridge_control.ai_provider_attempt_reservations (
        workspace_id,
        lease_expires_at,
        fencing_token,
        reservation_id
    )
    WHERE status = 'reserved';

CREATE INDEX ai_provider_attempt_retention_idx
    ON schemabridge_control.ai_provider_attempt_reservations (
        retain_until,
        workspace_id,
        reservation_id
    )
    WHERE status IN ('settled', 'expired');

CREATE TABLE schemabridge_control.ai_provider_usage_audit (
    audit_id varchar(80) PRIMARY KEY
        CHECK (audit_id ~ '^aia_[0-9a-f]{64}$'),
    reservation_id varchar(80) NOT NULL UNIQUE
        CHECK (reservation_id ~ '^air_[0-9a-f]{64}$'),
    workspace_scope_digest char(64) NOT NULL
        CHECK (workspace_scope_digest ~ '^[0-9a-f]{64}$'),
    actor_digest char(64) NOT NULL
        CHECK (actor_digest ~ '^[0-9a-f]{64}$'),
    request_id varchar(200) NOT NULL
        CHECK (request_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'),
    stage varchar(24) NOT NULL
        CHECK (stage IN ('expansion', 'interpretation')),
    attempt_number smallint NOT NULL
        CHECK (attempt_number BETWEEN 1 AND 2),
    model_snapshot varchar(80) NOT NULL
        CHECK (
            model_snapshot IN (
                'gpt-5-nano-2025-08-07',
                'gpt-5.4-nano-2026-03-17',
                'gpt-5.6-luna'
            )
        ),
    endpoint_region varchar(16) NOT NULL
        CHECK (endpoint_region IN ('global', 'eu', 'us')),
    configuration_fingerprint char(64) NOT NULL
        CHECK (configuration_fingerprint ~ '^[0-9a-f]{64}$'),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    semantic_scope_fingerprint char(64) NOT NULL
        CHECK (semantic_scope_fingerprint ~ '^[0-9a-f]{64}$'),
    semantic_payload_fingerprint char(64) NOT NULL
        CHECK (semantic_payload_fingerprint ~ '^[0-9a-f]{64}$'),
    input_tokens integer NOT NULL
        CHECK (input_tokens BETWEEN 0 AND 1000000000),
    output_tokens integer NOT NULL
        CHECK (output_tokens BETWEEN 0 AND 1000000000),
    duration_ms integer NOT NULL
        CHECK (duration_ms BETWEEN 0 AND 3600000),
    outcome_code varchar(32) NOT NULL
        CHECK (
            outcome_code IN (
                'succeeded',
                'timeout',
                'rate_limited',
                'provider_unavailable',
                'refusal',
                'missing_output',
                'invalid_output',
                'expired_crash'
            )
        ),
    occurred_at timestamptz NOT NULL,
    retain_until timestamptz NOT NULL,
    CHECK (retain_until >= occurred_at + interval '30 days')
);

CREATE INDEX ai_provider_usage_audit_scope_time_idx
    ON schemabridge_control.ai_provider_usage_audit (
        workspace_scope_digest,
        occurred_at DESC,
        audit_id
    );

CREATE INDEX ai_provider_usage_audit_retention_idx
    ON schemabridge_control.ai_provider_usage_audit (
        retain_until,
        audit_id
    );

CREATE INDEX catalog_fields_physical_discovery_exact_idx
    ON schemabridge_control.catalog_fields (
        workspace_id,
        connection_id,
        generation,
        lower(field_name),
        asset_key,
        field_key
    );

CREATE FUNCTION schemabridge_control.reject_ai_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    RAISE EXCEPTION 'query studio immutable state cannot be changed'
        USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION schemabridge_control.guard_tenant_ai_policy()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'tenant AI policies cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.version <> OLD.version + 1
        OR NEW.updated_at <= OLD.updated_at
    ) THEN
        RAISE EXCEPTION 'tenant AI policy revision is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_ai_provider_attempt()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF (
            SESSION_USER <> 'schemabridge_migrator'
            OR current_setting(
                'schemabridge.ai_maintenance_operation',
                true
            ) IS DISTINCT FROM 'prune-v1'
            OR OLD.status NOT IN ('settled', 'expired')
            OR OLD.retain_until >= clock_timestamp()
        ) THEN
            RAISE EXCEPTION 'AI attempt reservation cannot be deleted'
                USING ERRCODE = '55000';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF (
            OLD.status <> 'reserved'
            OR NEW.status NOT IN ('settled', 'expired')
            OR NEW.reservation_id IS DISTINCT FROM OLD.reservation_id
            OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
            OR NEW.request_id IS DISTINCT FROM OLD.request_id
            OR NEW.actor_digest IS DISTINCT FROM OLD.actor_digest
            OR NEW.stage IS DISTINCT FROM OLD.stage
            OR NEW.attempt_number IS DISTINCT FROM OLD.attempt_number
            OR NEW.idempotency_digest IS DISTINCT FROM OLD.idempotency_digest
            OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
            OR NEW.semantic_scope_fingerprint
                IS DISTINCT FROM OLD.semantic_scope_fingerprint
            OR NEW.semantic_payload_fingerprint
                IS DISTINCT FROM OLD.semantic_payload_fingerprint
            OR NEW.configuration_fingerprint
                IS DISTINCT FROM OLD.configuration_fingerprint
            OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
            OR NEW.model_snapshot IS DISTINCT FROM OLD.model_snapshot
            OR NEW.endpoint_region IS DISTINCT FROM OLD.endpoint_region
            OR NEW.estimated_input_tokens
                IS DISTINCT FROM OLD.estimated_input_tokens
            OR NEW.estimated_output_tokens
                IS DISTINCT FROM OLD.estimated_output_tokens
            OR NEW.audit_retention_seconds
                IS DISTINCT FROM OLD.audit_retention_seconds
            OR NEW.capability_digest IS DISTINCT FROM OLD.capability_digest
            OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
            OR NEW.lease_acquired_at IS DISTINCT FROM OLD.lease_acquired_at
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
            OR NEW.retain_until IS DISTINCT FROM OLD.retain_until
        ) THEN
            RAISE EXCEPTION 'AI attempt reservation transition is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.apply_tenant_ai_policy(
    p_workspace_id varchar,
    p_expected_version bigint,
    p_external_ai_enabled boolean,
    p_provider_governance_accepted boolean,
    p_provider_governance_fingerprint char,
    p_model_snapshot varchar,
    p_endpoint_region varchar,
    p_endpoint_origin_fingerprint char,
    p_configuration_fingerprint char,
    p_requests_per_minute integer,
    p_daily_input_token_limit bigint,
    p_daily_output_token_limit bigint,
    p_concurrent_attempt_limit integer,
    p_reservation_lease_seconds integer,
    p_audit_retention_seconds integer,
    p_updated_by varchar,
    p_confirmation varchar
)
RETURNS TABLE (
    workspace_id varchar(200),
    version bigint,
    external_ai_enabled boolean,
    provider_governance_accepted boolean,
    model_snapshot varchar(80),
    endpoint_region varchar(16),
    configuration_fingerprint char(64),
    requests_per_minute integer,
    daily_input_token_limit bigint,
    daily_output_token_limit bigint,
    concurrent_attempt_limit integer,
    reservation_lease_seconds integer,
    audit_retention_seconds integer,
    updated_by varchar(200),
    updated_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    current_policy schemabridge_control.tenant_ai_policies%ROWTYPE;
    next_version bigint;
    observed_at timestamptz := clock_timestamp();
    accepted_at timestamptz;
BEGIN
    IF SESSION_USER <> 'schemabridge_migrator' THEN
        RAISE EXCEPTION 'tenant AI policy operator role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_workspace_id) > 200
        OR p_expected_version NOT BETWEEN 0 AND 9223372036854775806
        OR p_external_ai_enabled IS NULL
        OR p_provider_governance_accepted IS NULL
        OR (
            p_provider_governance_accepted
            AND p_provider_governance_fingerprint
                !~ '^[0-9a-f]{64}$'
        )
        OR (
            NOT p_provider_governance_accepted
            AND p_provider_governance_fingerprint IS NOT NULL
        )
        OR (
            p_external_ai_enabled
            AND NOT p_provider_governance_accepted
        )
        OR p_model_snapshot NOT IN (
            'gpt-5-nano-2025-08-07',
            'gpt-5.4-nano-2026-03-17',
            'gpt-5.6-luna'
        )
        OR p_endpoint_region NOT IN ('global', 'eu', 'us')
        OR p_endpoint_origin_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_configuration_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_requests_per_minute NOT BETWEEN 1 AND 10000
        OR p_daily_input_token_limit NOT BETWEEN 1 AND 1000000000
        OR p_daily_output_token_limit NOT BETWEEN 1 AND 1000000000
        OR p_concurrent_attempt_limit NOT BETWEEN 1 AND 1000
        OR p_reservation_lease_seconds NOT BETWEEN 10 AND 300
        OR p_audit_retention_seconds NOT BETWEEN 2592000 AND 315360000
        OR p_updated_by !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_updated_by) > 200
        OR p_confirmation <> 'APPLY TENANT AI POLICY'
    ) THEN
        RAISE EXCEPTION 'tenant AI policy input is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT *
    INTO current_policy
    FROM schemabridge_control.tenant_ai_policies AS policy
    WHERE policy.workspace_id = p_workspace_id
    FOR UPDATE;

    IF current_policy.workspace_id IS NULL THEN
        IF p_expected_version <> 0 THEN
            RAISE EXCEPTION 'tenant AI policy version conflict'
                USING ERRCODE = '40001';
        END IF;
        next_version := 1;
        accepted_at := CASE
            WHEN p_provider_governance_accepted THEN observed_at
            ELSE NULL
        END;
        INSERT INTO schemabridge_control.tenant_ai_policies (
            workspace_id,
            external_ai_enabled,
            provider_governance_accepted,
            provider_governance_fingerprint,
            provider_governance_accepted_at,
            model_snapshot,
            endpoint_region,
            endpoint_origin_fingerprint,
            configuration_fingerprint,
            requests_per_minute,
            request_window_seconds,
            daily_input_token_limit,
            daily_output_token_limit,
            concurrent_attempt_limit,
            reservation_lease_seconds,
            audit_retention_seconds,
            version,
            updated_by,
            created_at,
            updated_at
        ) VALUES (
            p_workspace_id,
            p_external_ai_enabled,
            p_provider_governance_accepted,
            p_provider_governance_fingerprint,
            accepted_at,
            p_model_snapshot,
            p_endpoint_region,
            p_endpoint_origin_fingerprint,
            p_configuration_fingerprint,
            p_requests_per_minute,
            60,
            p_daily_input_token_limit,
            p_daily_output_token_limit,
            p_concurrent_attempt_limit,
            p_reservation_lease_seconds,
            p_audit_retention_seconds,
            next_version,
            p_updated_by,
            observed_at,
            observed_at
        );
        INSERT INTO schemabridge_control.ai_provider_admission_state (
            workspace_id,
            active_attempt_count,
            next_fencing_token,
            updated_at
        ) VALUES (p_workspace_id, 0, 0, observed_at);
    ELSE
        IF current_policy.version <> p_expected_version THEN
            RAISE EXCEPTION 'tenant AI policy version conflict'
                USING ERRCODE = '40001';
        END IF;
        next_version := current_policy.version + 1;
        accepted_at := CASE
            WHEN NOT p_provider_governance_accepted THEN NULL
            WHEN (
                current_policy.provider_governance_accepted
                AND current_policy.provider_governance_fingerprint
                    = p_provider_governance_fingerprint
            ) THEN current_policy.provider_governance_accepted_at
            ELSE observed_at
        END;
        UPDATE schemabridge_control.tenant_ai_policies AS policy
        SET external_ai_enabled = p_external_ai_enabled,
            provider_governance_accepted = p_provider_governance_accepted,
            provider_governance_fingerprint
                = p_provider_governance_fingerprint,
            provider_governance_accepted_at = accepted_at,
            model_snapshot = p_model_snapshot,
            endpoint_region = p_endpoint_region,
            endpoint_origin_fingerprint = p_endpoint_origin_fingerprint,
            configuration_fingerprint = p_configuration_fingerprint,
            requests_per_minute = p_requests_per_minute,
            daily_input_token_limit = p_daily_input_token_limit,
            daily_output_token_limit = p_daily_output_token_limit,
            concurrent_attempt_limit = p_concurrent_attempt_limit,
            reservation_lease_seconds = p_reservation_lease_seconds,
            audit_retention_seconds = p_audit_retention_seconds,
            version = next_version,
            updated_by = p_updated_by,
            updated_at = observed_at
        WHERE policy.workspace_id = p_workspace_id;
    END IF;

    INSERT INTO schemabridge_control.tenant_ai_policy_revisions (
        workspace_id,
        version,
        external_ai_enabled,
        provider_governance_accepted,
        provider_governance_fingerprint,
        provider_governance_accepted_at,
        model_snapshot,
        endpoint_region,
        endpoint_origin_fingerprint,
        configuration_fingerprint,
        requests_per_minute,
        daily_input_token_limit,
        daily_output_token_limit,
        concurrent_attempt_limit,
        reservation_lease_seconds,
        audit_retention_seconds,
        updated_by,
        updated_at
    ) VALUES (
        p_workspace_id,
        next_version,
        p_external_ai_enabled,
        p_provider_governance_accepted,
        p_provider_governance_fingerprint,
        accepted_at,
        p_model_snapshot,
        p_endpoint_region,
        p_endpoint_origin_fingerprint,
        p_configuration_fingerprint,
        p_requests_per_minute,
        p_daily_input_token_limit,
        p_daily_output_token_limit,
        p_concurrent_attempt_limit,
        p_reservation_lease_seconds,
        p_audit_retention_seconds,
        p_updated_by,
        observed_at
    );

    RETURN QUERY
    SELECT
        policy.workspace_id,
        policy.version,
        policy.external_ai_enabled,
        policy.provider_governance_accepted,
        policy.model_snapshot,
        policy.endpoint_region,
        policy.configuration_fingerprint,
        policy.requests_per_minute,
        policy.daily_input_token_limit,
        policy.daily_output_token_limit,
        policy.concurrent_attempt_limit,
        policy.reservation_lease_seconds,
        policy.audit_retention_seconds,
        policy.updated_by,
        policy.updated_at
    FROM schemabridge_control.tenant_ai_policies AS policy
    WHERE policy.workspace_id = p_workspace_id;
END;
$$;

CREATE FUNCTION schemabridge_control.inspect_tenant_ai_policy(
    p_workspace_id varchar
)
RETURNS TABLE (
    workspace_id varchar(200),
    version bigint,
    external_ai_enabled boolean,
    provider_governance_accepted boolean,
    provider_governance_fingerprint char(64),
    provider_governance_accepted_at timestamptz,
    model_snapshot varchar(80),
    endpoint_region varchar(16),
    endpoint_origin_fingerprint char(64),
    configuration_fingerprint char(64),
    requests_per_minute integer,
    daily_input_token_limit bigint,
    daily_output_token_limit bigint,
    concurrent_attempt_limit integer,
    reservation_lease_seconds integer,
    audit_retention_seconds integer,
    updated_by varchar(200),
    updated_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF SESSION_USER <> 'schemabridge_migrator' THEN
        RAISE EXCEPTION 'tenant AI policy inspector role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR p_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_workspace_id) > 200
    ) THEN
        RAISE EXCEPTION 'tenant AI policy inspector scope is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        policy.workspace_id,
        policy.version,
        policy.external_ai_enabled,
        policy.provider_governance_accepted,
        policy.provider_governance_fingerprint,
        policy.provider_governance_accepted_at,
        policy.model_snapshot,
        policy.endpoint_region,
        policy.endpoint_origin_fingerprint,
        policy.configuration_fingerprint,
        policy.requests_per_minute,
        policy.daily_input_token_limit,
        policy.daily_output_token_limit,
        policy.concurrent_attempt_limit,
        policy.reservation_lease_seconds,
        policy.audit_retention_seconds,
        policy.updated_by,
        policy.updated_at
    FROM schemabridge_control.tenant_ai_policies AS policy
    WHERE policy.workspace_id = p_workspace_id;
END;
$$;

CREATE FUNCTION schemabridge_control.load_tenant_ai_policy(
    p_workspace_id varchar
)
RETURNS TABLE (
    version bigint,
    external_ai_enabled boolean,
    provider_governance_accepted boolean,
    model_snapshot varchar(80),
    endpoint_region varchar(16),
    configuration_fingerprint char(64),
    requests_per_minute integer,
    daily_input_token_limit bigint,
    daily_output_token_limit bigint,
    concurrent_attempt_limit integer,
    reservation_lease_seconds integer,
    audit_retention_seconds integer,
    updated_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'tenant AI policy reader role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR p_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_workspace_id) > 200
    ) THEN
        RAISE EXCEPTION 'tenant AI policy scope is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        policy.version,
        policy.external_ai_enabled,
        policy.provider_governance_accepted,
        policy.model_snapshot,
        policy.endpoint_region,
        policy.configuration_fingerprint,
        policy.requests_per_minute,
        policy.daily_input_token_limit,
        policy.daily_output_token_limit,
        policy.concurrent_attempt_limit,
        policy.reservation_lease_seconds,
        policy.audit_retention_seconds,
        policy.updated_at
    FROM schemabridge_control.tenant_ai_policies AS policy
    WHERE policy.workspace_id = p_workspace_id;
END;
$$;

CREATE FUNCTION schemabridge_control.load_physical_discovery_scope(
    p_workspace_id varchar,
    p_catalog_scope varchar,
    p_registry_id varchar
)
RETURNS TABLE (
    catalog_generation_vector_fingerprint char(64),
    connection_count bigint,
    asset_count bigint,
    field_count bigint
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'physical discovery scope role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_catalog_scope IS NULL
        OR length(trim(p_catalog_scope)) NOT BETWEEN 1 AND 120
        OR octet_length(p_catalog_scope) > 400
        OR p_registry_id IS NULL
        OR length(trim(p_registry_id)) NOT BETWEEN 1 AND 80
        OR octet_length(p_registry_id) > 320
    ) THEN
        RAISE EXCEPTION 'physical discovery scope input is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH current_generations AS MATERIALIZED (
        SELECT
            connection.connection_id,
            connection.active_generation,
            connection.active_generation_fingerprint,
            generation.asset_count,
            generation.field_count
        FROM schemabridge_control.catalog_connections AS connection
        JOIN schemabridge_control.catalog_generations AS generation
          ON generation.workspace_id = connection.workspace_id
         AND generation.connection_id = connection.connection_id
         AND generation.generation = connection.active_generation
         AND generation.status = 'completed'
        WHERE connection.workspace_id = p_workspace_id
          AND connection.catalog_scope = p_catalog_scope
          AND connection.status = 'enabled'
          AND connection.active_generation IS NOT NULL
          AND connection.active_generation_fingerprint IS NOT NULL
    )
    SELECT
        encode(
            sha256(
                convert_to(
                    jsonb_build_object(
                        'catalog_scope',
                        p_catalog_scope,
                        'connections',
                        coalesce(
                            jsonb_agg(
                                jsonb_build_array(
                                    current.connection_id,
                                    current.active_generation,
                                    current.active_generation_fingerprint
                                )
                                ORDER BY current.connection_id
                            ) FILTER (WHERE current.connection_id IS NOT NULL),
                            '[]'::jsonb
                        ),
                        'registry_id',
                        p_registry_id,
                        'workspace_id',
                        p_workspace_id
                    )::text,
                    'UTF8'
                )
            ),
            'hex'
        )::char(64),
        count(current.connection_id)::bigint,
        coalesce(sum(current.asset_count), 0)::bigint,
        coalesce(sum(current.field_count), 0)::bigint
    FROM current_generations AS current;
END;
$$;

CREATE FUNCTION schemabridge_control.resolve_query_studio_physical_type(
    p_approved_type varchar,
    p_native_type varchar
)
RETURNS varchar(32)
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
    WITH normalized AS (
        SELECT lower(
            regexp_replace(
                trim(coalesce(p_native_type, '')),
                '\s+',
                ' ',
                'g'
            )
        ) AS native_type
    )
    SELECT (
        CASE
            WHEN p_approved_type IN (
                'string',
                'integer',
                'decimal',
                'float',
                'boolean',
                'date',
                'timestamp'
            ) THEN p_approved_type
            WHEN normalized.native_type
                ~ '^(text|string|uuid|character varying|varchar|character|char)(\([1-9][0-9]*\))?$'
                THEN 'string'
            WHEN normalized.native_type
                ~ '^(smallint|integer|bigint|int2|int4|int8|smallserial|serial|bigserial)$'
                THEN 'integer'
            WHEN normalized.native_type
                ~ '^(numeric|decimal)(\([1-9][0-9]*(,[0-9]+)?\))?$'
                THEN 'decimal'
            WHEN normalized.native_type
                ~ '^(real|double precision|float|float4|float8)(\([1-9][0-9]*\))?$'
                THEN 'float'
            WHEN normalized.native_type IN ('boolean', 'bool')
                THEN 'boolean'
            WHEN normalized.native_type = 'date'
                THEN 'date'
            WHEN normalized.native_type
                ~ '^timestamp(\([0-9]+\))?( with time zone| without time zone)?$'
                OR normalized.native_type = 'timestamptz'
                THEN 'timestamp'
            ELSE NULL
        END
    )::varchar(32)
    FROM normalized;
$$;

CREATE FUNCTION schemabridge_control.discover_physical_fields(
    p_workspace_id varchar,
    p_catalog_scope varchar,
    p_registry_id varchar,
    p_catalog_generation_vector_fingerprint char,
    p_query varchar,
    p_page_size integer,
    p_after_score integer,
    p_after_connection_id varchar,
    p_after_asset_key char,
    p_after_field_key char
)
RETURNS TABLE (
    connection_id varchar(200),
    catalog_generation bigint,
    catalog_generation_fingerprint char(64),
    asset_key char(64),
    asset_id varchar(500),
    qualified_name varchar(500),
    asset_metadata_fingerprint char(64),
    field_key char(64),
    field_path varchar(200)[],
    field_name varchar(200),
    native_type varchar(200),
    field_description varchar(4000),
    field_metadata_fingerprint char(64),
    catalog_vector_fingerprint char(64),
    deterministic_score integer
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    normalized_query varchar;
    current_catalog_vector char(64);
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'physical discovery role is invalid'
            USING ERRCODE = '42501';
    END IF;
    normalized_query := lower(trim(coalesce(p_query, '')));
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_catalog_scope IS NULL
        OR length(trim(p_catalog_scope)) NOT BETWEEN 1 AND 120
        OR octet_length(p_catalog_scope) > 400
        OR p_registry_id IS NULL
        OR length(trim(p_registry_id)) NOT BETWEEN 1 AND 80
        OR octet_length(p_registry_id) > 320
        OR p_catalog_generation_vector_fingerprint !~ '^[0-9a-f]{64}$'
        OR length(normalized_query) > 2000
        OR octet_length(normalized_query) > 8192
        OR p_page_size NOT BETWEEN 1 AND 50
        OR (
            (
                p_after_score IS NULL
                AND p_after_connection_id IS NULL
                AND p_after_asset_key IS NULL
                AND p_after_field_key IS NULL
            ) IS FALSE
            AND (
                p_after_score IS NOT NULL
                AND p_after_score BETWEEN 0 AND 5000
                AND p_after_connection_id
                    ~ '^[a-z][a-z0-9_-]{2,199}$'
                AND p_after_asset_key ~ '^[0-9a-f]{64}$'
                AND p_after_field_key ~ '^[0-9a-f]{64}$'
            ) IS FALSE
        )
    ) THEN
        RAISE EXCEPTION 'physical discovery input is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT scope.catalog_generation_vector_fingerprint
    INTO current_catalog_vector
    FROM schemabridge_control.load_physical_discovery_scope(
        p_workspace_id,
        p_catalog_scope,
        p_registry_id
    ) AS scope;
    IF current_catalog_vector
        IS DISTINCT FROM p_catalog_generation_vector_fingerprint
    THEN
        RAISE EXCEPTION 'physical discovery catalog snapshot changed'
            USING ERRCODE = '40001';
    END IF;

    IF normalized_query = '' THEN
        RETURN QUERY
        SELECT
            connection.connection_id,
            connection.active_generation AS catalog_generation,
            connection.active_generation_fingerprint
                AS catalog_generation_fingerprint,
            asset.asset_key,
            asset.asset_id,
            asset.qualified_name,
            asset.metadata_fingerprint AS asset_metadata_fingerprint,
            field.field_key,
            field.field_path,
            field.field_name,
            field.native_type,
            field.description AS field_description,
            field.metadata_fingerprint AS field_metadata_fingerprint,
            current_catalog_vector,
            0::integer
        FROM schemabridge_control.catalog_connections AS connection
        JOIN schemabridge_control.catalog_generations AS generation
          ON generation.workspace_id = connection.workspace_id
         AND generation.connection_id = connection.connection_id
         AND generation.generation = connection.active_generation
         AND generation.status = 'completed'
        JOIN schemabridge_control.catalog_assets AS asset
          ON asset.workspace_id = connection.workspace_id
         AND asset.connection_id = connection.connection_id
         AND asset.generation = connection.active_generation
        JOIN schemabridge_control.catalog_fields AS field
          ON field.workspace_id = asset.workspace_id
         AND field.connection_id = asset.connection_id
         AND field.generation = asset.generation
         AND field.asset_key = asset.asset_key
        WHERE connection.workspace_id = p_workspace_id
          AND connection.catalog_scope = p_catalog_scope
          AND connection.status = 'enabled'
          AND connection.active_generation IS NOT NULL
          AND (
                p_after_score IS NULL
                OR (
                    p_after_score = 0
                    AND (
                        connection.connection_id,
                        asset.asset_key,
                        field.field_key
                    ) > (
                        p_after_connection_id,
                        p_after_asset_key,
                        p_after_field_key
                    )
                )
          )
        ORDER BY
            connection.connection_id,
            asset.asset_key,
            field.field_key
        LIMIT p_page_size + 1;
        RETURN;
    END IF;

    RETURN QUERY
    WITH current_connections AS MATERIALIZED (
        SELECT
            connection.workspace_id,
            connection.connection_id,
            connection.active_generation,
            connection.active_generation_fingerprint
        FROM schemabridge_control.catalog_connections AS connection
        JOIN schemabridge_control.catalog_generations AS generation
          ON generation.workspace_id = connection.workspace_id
         AND generation.connection_id = connection.connection_id
         AND generation.generation = connection.active_generation
         AND generation.status = 'completed'
        WHERE connection.workspace_id = p_workspace_id
          AND connection.catalog_scope = p_catalog_scope
          AND connection.status = 'enabled'
          AND connection.active_generation IS NOT NULL
    ),
    query_value AS MATERIALIZED (
        SELECT
            normalized_query AS normalized,
            plainto_tsquery(
                'pg_catalog.simple'::regconfig,
                normalized_query
            ) AS document_query
    ),
    exact_candidates AS MATERIALIZED (
        SELECT
            field.workspace_id,
            field.connection_id,
            field.generation,
            field.asset_key,
            field.field_key,
            1000::integer AS deterministic_score
        FROM schemabridge_control.catalog_fields AS field
        JOIN current_connections AS connection
          ON connection.workspace_id = field.workspace_id
         AND connection.connection_id = field.connection_id
         AND connection.active_generation = field.generation
        CROSS JOIN query_value AS query
        WHERE field.workspace_id = p_workspace_id
          AND lower(field.field_name) = query.normalized
          AND (
                p_after_score IS NULL
                OR 1000 < p_after_score
                OR (
                    p_after_score = 1000
                    AND (
                        field.connection_id,
                        field.asset_key,
                        field.field_key
                    ) > (
                        p_after_connection_id,
                        p_after_asset_key,
                        p_after_field_key
                    )
                )
          )
        ORDER BY
            field.connection_id,
            field.asset_key,
            field.field_key
        LIMIT p_page_size + 1
    ),
    field_fts_candidates AS MATERIALIZED (
        SELECT
            field.workspace_id,
            field.connection_id,
            field.generation,
            field.asset_key,
            field.field_key,
            500::integer AS deterministic_score
        FROM schemabridge_control.catalog_fields AS field
        JOIN current_connections AS connection
          ON connection.workspace_id = field.workspace_id
         AND connection.connection_id = field.connection_id
         AND connection.active_generation = field.generation
        CROSS JOIN query_value AS query
        WHERE (
                SELECT count(*) FROM exact_candidates
              ) < p_page_size + 1
          AND field.workspace_id = p_workspace_id
          AND field.search_document @@ query.document_query
          AND lower(field.field_name) <> query.normalized
          AND (
                p_after_score IS NULL
                OR 500 < p_after_score
                OR (
                    p_after_score = 500
                    AND (
                        field.connection_id,
                        field.asset_key,
                        field.field_key
                    ) > (
                        p_after_connection_id,
                        p_after_asset_key,
                        p_after_field_key
                    )
                )
          )
        ORDER BY
            field.connection_id,
            field.asset_key,
            field.field_key
        LIMIT greatest(
            p_page_size + 1 - (
                SELECT count(*) FROM exact_candidates
            ),
            0
        )
    ),
    asset_fts_candidates AS MATERIALIZED (
        SELECT
            field.workspace_id,
            field.connection_id,
            field.generation,
            field.asset_key,
            field.field_key,
            300::integer AS deterministic_score
        FROM schemabridge_control.catalog_assets AS asset
        JOIN current_connections AS connection
          ON connection.workspace_id = asset.workspace_id
         AND connection.connection_id = asset.connection_id
         AND connection.active_generation = asset.generation
        JOIN schemabridge_control.catalog_fields AS field
          ON field.workspace_id = asset.workspace_id
         AND field.connection_id = asset.connection_id
         AND field.generation = asset.generation
         AND field.asset_key = asset.asset_key
        CROSS JOIN query_value AS query
        WHERE (
                SELECT count(*)
                FROM (
                    SELECT 1 FROM exact_candidates
                    UNION ALL
                    SELECT 1 FROM field_fts_candidates
                ) AS higher_score_candidates
              ) < p_page_size + 1
          AND asset.workspace_id = p_workspace_id
          AND asset.search_document @@ query.document_query
          AND NOT (field.search_document @@ query.document_query)
          AND lower(field.field_name) <> query.normalized
          AND (
                p_after_score IS NULL
                OR 300 < p_after_score
                OR (
                    p_after_score = 300
                    AND (
                        field.connection_id,
                        field.asset_key,
                        field.field_key
                    ) > (
                        p_after_connection_id,
                        p_after_asset_key,
                        p_after_field_key
                    )
                )
          )
        ORDER BY
            field.connection_id,
            field.asset_key,
            field.field_key
        LIMIT greatest(
            p_page_size + 1 - (
                SELECT count(*)
                FROM (
                    SELECT 1 FROM exact_candidates
                    UNION ALL
                    SELECT 1 FROM field_fts_candidates
                ) AS higher_score_candidates
            ),
            0
        )
    ),
    candidate_keys AS MATERIALIZED (
        SELECT * FROM exact_candidates
        UNION ALL
        SELECT * FROM field_fts_candidates
        UNION ALL
        SELECT * FROM asset_fts_candidates
    ),
    matched AS MATERIALIZED (
        SELECT
            connection.connection_id,
            connection.active_generation AS catalog_generation,
            connection.active_generation_fingerprint
                AS catalog_generation_fingerprint,
            asset.asset_key,
            asset.asset_id,
            asset.qualified_name,
            asset.metadata_fingerprint AS asset_metadata_fingerprint,
            field.field_key,
            field.field_path,
            field.field_name,
            field.native_type,
            field.description AS field_description,
            field.metadata_fingerprint AS field_metadata_fingerprint,
            candidate.deterministic_score
        FROM candidate_keys AS candidate
        JOIN current_connections AS connection
          ON connection.workspace_id = candidate.workspace_id
         AND connection.connection_id = candidate.connection_id
         AND connection.active_generation = candidate.generation
        JOIN schemabridge_control.catalog_assets AS asset
          ON asset.workspace_id = candidate.workspace_id
         AND asset.connection_id = candidate.connection_id
         AND asset.generation = candidate.generation
         AND asset.asset_key = candidate.asset_key
        JOIN schemabridge_control.catalog_fields AS field
          ON field.workspace_id = candidate.workspace_id
         AND field.connection_id = candidate.connection_id
         AND field.generation = candidate.generation
         AND field.asset_key = candidate.asset_key
         AND field.field_key = candidate.field_key
    )
    SELECT
        matched.connection_id,
        matched.catalog_generation,
        matched.catalog_generation_fingerprint,
        matched.asset_key,
        matched.asset_id,
        matched.qualified_name,
        matched.asset_metadata_fingerprint,
        matched.field_key,
        matched.field_path,
        matched.field_name,
        matched.native_type,
        matched.field_description,
        matched.field_metadata_fingerprint,
        current_catalog_vector,
        matched.deterministic_score
    FROM matched
    WHERE (
        p_after_score IS NULL
        OR matched.deterministic_score < p_after_score
        OR (
            matched.deterministic_score = p_after_score
            AND (
                matched.connection_id,
                matched.asset_key,
                matched.field_key
            ) > (
                p_after_connection_id,
                p_after_asset_key,
                p_after_field_key
            )
        )
    )
    ORDER BY
        matched.deterministic_score DESC,
        matched.connection_id,
        matched.asset_key,
        matched.field_key
    LIMIT p_page_size + 1;
END;
$$;

CREATE FUNCTION schemabridge_control.load_query_studio_governance_scope(
    p_workspace_id varchar,
    p_catalog_scope varchar,
    p_registry_id varchar
)
RETURNS TABLE (
    registry_generation bigint,
    registry_version bigint,
    registry_fingerprint char(64),
    pointer_transition_id varchar(200),
    pointer_fingerprint char(64),
    head_revision bigint,
    baseline_revision bigint,
    baseline_fingerprint char(64),
    catalog_generation_vector_fingerprint char(64),
    eligible_mapping_count bigint
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'query studio governed scope role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_catalog_scope IS NULL
        OR length(trim(p_catalog_scope)) NOT BETWEEN 1 AND 120
        OR octet_length(p_catalog_scope) > 400
        OR p_registry_id IS NULL
        OR length(trim(p_registry_id)) NOT BETWEEN 1 AND 80
        OR octet_length(p_registry_id) > 320
    ) THEN
        RAISE EXCEPTION 'query studio governed scope input is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        pointer.generation,
        pointer.registry_version,
        pointer.registry_fingerprint,
        pointer.transition_id,
        report.pointer_fingerprint,
        head.head_revision,
        head.baseline_revision,
        head.baseline_fingerprint,
        head.catalog_generation_vector_fingerprint,
        count(gate.evidence_id) FILTER (WHERE gate.gate_eligible)
    FROM schemabridge_control.registry_active_pointers AS pointer
    JOIN schemabridge_control.semantic_change_heads AS head
      ON head.workspace_id = pointer.workspace_id
     AND head.catalog_scope = pointer.catalog_scope
     AND head.registry_id = pointer.registry_id
     AND head.registry_generation = pointer.generation
     AND head.registry_version = pointer.registry_version
     AND head.registry_fingerprint = pointer.registry_fingerprint
     AND head.pointer_transition_id = pointer.transition_id
    JOIN schemabridge_control.semantic_change_reports AS report
      ON report.workspace_id = head.workspace_id
     AND report.catalog_scope = head.catalog_scope
     AND report.registry_id = head.registry_id
     AND report.report_id = head.current_report_id
     AND report.report_fingerprint = head.current_report_fingerprint
    LEFT JOIN schemabridge_control.semantic_context_gate_projection AS gate
      ON gate.workspace_id = head.workspace_id
     AND gate.catalog_scope = head.catalog_scope
     AND gate.registry_id = head.registry_id
     AND gate.registry_generation = head.registry_generation
     AND gate.registry_version = head.registry_version
     AND gate.registry_fingerprint = head.registry_fingerprint
     AND gate.head_revision = head.head_revision
     AND gate.baseline_revision = head.baseline_revision
     AND gate.dependency_kind = 'mapping'
    WHERE pointer.workspace_id = p_workspace_id
      AND pointer.catalog_scope = p_catalog_scope
      AND pointer.registry_id = p_registry_id
      AND head.state IN ('current', 'revalidated')
      AND head.dependency_index_complete
      AND head.baseline_revision >= 1
      AND head.baseline_fingerprint IS NOT NULL
    GROUP BY
        pointer.generation,
        pointer.registry_version,
        pointer.registry_fingerprint,
        pointer.transition_id,
        report.pointer_fingerprint,
        head.head_revision,
        head.baseline_revision,
        head.baseline_fingerprint,
        head.catalog_generation_vector_fingerprint;
END;
$$;

CREATE FUNCTION schemabridge_control.search_governed_query_studio_fields(
    p_workspace_id varchar,
    p_catalog_scope varchar,
    p_registry_id varchar,
    p_registry_generation bigint,
    p_registry_version bigint,
    p_registry_fingerprint char,
    p_pointer_transition_id varchar,
    p_pointer_fingerprint char,
    p_head_revision bigint,
    p_baseline_revision bigint,
    p_baseline_fingerprint char,
    p_catalog_generation_vector_fingerprint char,
    p_query varchar,
    p_logical_field_allowlist varchar[],
    p_restrict_logical_fields boolean,
    p_page_size integer,
    p_after_score integer,
    p_after_logical_field varchar,
    p_after_binding_id varchar
)
RETURNS TABLE (
    logical_field varchar(200),
    mapping_decision_id varchar(200),
    mapping_version bigint,
    binding_id varchar(200),
    binding_fingerprint char(64),
    evidence_fingerprint char(64),
    binding_state varchar(24),
    connection_id varchar(200),
    catalog_generation bigint,
    catalog_generation_fingerprint char(64),
    asset_key char(64),
    asset_id varchar(500),
    qualified_name varchar(500),
    asset_metadata_fingerprint char(64),
    field_key char(64),
    field_path varchar(200)[],
    field_name varchar(200),
    native_type varchar(200),
    normalized_type varchar(32),
    nullable boolean,
    is_part_of_key boolean,
    field_description varchar(4000),
    tags varchar(200)[],
    glossary_terms varchar(200)[],
    field_metadata_fingerprint char(64),
    field_definition_fingerprint char(64),
    field_terms_fingerprint char(64),
    registry_generation bigint,
    registry_version bigint,
    registry_fingerprint char(64),
    pointer_transition_id varchar(200),
    pointer_fingerprint char(64),
    head_revision bigint,
    baseline_revision bigint,
    baseline_fingerprint char(64),
    catalog_generation_vector_fingerprint char(64),
    exact_logical_signal boolean,
    exact_field_name_signal boolean,
    field_name_prefix_signal boolean,
    glossary_term_signal boolean,
    tag_signal boolean,
    full_text_signal boolean,
    exact_type_signal boolean,
    deterministic_score integer
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    normalized_query varchar;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'query studio governed search role is invalid'
            USING ERRCODE = '42501';
    END IF;
    normalized_query := lower(trim(coalesce(p_query, '')));
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_catalog_scope IS NULL
        OR length(trim(p_catalog_scope)) NOT BETWEEN 1 AND 120
        OR octet_length(p_catalog_scope) > 400
        OR p_registry_id IS NULL
        OR length(trim(p_registry_id)) NOT BETWEEN 1 AND 80
        OR octet_length(p_registry_id) > 320
        OR p_registry_generation < 1
        OR p_registry_version < 1
        OR p_registry_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_pointer_transition_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_pointer_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_head_revision < 1
        OR p_baseline_revision < 1
        OR p_baseline_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_catalog_generation_vector_fingerprint
            !~ '^[0-9a-f]{64}$'
        OR length(normalized_query) > 2000
        OR octet_length(normalized_query) > 8192
        OR p_restrict_logical_fields IS NULL
        OR p_logical_field_allowlist IS NULL
        OR cardinality(p_logical_field_allowlist) > 1000
        OR array_position(p_logical_field_allowlist, NULL) IS NOT NULL
        OR octet_length(
            array_to_string(p_logical_field_allowlist, '')
        ) > 400000
        OR EXISTS (
            SELECT 1
            FROM unnest(p_logical_field_allowlist) AS allowed(value)
            WHERE allowed.value
                    !~ '^[A-Za-z_][A-Za-z0-9_]*[.][A-Za-z_][A-Za-z0-9_]*$'
        )
        OR (
            SELECT count(*) <> count(DISTINCT allowed.value)
            FROM unnest(p_logical_field_allowlist) AS allowed(value)
        )
        OR (
            NOT p_restrict_logical_fields
            AND cardinality(p_logical_field_allowlist) <> 0
        )
        OR p_page_size NOT BETWEEN 1 AND 50
        OR (
            (
                p_after_score IS NULL
                AND p_after_logical_field IS NULL
                AND p_after_binding_id IS NULL
            ) IS FALSE
            AND (
                p_after_score IS NOT NULL
                AND p_after_score BETWEEN 0 AND 5000
                AND length(trim(p_after_logical_field)) BETWEEN 1 AND 200
                AND octet_length(p_after_logical_field) <= 400
                AND p_after_binding_id ~ '^[a-z][a-z0-9_-]{2,199}$'
            ) IS FALSE
        )
    ) THEN
        RAISE EXCEPTION 'query studio governed search input is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH requested_scope AS MATERIALIZED (
        SELECT current_scope.*
        FROM schemabridge_control.load_query_studio_governance_scope(
            p_workspace_id,
            p_catalog_scope,
            p_registry_id
        ) AS current_scope
        WHERE current_scope.registry_generation = p_registry_generation
          AND current_scope.registry_version = p_registry_version
          AND current_scope.registry_fingerprint = p_registry_fingerprint
          AND current_scope.pointer_transition_id = p_pointer_transition_id
          AND current_scope.pointer_fingerprint = p_pointer_fingerprint
          AND current_scope.head_revision = p_head_revision
          AND current_scope.baseline_revision = p_baseline_revision
          AND current_scope.baseline_fingerprint = p_baseline_fingerprint
          AND current_scope.catalog_generation_vector_fingerprint
                = p_catalog_generation_vector_fingerprint
          AND current_scope.eligible_mapping_count BETWEEN 0 AND 1000
    ),
    query_value AS MATERIALIZED (
        SELECT
            normalized_query AS normalized,
            CASE
                WHEN normalized_query = '' THEN NULL::tsquery
                ELSE plainto_tsquery(
                    'pg_catalog.simple'::regconfig,
                    normalized_query
                )
            END AS document_query
    ),
    eligible AS MATERIALIZED (
        SELECT
            binding.logical_field,
            binding.mapping_decision_id,
            binding.mapping_version,
            binding.binding_id,
            binding.binding_fingerprint,
            binding.evidence_fingerprint,
            binding.binding_state,
            binding.connection_id,
            connection.active_generation AS catalog_generation,
            connection.active_generation_fingerprint
                AS catalog_generation_fingerprint,
            asset.asset_key,
            asset.asset_id,
            asset.qualified_name,
            asset.metadata_fingerprint AS asset_metadata_fingerprint,
            field.field_key,
            field.field_path,
            field.field_name,
            field.native_type,
            schemabridge_control.resolve_query_studio_physical_type(
                binding.normalized_type,
                field.native_type
            ) AS normalized_type,
            field.nullable,
            field.is_part_of_key,
            field.description AS field_description,
            field.tags,
            field.glossary_terms,
            field.metadata_fingerprint AS field_metadata_fingerprint,
            schemabridge_control.semantic_definition_fingerprint(
                field.description
            ) AS field_definition_fingerprint,
            schemabridge_control.semantic_terms_fingerprint(
                field.tags,
                field.glossary_terms
            ) AS field_terms_fingerprint,
            scope.registry_generation,
            scope.registry_version,
            scope.registry_fingerprint,
            scope.pointer_transition_id,
            scope.pointer_fingerprint,
            scope.head_revision,
            scope.baseline_revision,
            scope.baseline_fingerprint,
            scope.catalog_generation_vector_fingerprint,
            (
                query_value.normalized <> ''
                AND lower(binding.logical_field) = query_value.normalized
            ) AS exact_logical_signal,
            (
                query_value.normalized <> ''
                AND lower(field.field_name) = query_value.normalized
            ) AS exact_field_name_signal,
            (
                query_value.normalized <> ''
                AND lower(field.field_name)
                    LIKE query_value.normalized || '%'
            ) AS field_name_prefix_signal,
            (
                query_value.normalized <> ''
                AND EXISTS (
                    SELECT 1
                    FROM unnest(field.glossary_terms) AS term(value)
                    WHERE lower(term.value) = query_value.normalized
                )
            ) AS glossary_term_signal,
            (
                query_value.normalized <> ''
                AND EXISTS (
                    SELECT 1
                    FROM unnest(field.tags) AS tag(value)
                    WHERE lower(tag.value) = query_value.normalized
                )
            ) AS tag_signal,
            (
                query_value.document_query IS NOT NULL
                AND field.search_document @@ query_value.document_query
            ) AS full_text_signal,
            (
                query_value.normalized <> ''
                AND (
                    lower(coalesce(field.native_type, ''))
                        = query_value.normalized
                    OR lower(coalesce(
                        schemabridge_control.resolve_query_studio_physical_type(
                            binding.normalized_type,
                            field.native_type
                        ),
                        ''
                    ))
                        = query_value.normalized
                )
            ) AS exact_type_signal
        FROM requested_scope AS scope
        CROSS JOIN query_value
        JOIN schemabridge_control.semantic_context_gate_projection AS gate
          ON gate.workspace_id = p_workspace_id
         AND gate.catalog_scope = p_catalog_scope
         AND gate.registry_id = p_registry_id
         AND gate.registry_generation = scope.registry_generation
         AND gate.registry_version = scope.registry_version
         AND gate.registry_fingerprint = scope.registry_fingerprint
         AND gate.head_revision = scope.head_revision
         AND gate.baseline_revision = scope.baseline_revision
         AND gate.dependency_kind = 'mapping'
         AND gate.gate_eligible
        JOIN schemabridge_control.semantic_resource_bindings AS binding
          ON binding.workspace_id = gate.workspace_id
         AND binding.catalog_scope = gate.catalog_scope
         AND binding.registry_id = gate.registry_id
         AND binding.registry_generation = gate.registry_generation
         AND binding.registry_fingerprint = gate.registry_fingerprint
         AND binding.binding_id = gate.evidence_id
         AND binding.mapping_decision_id = gate.dependency_id
         AND binding.mapping_version = gate.dependency_version
         AND binding.approved_baseline_revision = gate.baseline_revision
         AND binding.binding_state IN ('approved', 'revalidated')
        JOIN schemabridge_control.catalog_connections AS connection
          ON connection.workspace_id = binding.workspace_id
         AND connection.catalog_scope = binding.catalog_scope
         AND connection.connection_id = binding.connection_id
         AND connection.status = 'enabled'
         AND connection.active_generation = gate.current_catalog_generation
         AND connection.active_generation_fingerprint
                = gate.current_catalog_generation_fingerprint
        JOIN schemabridge_control.catalog_assets AS asset
          ON asset.workspace_id = connection.workspace_id
         AND asset.connection_id = connection.connection_id
         AND asset.generation = connection.active_generation
         AND asset.asset_key = binding.asset_key
         AND asset.asset_id = binding.asset_id
        JOIN schemabridge_control.catalog_fields AS field
          ON field.workspace_id = asset.workspace_id
         AND field.connection_id = asset.connection_id
         AND field.generation = asset.generation
         AND field.asset_key = asset.asset_key
         AND field.field_key = binding.field_key
         AND field.field_path = binding.field_path
        WHERE schemabridge_control.resolve_query_studio_physical_type(
                binding.normalized_type,
                field.native_type
            ) IS NOT NULL
          AND (
                NOT p_restrict_logical_fields
                OR binding.logical_field = ANY(p_logical_field_allowlist)
            )
    ),
    matched AS MATERIALIZED (
        SELECT
            eligible.*,
            (
                CASE WHEN eligible.exact_logical_signal THEN 1000 ELSE 0 END
                + CASE WHEN eligible.exact_field_name_signal THEN 900 ELSE 0 END
                + CASE WHEN eligible.field_name_prefix_signal THEN 400 ELSE 0 END
                + CASE WHEN eligible.glossary_term_signal THEN 700 ELSE 0 END
                + CASE WHEN eligible.tag_signal THEN 650 ELSE 0 END
                + CASE WHEN eligible.full_text_signal THEN 500 ELSE 0 END
                + CASE WHEN eligible.exact_type_signal THEN 250 ELSE 0 END
            )::integer AS deterministic_score
        FROM eligible
        WHERE normalized_query = ''
           OR eligible.exact_logical_signal
           OR eligible.exact_field_name_signal
           OR eligible.field_name_prefix_signal
           OR eligible.glossary_term_signal
           OR eligible.tag_signal
           OR eligible.full_text_signal
           OR eligible.exact_type_signal
    )
    SELECT
        matched.logical_field,
        matched.mapping_decision_id,
        matched.mapping_version,
        matched.binding_id,
        matched.binding_fingerprint,
        matched.evidence_fingerprint,
        matched.binding_state,
        matched.connection_id,
        matched.catalog_generation,
        matched.catalog_generation_fingerprint,
        matched.asset_key,
        matched.asset_id,
        matched.qualified_name,
        matched.asset_metadata_fingerprint,
        matched.field_key,
        matched.field_path,
        matched.field_name,
        matched.native_type,
        matched.normalized_type,
        matched.nullable,
        matched.is_part_of_key,
        matched.field_description,
        matched.tags,
        matched.glossary_terms,
        matched.field_metadata_fingerprint,
        matched.field_definition_fingerprint,
        matched.field_terms_fingerprint,
        matched.registry_generation,
        matched.registry_version,
        matched.registry_fingerprint,
        matched.pointer_transition_id,
        matched.pointer_fingerprint,
        matched.head_revision,
        matched.baseline_revision,
        matched.baseline_fingerprint,
        matched.catalog_generation_vector_fingerprint,
        matched.exact_logical_signal,
        matched.exact_field_name_signal,
        matched.field_name_prefix_signal,
        matched.glossary_term_signal,
        matched.tag_signal,
        matched.full_text_signal,
        matched.exact_type_signal,
        matched.deterministic_score
    FROM matched
    WHERE (
        p_after_score IS NULL
        OR matched.deterministic_score < p_after_score
        OR (
            matched.deterministic_score = p_after_score
            AND (
                matched.logical_field,
                matched.binding_id
            ) > (
                p_after_logical_field,
                p_after_binding_id
            )
        )
    )
    ORDER BY
        matched.deterministic_score DESC,
        matched.logical_field,
        matched.binding_id
    LIMIT p_page_size + 1;
END;
$$;

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
    reservation_record record;
    observed_at timestamptz := clock_timestamp();
    expired_count integer := 0;
    generated_settlement_fingerprint char(64);
    generated_audit_id varchar(80);
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

    FOR reservation_record IN
        SELECT reservation.*
        FROM schemabridge_control.ai_provider_attempt_reservations AS reservation
        WHERE reservation.workspace_id = p_workspace_id
          AND reservation.status = 'reserved'
          AND reservation.lease_expires_at <= observed_at
        ORDER BY
            reservation.lease_expires_at,
            reservation.fencing_token,
            reservation.reservation_id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    LOOP
        generated_settlement_fingerprint := encode(
            sha256(
                convert_to(
                    concat_ws(
                        '|',
                        'ai_attempt_settlement_v1',
                        reservation_record.reservation_id,
                        reservation_record.fencing_token::text,
                        'expired_crash',
                        reservation_record.estimated_input_tokens::text,
                        reservation_record.estimated_output_tokens::text,
                        '0'
                    ),
                    'UTF8'
                )
            ),
            'hex'
        )::char(64);
        UPDATE schemabridge_control.ai_provider_attempt_reservations
        SET status = 'expired',
            lease_expires_at = NULL,
            outcome_code = 'expired_crash',
            charged_input_tokens
                = reservation_record.estimated_input_tokens,
            charged_output_tokens
                = reservation_record.estimated_output_tokens,
            duration_ms = 0,
            settlement_fingerprint = generated_settlement_fingerprint,
            settled_at = observed_at
        WHERE reservation_id = reservation_record.reservation_id
          AND status = 'reserved'
          AND fencing_token = reservation_record.fencing_token;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        UPDATE schemabridge_control.ai_provider_daily_usage
        SET reserved_input_tokens = reserved_input_tokens
                - reservation_record.estimated_input_tokens,
            reserved_output_tokens = reserved_output_tokens
                - reservation_record.estimated_output_tokens,
            charged_input_tokens = charged_input_tokens
                + reservation_record.estimated_input_tokens,
            charged_output_tokens = charged_output_tokens
                + reservation_record.estimated_output_tokens,
            updated_at = observed_at
        WHERE workspace_id = reservation_record.workspace_id
          AND usage_date = (
                reservation_record.created_at AT TIME ZONE 'UTC'
            )::date
          AND reserved_input_tokens
                >= reservation_record.estimated_input_tokens
          AND reserved_output_tokens
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
            reservation_record.estimated_input_tokens,
            reservation_record.estimated_output_tokens,
            0,
            'expired_crash',
            observed_at,
            observed_at + make_interval(
                secs => reservation_record.audit_retention_seconds
            )
        );
        expired_count := expired_count + 1;
    END LOOP;
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
    state_record schemabridge_control.ai_provider_admission_state%ROWTYPE;
    existing_record
        schemabridge_control.ai_provider_attempt_reservations%ROWTYPE;
    observed_at timestamptz := clock_timestamp();
    window_start timestamptz;
    usage_day date;
    window_admitted integer;
    capability_digest char(64);
    next_fence bigint;
    generated_reservation_id varchar(80);
    expiration_time timestamptz;
    retention_time timestamptz;
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
    capability_digest := encode(
        sha256(convert_to(p_lease_capability, 'UTF8')),
        'hex'
    )::char(64);

    SELECT *
    INTO existing_record
    FROM schemabridge_control.ai_provider_attempt_reservations AS reservation
    WHERE reservation.workspace_id = p_workspace_id
      AND reservation.idempotency_digest = p_idempotency_digest
    FOR UPDATE;
    IF existing_record.reservation_id IS NOT NULL THEN
        IF (
            existing_record.request_id <> p_request_id
            OR existing_record.actor_digest <> p_actor_digest
            OR existing_record.stage <> p_stage
            OR existing_record.attempt_number <> p_attempt_number
            OR existing_record.request_fingerprint <> p_request_fingerprint
            OR existing_record.semantic_scope_fingerprint
                <> p_semantic_scope_fingerprint
            OR existing_record.semantic_payload_fingerprint
                <> p_semantic_payload_fingerprint
            OR existing_record.configuration_fingerprint
                <> p_configuration_fingerprint
            OR existing_record.estimated_input_tokens
                <> p_estimated_input_tokens
            OR existing_record.estimated_output_tokens
                <> p_estimated_output_tokens
            OR existing_record.capability_digest <> capability_digest
        ) THEN
            RAISE EXCEPTION 'AI reservation idempotency conflict'
                USING ERRCODE = '23505';
        END IF;
        SELECT *
        INTO policy_record
        FROM schemabridge_control.tenant_ai_policies AS policy
        WHERE policy.workspace_id = p_workspace_id
        FOR SHARE;
        IF (
            existing_record.status = 'reserved'
            AND (
                policy_record.workspace_id IS NULL
                OR NOT policy_record.external_ai_enabled
                OR NOT policy_record.provider_governance_accepted
            )
        ) THEN
            RETURN QUERY
            SELECT
                'policy_disabled'::varchar(32),
                false,
                NULL::varchar(80),
                NULL::varchar(16),
                NULL::bigint,
                NULL::timestamptz,
                coalesce(policy_record.version, 0),
                policy_record.model_snapshot,
                policy_record.endpoint_region,
                policy_record.configuration_fingerprint;
            RETURN;
        END IF;
        IF (
            existing_record.status = 'reserved'
            AND (
                policy_record.configuration_fingerprint
                    <> p_configuration_fingerprint
                OR policy_record.version <> existing_record.policy_version
            )
        ) THEN
            RETURN QUERY
            SELECT
                'policy_mismatch'::varchar(32),
                false,
                NULL::varchar(80),
                NULL::varchar(16),
                NULL::bigint,
                NULL::timestamptz,
                policy_record.version,
                policy_record.model_snapshot,
                policy_record.endpoint_region,
                policy_record.configuration_fingerprint;
            RETURN;
        END IF;
        RETURN QUERY
        SELECT
            'replayed'::varchar(32),
            true,
            existing_record.reservation_id,
            existing_record.status,
            existing_record.fencing_token,
            existing_record.lease_expires_at,
            existing_record.policy_version,
            existing_record.model_snapshot,
            existing_record.endpoint_region,
            existing_record.configuration_fingerprint;
        RETURN;
    END IF;

    PERFORM schemabridge_control.expire_ai_provider_attempts(
        p_workspace_id,
        1000
    );

    SELECT *
    INTO policy_record
    FROM schemabridge_control.tenant_ai_policies AS policy
    WHERE policy.workspace_id = p_workspace_id
    FOR UPDATE;
    IF (
        policy_record.workspace_id IS NULL
        OR NOT policy_record.external_ai_enabled
        OR NOT policy_record.provider_governance_accepted
    ) THEN
        RETURN QUERY
        SELECT
            'policy_disabled'::varchar(32),
            false,
            NULL::varchar(80),
            NULL::varchar(16),
            NULL::bigint,
            NULL::timestamptz,
            coalesce(policy_record.version, 0),
            policy_record.model_snapshot,
            policy_record.endpoint_region,
            policy_record.configuration_fingerprint;
        RETURN;
    END IF;
    IF (
        policy_record.configuration_fingerprint
            <> p_configuration_fingerprint
    ) THEN
        RETURN QUERY
        SELECT
            'policy_mismatch'::varchar(32),
            false,
            NULL::varchar(80),
            NULL::varchar(16),
            NULL::bigint,
            NULL::timestamptz,
            policy_record.version,
            policy_record.model_snapshot,
            policy_record.endpoint_region,
            policy_record.configuration_fingerprint;
        RETURN;
    END IF;

    SELECT *
    INTO state_record
    FROM schemabridge_control.ai_provider_admission_state AS state
    WHERE state.workspace_id = p_workspace_id
    FOR UPDATE;
    IF state_record.workspace_id IS NULL THEN
        RAISE EXCEPTION 'AI admission state is unavailable'
            USING ERRCODE = '55000';
    END IF;

    window_start := date_trunc('minute', observed_at);
    INSERT INTO schemabridge_control.ai_provider_request_windows (
        workspace_id,
        actor_digest,
        window_started_at,
        window_expires_at,
        request_count,
        created_at,
        updated_at
    ) VALUES (
        p_workspace_id,
        p_actor_digest,
        window_start,
        window_start + interval '60 seconds',
        1,
        observed_at,
        observed_at
    )
    ON CONFLICT (workspace_id, actor_digest, window_started_at)
    DO UPDATE
    SET request_count
            = schemabridge_control.ai_provider_request_windows.request_count + 1,
        updated_at = observed_at
    WHERE schemabridge_control.ai_provider_request_windows.request_count
            < policy_record.requests_per_minute
    RETURNING request_count
    INTO window_admitted;
    IF window_admitted IS NULL THEN
        RETURN QUERY
        SELECT
            'rate_limited'::varchar(32),
            false,
            NULL::varchar(80),
            NULL::varchar(16),
            NULL::bigint,
            NULL::timestamptz,
            policy_record.version,
            policy_record.model_snapshot,
            policy_record.endpoint_region,
            policy_record.configuration_fingerprint;
        RETURN;
    END IF;

    usage_day := (observed_at AT TIME ZONE 'UTC')::date;
    INSERT INTO schemabridge_control.ai_provider_daily_usage (
        workspace_id,
        usage_date,
        reserved_input_tokens,
        reserved_output_tokens,
        charged_input_tokens,
        charged_output_tokens,
        updated_at
    ) VALUES (
        p_workspace_id,
        usage_day,
        0,
        0,
        0,
        0,
        observed_at
    )
    ON CONFLICT (workspace_id, usage_date) DO NOTHING;

    PERFORM 1
    FROM schemabridge_control.ai_provider_daily_usage AS usage
    WHERE usage.workspace_id = p_workspace_id
      AND usage.usage_date = usage_day
      AND usage.reserved_input_tokens
            + usage.charged_input_tokens
            + p_estimated_input_tokens
            <= policy_record.daily_input_token_limit
      AND usage.reserved_output_tokens
            + usage.charged_output_tokens
            + p_estimated_output_tokens
            <= policy_record.daily_output_token_limit
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            'quota_exhausted'::varchar(32),
            false,
            NULL::varchar(80),
            NULL::varchar(16),
            NULL::bigint,
            NULL::timestamptz,
            policy_record.version,
            policy_record.model_snapshot,
            policy_record.endpoint_region,
            policy_record.configuration_fingerprint;
        RETURN;
    END IF;

    IF (
        state_record.active_attempt_count
            >= policy_record.concurrent_attempt_limit
    ) THEN
        RETURN QUERY
        SELECT
            'concurrency_limited'::varchar(32),
            false,
            NULL::varchar(80),
            NULL::varchar(16),
            NULL::bigint,
            NULL::timestamptz,
            policy_record.version,
            policy_record.model_snapshot,
            policy_record.endpoint_region,
            policy_record.configuration_fingerprint;
        RETURN;
    END IF;

    next_fence := state_record.next_fencing_token + 1;
    expiration_time := observed_at
        + make_interval(secs => policy_record.reservation_lease_seconds);
    retention_time := observed_at
        + make_interval(secs => policy_record.audit_retention_seconds);
    generated_reservation_id := 'air_' || encode(
        sha256(
            convert_to(
                concat_ws(
                    '|',
                    'ai_attempt_reservation_v1',
                    p_workspace_id,
                    p_request_id,
                    p_stage,
                    p_attempt_number::text,
                    p_idempotency_digest
                ),
                'UTF8'
            )
        ),
        'hex'
    );

    UPDATE schemabridge_control.ai_provider_admission_state
    SET active_attempt_count = active_attempt_count + 1,
        next_fencing_token = next_fence,
        updated_at = observed_at
    WHERE workspace_id = p_workspace_id;

    UPDATE schemabridge_control.ai_provider_daily_usage
    SET reserved_input_tokens
            = reserved_input_tokens + p_estimated_input_tokens,
        reserved_output_tokens
            = reserved_output_tokens + p_estimated_output_tokens,
        updated_at = observed_at
    WHERE workspace_id = p_workspace_id
      AND usage_date = usage_day;

    INSERT INTO schemabridge_control.ai_provider_attempt_reservations (
        reservation_id,
        workspace_id,
        request_id,
        actor_digest,
        stage,
        attempt_number,
        idempotency_digest,
        request_fingerprint,
        semantic_scope_fingerprint,
        semantic_payload_fingerprint,
        configuration_fingerprint,
        policy_version,
        model_snapshot,
        endpoint_region,
        estimated_input_tokens,
        estimated_output_tokens,
        audit_retention_seconds,
        status,
        capability_digest,
        fencing_token,
        lease_acquired_at,
        lease_expires_at,
        created_at,
        retain_until
    ) VALUES (
        generated_reservation_id,
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
        policy_record.version,
        policy_record.model_snapshot,
        policy_record.endpoint_region,
        p_estimated_input_tokens,
        p_estimated_output_tokens,
        policy_record.audit_retention_seconds,
        'reserved',
        capability_digest,
        next_fence,
        observed_at,
        expiration_time,
        observed_at,
        retention_time
    );

    RETURN QUERY
    SELECT
        'reserved'::varchar(32),
        false,
        generated_reservation_id,
        'reserved'::varchar(16),
        next_fence,
        expiration_time,
        policy_record.version,
        policy_record.model_snapshot,
        policy_record.endpoint_region,
        policy_record.configuration_fingerprint;
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
            AND p_observed_input_tokens IS NULL
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

CREATE FUNCTION schemabridge_control.prune_ai_provider_temporary_state(
    p_before timestamptz,
    p_limit integer,
    p_confirmation varchar
)
RETURNS TABLE (
    request_windows_deleted integer,
    reservations_deleted integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    deleted_windows integer;
    deleted_reservations integer;
BEGIN
    IF SESSION_USER <> 'schemabridge_migrator' THEN
        RAISE EXCEPTION 'AI temporary-state maintenance role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_before IS NULL
        OR p_before > clock_timestamp()
        OR p_limit NOT BETWEEN 1 AND 10000
        OR p_confirmation <> 'PRUNE EXPIRED AI TEMPORARY STATE'
    ) THEN
        RAISE EXCEPTION 'AI temporary-state maintenance input is invalid'
            USING ERRCODE = '22023';
    END IF;

    PERFORM set_config(
        'schemabridge.ai_maintenance_operation',
        'prune-v1',
        true
    );
    WITH selected AS (
        SELECT
            workspace_id,
            actor_digest,
            window_started_at
        FROM schemabridge_control.ai_provider_request_windows
        WHERE window_expires_at < p_before
        ORDER BY
            window_expires_at,
            workspace_id,
            actor_digest
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    ),
    removed AS (
        DELETE FROM schemabridge_control.ai_provider_request_windows AS request_window
        USING selected
        WHERE request_window.workspace_id = selected.workspace_id
          AND request_window.actor_digest = selected.actor_digest
          AND request_window.window_started_at = selected.window_started_at
        RETURNING 1
    )
    SELECT count(*)::integer INTO deleted_windows FROM removed;

    WITH selected AS (
        SELECT reservation_id
        FROM schemabridge_control.ai_provider_attempt_reservations
        WHERE status IN ('settled', 'expired')
          AND retain_until < p_before
        ORDER BY retain_until, reservation_id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    ),
    removed AS (
        DELETE FROM schemabridge_control.ai_provider_attempt_reservations
        WHERE reservation_id IN (
            SELECT reservation_id FROM selected
        )
        RETURNING 1
    )
    SELECT count(*)::integer INTO deleted_reservations FROM removed;

    RETURN QUERY SELECT deleted_windows, deleted_reservations;
END;
$$;

CREATE TRIGGER tenant_ai_policies_guard
BEFORE UPDATE OR DELETE
ON schemabridge_control.tenant_ai_policies
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_tenant_ai_policy();

CREATE TRIGGER tenant_ai_policy_revisions_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.tenant_ai_policy_revisions
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_ai_immutable_mutation();

CREATE TRIGGER ai_provider_attempt_reservations_guard
BEFORE UPDATE OR DELETE
ON schemabridge_control.ai_provider_attempt_reservations
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_ai_provider_attempt();

CREATE TRIGGER ai_provider_usage_audit_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.ai_provider_usage_audit
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_ai_immutable_mutation();

REVOKE ALL ON
    schemabridge_control.tenant_ai_policies,
    schemabridge_control.tenant_ai_policy_revisions,
    schemabridge_control.ai_provider_request_windows,
    schemabridge_control.ai_provider_daily_usage,
    schemabridge_control.ai_provider_admission_state,
    schemabridge_control.ai_provider_attempt_reservations,
    schemabridge_control.ai_provider_usage_audit
    FROM PUBLIC;

REVOKE ALL ON FUNCTION
    schemabridge_control.reject_ai_immutable_mutation(),
    schemabridge_control.guard_tenant_ai_policy(),
    schemabridge_control.guard_ai_provider_attempt(),
    schemabridge_control.apply_tenant_ai_policy(
        varchar,
        bigint,
        boolean,
        boolean,
        char,
        varchar,
        varchar,
        char,
        char,
        integer,
        bigint,
        bigint,
        integer,
        integer,
        integer,
        varchar,
        varchar
    ),
    schemabridge_control.inspect_tenant_ai_policy(varchar),
    schemabridge_control.load_tenant_ai_policy(varchar),
    schemabridge_control.resolve_query_studio_physical_type(
        varchar,
        varchar
    ),
    schemabridge_control.load_physical_discovery_scope(
        varchar,
        varchar,
        varchar
    ),
    schemabridge_control.discover_physical_fields(
        varchar,
        varchar,
        varchar,
        char,
        varchar,
        integer,
        integer,
        varchar,
        char,
        char
    ),
    schemabridge_control.load_query_studio_governance_scope(
        varchar,
        varchar,
        varchar
    ),
    schemabridge_control.search_governed_query_studio_fields(
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        char,
        varchar,
        char,
        bigint,
        bigint,
        char,
        char,
        varchar,
        varchar[],
        boolean,
        integer,
        integer,
        varchar,
        varchar
    ),
    schemabridge_control.expire_ai_provider_attempts(varchar, integer),
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
    ),
    schemabridge_control.prune_ai_provider_temporary_state(
        timestamptz,
        integer,
        varchar
    )
    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_tenant_ai_policy(varchar),
    schemabridge_control.load_physical_discovery_scope(
        varchar,
        varchar,
        varchar
    ),
    schemabridge_control.discover_physical_fields(
        varchar,
        varchar,
        varchar,
        char,
        varchar,
        integer,
        integer,
        varchar,
        char,
        char
    ),
    schemabridge_control.load_query_studio_governance_scope(
        varchar,
        varchar,
        varchar
    ),
    schemabridge_control.search_governed_query_studio_fields(
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        char,
        varchar,
        char,
        bigint,
        bigint,
        char,
        char,
        varchar,
        varchar[],
        boolean,
        integer,
        integer,
        varchar,
        varchar
    ),
    schemabridge_control.expire_ai_provider_attempts(varchar, integer),
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

GRANT EXECUTE ON FUNCTION
    schemabridge_control.reject_ai_immutable_mutation(),
    schemabridge_control.guard_tenant_ai_policy(),
    schemabridge_control.guard_ai_provider_attempt(),
    schemabridge_control.apply_tenant_ai_policy(
        varchar,
        bigint,
        boolean,
        boolean,
        char,
        varchar,
        varchar,
        char,
        char,
        integer,
        bigint,
        bigint,
        integer,
        integer,
        integer,
        varchar,
        varchar
    ),
    schemabridge_control.inspect_tenant_ai_policy(varchar),
    schemabridge_control.prune_ai_provider_temporary_state(
        timestamptz,
        integer,
        varchar
    )
    TO schemabridge_migrator;
