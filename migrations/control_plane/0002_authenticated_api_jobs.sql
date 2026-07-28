GRANT USAGE ON SCHEMA schemabridge_control
    TO schemabridge_api, schemabridge_worker;

ALTER TABLE schemabridge_control.workflow_access_grants
    ADD CONSTRAINT workflow_access_grants_identity_unique
    UNIQUE (workspace_id, workflow_id, owner_actor_id);

CREATE TABLE schemabridge_control.execution_jobs (
    job_id varchar(200) PRIMARY KEY
        CHECK (job_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'),
    kind varchar(40) NOT NULL
        CHECK (kind = 'execute_workflow_preview'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    workflow_workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workflow_workspace_id)) BETWEEN 1 AND 200),
    workflow_id varchar(64) NOT NULL
        CHECK (workflow_id ~ '^[a-z0-9][a-z0-9_-]{2,63}$'),
    workflow_owner_actor_id varchar(200) NOT NULL
        CHECK (length(trim(workflow_owner_actor_id)) BETWEEN 1 AND 200),
    submitting_actor_id varchar(200) NOT NULL
        CHECK (length(trim(submitting_actor_id)) BETWEEN 1 AND 200),
    workflow_access_scope varchar(16) NOT NULL
        CHECK (workflow_access_scope IN ('owner', 'workspace')),
    authorized_operation varchar(32) NOT NULL
        CHECK (authorized_operation = 'execute_workflow_preview'),
    expected_workflow_revision integer NOT NULL
        CHECK (expected_workflow_revision >= 1),
    expected_plan_fingerprint char(64) NOT NULL
        CHECK (expected_plan_fingerprint ~ '^[0-9a-f]{64}$'),
    authenticated_at timestamptz NOT NULL,
    authorized_at timestamptz NOT NULL,
    authorization_expires_at timestamptz NOT NULL,
    payload_fingerprint char(64) NOT NULL
        CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    idempotency_digest char(64) NOT NULL
        CHECK (idempotency_digest ~ '^[0-9a-f]{64}$'),
    status varchar(24) NOT NULL
        CHECK (
            status IN (
                'queued',
                'leased',
                'cancel_requested',
                'retry_wait',
                'succeeded',
                'failed',
                'cancelled',
                'dead_lettered'
            )
        ),
    attempt_count integer NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND 10),
    max_attempts integer NOT NULL
        CHECK (max_attempts BETWEEN 1 AND 10),
    available_at timestamptz,
    lease_owner_id varchar(200)
        CHECK (
            lease_owner_id IS NULL
            OR length(trim(lease_owner_id)) BETWEEN 1 AND 200
        ),
    lease_token_digest char(64)
        CHECK (
            lease_token_digest IS NULL
            OR lease_token_digest ~ '^[0-9a-f]{64}$'
        ),
    fencing_token bigint NOT NULL DEFAULT 0
        CHECK (fencing_token >= 0),
    lease_acquired_at timestamptz,
    lease_heartbeat_at timestamptz,
    lease_expires_at timestamptz,
    cancel_requested_by varchar(200)
        CHECK (
            cancel_requested_by IS NULL
            OR length(trim(cancel_requested_by)) BETWEEN 1 AND 200
        ),
    cancel_requested_at timestamptz,
    failure_code varchar(64)
        CHECK (
            failure_code IS NULL
            OR failure_code ~ '^[a-z][a-z0-9_]{1,63}$'
        ),
    result_workflow_stage varchar(64)
        CHECK (
            result_workflow_stage IS NULL
            OR result_workflow_stage = 'publication_proposed'
        ),
    result_workflow_revision integer
        CHECK (
            result_workflow_revision IS NULL
            OR result_workflow_revision >= 1
        ),
    result_row_count integer
        CHECK (result_row_count IS NULL OR result_row_count BETWEEN 0 AND 10000),
    result_preview_fingerprint char(64)
        CHECK (
            result_preview_fingerprint IS NULL
            OR result_preview_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    result_rejected_count bigint
        CHECK (
            result_rejected_count IS NULL
            OR result_rejected_count BETWEEN 0 AND 2147483647
        ),
    result_rejection_code_counts jsonb
        CHECK (
            result_rejection_code_counts IS NULL
            OR (
                jsonb_typeof(result_rejection_code_counts) = 'object'
                AND octet_length(result_rejection_code_counts::text) <= 4096
            )
        ),
    result_truncated boolean,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    CONSTRAINT execution_jobs_scope_identity_unique
        UNIQUE (workspace_id, job_id),
    CONSTRAINT execution_jobs_idempotency_unique
        UNIQUE (workspace_id, submitting_actor_id, idempotency_digest),
    CONSTRAINT execution_jobs_workflow_grant_fk
        FOREIGN KEY (
            workflow_workspace_id,
            workflow_id,
            workflow_owner_actor_id
        )
        REFERENCES schemabridge_control.workflow_access_grants (
            workspace_id,
            workflow_id,
            owner_actor_id
        ),
    CONSTRAINT execution_jobs_authorization_time_order
        CHECK (
            authorized_at >= authenticated_at
            AND authorization_expires_at > authorized_at
            AND authorization_expires_at > created_at
        ),
    CONSTRAINT execution_jobs_time_order
        CHECK (
            updated_at >= created_at
            AND (available_at IS NULL OR available_at >= created_at)
            AND (completed_at IS NULL OR completed_at >= created_at)
        ),
    CONSTRAINT execution_jobs_attempt_bound
        CHECK (
            attempt_count <= max_attempts
            AND fencing_token = attempt_count
            AND (
                status NOT IN (
                    'leased',
                    'cancel_requested',
                    'retry_wait',
                    'succeeded',
                    'dead_lettered'
                )
                OR attempt_count >= 1
            )
        ),
    CONSTRAINT execution_jobs_availability_shape
        CHECK (
            (status = 'queued' AND available_at = created_at)
            OR (
                status = 'retry_wait'
                AND available_at IS NOT NULL
                AND available_at > updated_at
            )
            OR (
                status NOT IN ('queued', 'retry_wait')
                AND available_at IS NULL
            )
        ),
    CONSTRAINT execution_jobs_cancel_shape
        CHECK (
            (cancel_requested_by IS NULL) =
            (cancel_requested_at IS NULL)
            AND (
                (
                    status IN ('queued', 'leased', 'retry_wait')
                    AND cancel_requested_at IS NULL
                )
                OR (
                    status IN ('cancel_requested', 'cancelled')
                    AND cancel_requested_at IS NOT NULL
                )
                OR status IN ('succeeded', 'failed', 'dead_lettered')
            )
        ),
    CONSTRAINT execution_jobs_lease_shape
        CHECK (
            (
                status IN ('leased', 'cancel_requested')
                AND attempt_count >= 1
                AND fencing_token >= 1
                AND lease_owner_id IS NOT NULL
                AND lease_token_digest IS NOT NULL
                AND lease_acquired_at IS NOT NULL
                AND lease_heartbeat_at IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND lease_heartbeat_at >= lease_acquired_at
                AND lease_expires_at > lease_heartbeat_at
                AND lease_expires_at
                    <= lease_heartbeat_at + interval '5 minutes'
                AND (
                    (status = 'leased' AND lease_heartbeat_at = updated_at)
                    OR (
                        status = 'cancel_requested'
                        AND lease_heartbeat_at <= updated_at
                    )
                )
            )
            OR
            (
                status NOT IN ('leased', 'cancel_requested')
                AND lease_owner_id IS NULL
                AND lease_token_digest IS NULL
                AND lease_acquired_at IS NULL
                AND lease_heartbeat_at IS NULL
                AND lease_expires_at IS NULL
            )
        ),
    CONSTRAINT execution_jobs_terminal_shape
        CHECK (
            (
                status IN ('succeeded', 'failed', 'cancelled', 'dead_lettered')
                AND completed_at IS NOT NULL
                AND completed_at = updated_at
            )
            OR
            (
                status NOT IN ('succeeded', 'failed', 'cancelled', 'dead_lettered')
                AND completed_at IS NULL
            )
        ),
    CONSTRAINT execution_jobs_result_shape
        CHECK (
            (
                status = 'succeeded'
                AND result_workflow_stage IS NOT NULL
                AND result_workflow_revision IS NOT NULL
                AND result_row_count IS NOT NULL
                AND result_preview_fingerprint IS NOT NULL
                AND result_rejected_count IS NOT NULL
                AND result_rejection_code_counts IS NOT NULL
                AND result_truncated IS NOT NULL
                AND failure_code IS NULL
            )
            OR
            (
                status <> 'succeeded'
                AND result_workflow_stage IS NULL
                AND result_workflow_revision IS NULL
                AND result_row_count IS NULL
                AND result_preview_fingerprint IS NULL
                AND result_rejected_count IS NULL
                AND result_rejection_code_counts IS NULL
                AND result_truncated IS NULL
            )
        ),
    CONSTRAINT execution_jobs_failure_shape
        CHECK (
            (status IN ('retry_wait', 'failed', 'dead_lettered') AND failure_code IS NOT NULL)
            OR
            (status NOT IN ('retry_wait', 'failed', 'dead_lettered') AND failure_code IS NULL)
        ),
    CONSTRAINT execution_jobs_cancellation_status_shape
        CHECK (
            (status = 'cancel_requested' AND cancel_requested_at IS NOT NULL)
            OR status <> 'cancel_requested'
        )
);

CREATE INDEX execution_jobs_claim_idx
    ON schemabridge_control.execution_jobs (
        available_at,
        created_at,
        job_id
    )
    WHERE status IN ('queued', 'retry_wait');

CREATE INDEX execution_jobs_expired_lease_idx
    ON schemabridge_control.execution_jobs (
        lease_expires_at,
        created_at,
        job_id
    )
    WHERE status IN ('leased', 'cancel_requested');

CREATE INDEX execution_jobs_scope_workflow_idx
    ON schemabridge_control.execution_jobs (
        workspace_id,
        workflow_id,
        created_at DESC,
        job_id
    );

CREATE TABLE schemabridge_control.execution_job_events (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id varchar(200) NOT NULL UNIQUE
        CHECK (event_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    job_id varchar(200) NOT NULL
        CHECK (job_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'),
    event_type varchar(32) NOT NULL
        CHECK (
            event_type IN (
                'submitted',
                'claimed',
                'cancel_requested',
                'cancelled',
                'retry_scheduled',
                'succeeded',
                'failed',
                'dead_lettered'
            )
        ),
    status varchar(24) NOT NULL
        CHECK (
            status IN (
                'queued',
                'leased',
                'cancel_requested',
                'retry_wait',
                'succeeded',
                'failed',
                'cancelled',
                'dead_lettered'
            )
        ),
    attempt_count integer NOT NULL CHECK (attempt_count BETWEEN 0 AND 10),
    fencing_token bigint NOT NULL CHECK (fencing_token >= 0),
    reason_code varchar(64)
        CHECK (
            reason_code IS NULL
            OR reason_code ~ '^[a-z][a-z0-9_]{1,63}$'
        ),
    occurred_at timestamptz NOT NULL,
    CONSTRAINT execution_job_events_job_fk
        FOREIGN KEY (workspace_id, job_id)
        REFERENCES schemabridge_control.execution_jobs (workspace_id, job_id),
    CONSTRAINT execution_job_events_transition_unique
        UNIQUE (
            workspace_id,
            job_id,
            event_type,
            attempt_count,
            fencing_token
        )
);

CREATE INDEX execution_job_events_job_sequence_idx
    ON schemabridge_control.execution_job_events (
        workspace_id,
        job_id,
        sequence
    );

CREATE FUNCTION schemabridge_control.enforce_execution_job_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    rejection_key text;
    rejection_value jsonb;
    rejection_entries integer;
    rejection_total bigint := 0;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'execution jobs cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
            RAISE EXCEPTION 'only the API may submit execution jobs'
                USING ERRCODE = '42501';
        END IF;
        IF (
            NEW.status <> 'queued'
            OR NEW.attempt_count <> 0
            OR NEW.fencing_token <> 0
            OR NEW.available_at <> NEW.created_at
            OR NEW.updated_at <> NEW.created_at
            OR NEW.cancel_requested_at IS NOT NULL
            OR NEW.failure_code IS NOT NULL
            OR NEW.completed_at IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'new execution job state is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF (
        NEW.job_id IS DISTINCT FROM OLD.job_id
        OR NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.workflow_workspace_id IS DISTINCT FROM OLD.workflow_workspace_id
        OR NEW.workflow_id IS DISTINCT FROM OLD.workflow_id
        OR NEW.workflow_owner_actor_id IS DISTINCT FROM OLD.workflow_owner_actor_id
        OR NEW.submitting_actor_id IS DISTINCT FROM OLD.submitting_actor_id
        OR NEW.workflow_access_scope IS DISTINCT FROM OLD.workflow_access_scope
        OR NEW.authorized_operation IS DISTINCT FROM OLD.authorized_operation
        OR NEW.expected_workflow_revision IS DISTINCT FROM OLD.expected_workflow_revision
        OR NEW.expected_plan_fingerprint IS DISTINCT FROM OLD.expected_plan_fingerprint
        OR NEW.authenticated_at IS DISTINCT FROM OLD.authenticated_at
        OR NEW.authorized_at IS DISTINCT FROM OLD.authorized_at
        OR NEW.authorization_expires_at IS DISTINCT FROM OLD.authorization_expires_at
        OR NEW.payload_fingerprint IS DISTINCT FROM OLD.payload_fingerprint
        OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
        OR NEW.idempotency_digest IS DISTINCT FROM OLD.idempotency_digest
        OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    ) THEN
        RAISE EXCEPTION 'execution job authorization is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.status IN ('succeeded', 'failed', 'cancelled', 'dead_lettered') THEN
        RAISE EXCEPTION 'terminal execution jobs are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF SESSION_USER = 'schemabridge_api' THEN
        IF NOT (
            (OLD.status IN ('queued', 'retry_wait') AND NEW.status = 'cancelled')
            OR (OLD.status = 'leased' AND NEW.status = 'cancel_requested')
        ) THEN
            RAISE EXCEPTION 'API execution job transition is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSIF SESSION_USER IN ('schemabridge_worker', 'schemabridge_migrator') THEN
        IF NOT (
            (OLD.status IN ('queued', 'retry_wait') AND NEW.status = 'leased')
            OR (OLD.status = 'leased' AND NEW.status = 'leased')
            OR (
                OLD.status = 'leased'
                AND NEW.status IN (
                    'succeeded',
                    'failed',
                    'retry_wait',
                    'dead_lettered'
                )
            )
            OR (
                OLD.status = 'cancel_requested'
                AND NEW.status IN (
                    'succeeded',
                    'failed',
                    'cancelled',
                    'dead_lettered'
                )
            )
            OR (
                OLD.status IN ('queued', 'retry_wait')
                AND NEW.status = 'failed'
                AND NEW.failure_code = 'authorization_expired'
                AND OLD.authorization_expires_at <= clock_timestamp()
            )
            OR (
                OLD.status = 'cancel_requested'
                AND NEW.status = 'cancel_requested'
            )
        ) THEN
            RAISE EXCEPTION 'worker execution job transition is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSE
        RAISE EXCEPTION 'execution job role is invalid'
            USING ERRCODE = '42501';
    END IF;

    IF NEW.attempt_count < OLD.attempt_count
        OR NEW.fencing_token < OLD.fencing_token THEN
        RAISE EXCEPTION 'execution job counters cannot decrease'
            USING ERRCODE = '55000';
    END IF;

    IF (
        (
            NEW.attempt_count IS DISTINCT FROM OLD.attempt_count
            OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
        )
        AND NOT (
            (
                OLD.status IN ('queued', 'retry_wait')
                AND NEW.status = 'leased'
            )
            OR (
                OLD.status = NEW.status
                AND OLD.status IN ('leased', 'cancel_requested')
                AND OLD.lease_expires_at <= clock_timestamp()
            )
        )
    ) THEN
        RAISE EXCEPTION 'execution job counters may advance only during claim or reclaim'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.status = 'leased' AND OLD.status <> 'leased' THEN
        IF (
            NEW.attempt_count <> OLD.attempt_count + 1
            OR NEW.fencing_token <> OLD.fencing_token + 1
        ) THEN
            RAISE EXCEPTION 'execution job claim counters are invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSIF (
        NEW.status IN ('leased', 'cancel_requested')
        AND OLD.status = NEW.status
    ) THEN
        IF (
            NEW.attempt_count = OLD.attempt_count
            AND NEW.fencing_token = OLD.fencing_token
            AND NEW.lease_owner_id IS NOT DISTINCT FROM OLD.lease_owner_id
            AND NEW.lease_token_digest IS NOT DISTINCT FROM OLD.lease_token_digest
            AND NEW.lease_acquired_at IS NOT DISTINCT FROM OLD.lease_acquired_at
            AND NEW.lease_heartbeat_at >= OLD.lease_heartbeat_at
            AND NEW.lease_expires_at > OLD.lease_expires_at
        ) THEN
            NULL;
        ELSIF (
            OLD.lease_expires_at <= clock_timestamp()
            AND NEW.attempt_count = OLD.attempt_count + 1
            AND NEW.fencing_token = OLD.fencing_token + 1
        ) THEN
            NULL;
        ELSE
            RAISE EXCEPTION 'execution job lease renewal or reclaim is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    IF NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'execution job update time cannot decrease'
            USING ERRCODE = '55000';
    END IF;

    IF (
        NEW.cancel_requested_at IS NOT NULL
        AND (
            NEW.cancel_requested_by <> OLD.submitting_actor_id
            OR NEW.cancel_requested_at <> NEW.updated_at
        )
        AND OLD.cancel_requested_at IS NULL
    ) THEN
        RAISE EXCEPTION 'execution job cancellation evidence is invalid'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.status = 'retry_wait' THEN
        IF (
            NEW.failure_code NOT IN (
                'registry_unavailable',
                'source_unavailable',
                'source_timeout'
            )
            OR NEW.attempt_count >= NEW.max_attempts
            OR NEW.available_at <> (
                NEW.updated_at
                + LEAST(
                    5 * power(2, NEW.attempt_count - 1),
                    300
                ) * interval '1 second'
            )
        ) THEN
            RAISE EXCEPTION 'execution job retry state is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSIF NEW.status = 'failed' THEN
        IF (
            NEW.failure_code = 'authorization_expired'
            AND (
                OLD.authorization_expires_at > clock_timestamp()
                OR NEW.updated_at < OLD.authorization_expires_at
            )
        ) THEN
            RAISE EXCEPTION 'execution job authorization has not expired'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.failure_code IN (
            'registry_unavailable',
            'source_unavailable',
            'source_timeout',
            'ambiguous_external_effect',
            'unexpected_worker_failure'
        ) THEN
            RAISE EXCEPTION 'execution job terminal failure classification is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSIF NEW.status = 'dead_lettered' THEN
        IF NOT (
            NEW.failure_code IN (
                'ambiguous_external_effect',
                'unexpected_worker_failure'
            )
            OR (
                NEW.failure_code IN (
                    'registry_unavailable',
                    'source_unavailable',
                    'source_timeout'
                )
                AND NEW.attempt_count >= NEW.max_attempts
            )
        ) THEN
            RAISE EXCEPTION 'execution job dead-letter classification is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    IF (
        NEW.status = 'succeeded'
        AND NEW.result_workflow_revision < NEW.expected_workflow_revision
    ) THEN
        RAISE EXCEPTION 'execution job result revision is stale'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.result_rejection_code_counts IS NOT NULL THEN
        SELECT count(*)
        INTO rejection_entries
        FROM jsonb_object_keys(NEW.result_rejection_code_counts);
        IF rejection_entries > 64 THEN
            RAISE EXCEPTION 'execution job rejection counts are too large'
                USING ERRCODE = '55000';
        END IF;
        FOR rejection_key, rejection_value IN
            SELECT key, value FROM jsonb_each(NEW.result_rejection_code_counts)
        LOOP
            IF (
                rejection_key !~ '^[a-z][a-z0-9_]{1,63}$'
                OR jsonb_typeof(rejection_value) <> 'number'
                OR (rejection_value::text)::numeric < 1
                OR (
                    rejection_key = 'unclassified_rejections'
                    AND (rejection_value::text)::numeric > 2147483647
                )
                OR (
                    rejection_key <> 'unclassified_rejections'
                    AND (rejection_value::text)::numeric > 10000
                )
                OR trunc((rejection_value::text)::numeric)
                    <> (rejection_value::text)::numeric
            ) THEN
                RAISE EXCEPTION 'execution job rejection counts are invalid'
                    USING ERRCODE = '55000';
            END IF;
            rejection_total := rejection_total + (rejection_value::text)::bigint;
        END LOOP;
        IF rejection_total <> NEW.result_rejected_count THEN
            RAISE EXCEPTION 'execution job rejection total is invalid'
                USING ERRCODE = '55000';
        END IF;
        IF (
            NEW.result_rejection_code_counts ? 'unclassified_rejections'
            AND NEW.result_truncated IS NOT TRUE
        ) THEN
            RAISE EXCEPTION 'execution job truncated rejection summary is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION
    schemabridge_control.enforce_execution_job_lifecycle()
    FROM PUBLIC;

CREATE FUNCTION schemabridge_control.enforce_execution_job_event_append()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_status varchar(24);
    current_attempt integer;
    current_fence bigint;
    current_updated_at timestamptz;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'execution job events are append-only'
            USING ERRCODE = '55000';
    END IF;

    IF SESSION_USER = 'schemabridge_api' THEN
        IF NEW.event_type NOT IN ('submitted', 'cancel_requested', 'cancelled') THEN
            RAISE EXCEPTION 'API execution job event type is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSIF SESSION_USER = 'schemabridge_worker' THEN
        IF NEW.event_type NOT IN (
            'claimed',
            'cancelled',
            'retry_scheduled',
            'succeeded',
            'failed',
            'dead_lettered'
        ) THEN
            RAISE EXCEPTION 'worker execution job event type is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSIF SESSION_USER <> 'schemabridge_migrator' THEN
        RAISE EXCEPTION 'execution job event role is invalid'
            USING ERRCODE = '42501';
    END IF;

    SELECT status, attempt_count, fencing_token, updated_at
    INTO current_status, current_attempt, current_fence, current_updated_at
    FROM schemabridge_control.execution_jobs
    WHERE workspace_id = NEW.workspace_id
      AND job_id = NEW.job_id;

    IF (
        current_status IS NULL
        OR NEW.status <> current_status
        OR NEW.attempt_count <> current_attempt
        OR NEW.fencing_token <> current_fence
        OR NEW.occurred_at <> current_updated_at
    ) THEN
        RAISE EXCEPTION 'execution job event does not match current state'
            USING ERRCODE = '55000';
    END IF;

    IF NOT (
        (NEW.event_type = 'submitted' AND NEW.status = 'queued')
        OR (
            NEW.event_type = 'claimed'
            AND NEW.status IN ('leased', 'cancel_requested')
        )
        OR (
            NEW.event_type = 'cancel_requested'
            AND NEW.status = 'cancel_requested'
        )
        OR (NEW.event_type = 'cancelled' AND NEW.status = 'cancelled')
        OR (
            NEW.event_type = 'retry_scheduled'
            AND NEW.status = 'retry_wait'
        )
        OR (NEW.event_type = 'succeeded' AND NEW.status = 'succeeded')
        OR (NEW.event_type = 'failed' AND NEW.status = 'failed')
        OR (
            NEW.event_type = 'dead_lettered'
            AND NEW.status = 'dead_lettered'
        )
    ) THEN
        RAISE EXCEPTION 'execution job event type does not match state'
            USING ERRCODE = '55000';
    END IF;

    IF (
        (NEW.event_type IN ('retry_scheduled', 'failed', 'dead_lettered')
         AND NEW.reason_code IS NULL)
        OR
        (NEW.event_type NOT IN ('retry_scheduled', 'failed', 'dead_lettered')
         AND NEW.reason_code IS NOT NULL)
    ) THEN
        RAISE EXCEPTION 'execution job event reason is invalid'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION
    schemabridge_control.enforce_execution_job_event_append()
    FROM PUBLIC;

CREATE TRIGGER execution_jobs_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE
    ON schemabridge_control.execution_jobs
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.enforce_execution_job_lifecycle();

CREATE TRIGGER execution_job_events_append_only
    BEFORE INSERT OR UPDATE OR DELETE
    ON schemabridge_control.execution_job_events
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.enforce_execution_job_event_append();

REVOKE ALL ON
    schemabridge_control.execution_jobs,
    schemabridge_control.execution_job_events
    FROM PUBLIC;

REVOKE ALL ON SEQUENCE
    schemabridge_control.execution_job_events_sequence_seq
    FROM PUBLIC;

GRANT SELECT ON schemabridge_control.schema_migrations
    TO schemabridge_api, schemabridge_worker;

GRANT SELECT ON
    schemabridge_control.agent_workflow_drafts,
    schemabridge_control.workflow_access_grants,
    schemabridge_control.identity_bindings,
    schemabridge_control.identity_rotation_plans,
    schemabridge_control.identity_rotation_bindings
    TO schemabridge_api, schemabridge_worker;

GRANT SELECT ON
    schemabridge_control.registry_active_pointers
    TO schemabridge_worker;

GRANT UPDATE (
    revision,
    payload,
    execution_row_count,
    execution_preview_fingerprint,
    updated_at
) ON schemabridge_control.agent_workflow_drafts
    TO schemabridge_worker;

GRANT SELECT ON
    schemabridge_control.execution_jobs,
    schemabridge_control.execution_job_events
    TO schemabridge_api, schemabridge_worker;

GRANT INSERT (
    job_id,
    kind,
    workspace_id,
    workflow_workspace_id,
    workflow_id,
    workflow_owner_actor_id,
    submitting_actor_id,
    workflow_access_scope,
    authorized_operation,
    expected_workflow_revision,
    expected_plan_fingerprint,
    authenticated_at,
    authorized_at,
    authorization_expires_at,
    payload_fingerprint,
    request_fingerprint,
    idempotency_digest,
    status,
    attempt_count,
    max_attempts,
    available_at,
    fencing_token,
    created_at,
    updated_at
) ON schemabridge_control.execution_jobs
    TO schemabridge_api;

GRANT UPDATE (
    status,
    available_at,
    cancel_requested_by,
    cancel_requested_at,
    failure_code,
    updated_at,
    completed_at
) ON schemabridge_control.execution_jobs
    TO schemabridge_api;

GRANT UPDATE (
    status,
    attempt_count,
    available_at,
    lease_owner_id,
    lease_token_digest,
    fencing_token,
    lease_acquired_at,
    lease_heartbeat_at,
    lease_expires_at,
    failure_code,
    result_workflow_stage,
    result_workflow_revision,
    result_row_count,
    result_preview_fingerprint,
    result_rejected_count,
    result_rejection_code_counts,
    result_truncated,
    updated_at,
    completed_at
) ON schemabridge_control.execution_jobs
    TO schemabridge_worker;

GRANT INSERT (
    event_id,
    workspace_id,
    job_id,
    event_type,
    status,
    attempt_count,
    fencing_token,
    reason_code,
    occurred_at
) ON schemabridge_control.execution_job_events
    TO schemabridge_api, schemabridge_worker;

GRANT USAGE ON SEQUENCE
    schemabridge_control.execution_job_events_sequence_seq
    TO schemabridge_api, schemabridge_worker;
