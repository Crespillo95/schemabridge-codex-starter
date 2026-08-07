GRANT USAGE ON SCHEMA schemabridge_control
    TO schemabridge_publisher;

DO $$
DECLARE
    publisher_role record;
BEGIN
    SELECT rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
           rolreplication, rolbypassrls
    INTO publisher_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'schemabridge_publisher';
    IF NOT FOUND OR (
        publisher_role.rolcanlogin IS NOT TRUE
        OR publisher_role.rolsuper
        OR publisher_role.rolinherit
        OR publisher_role.rolcreaterole
        OR publisher_role.rolcreatedb
        OR publisher_role.rolreplication
        OR publisher_role.rolbypassrls
        OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_auth_members AS membership
            JOIN pg_catalog.pg_roles AS parent
              ON parent.oid = membership.roleid
            JOIN pg_catalog.pg_roles AS child
              ON child.oid = membership.member
            WHERE parent.rolname = 'schemabridge_publisher'
               OR child.rolname = 'schemabridge_publisher'
        )
    ) THEN
        RAISE EXCEPTION 'schemabridge_publisher role posture is invalid'
            USING ERRCODE = '42501';
    END IF;
END;
$$;

CREATE TABLE schemabridge_control.registry_publication_jobs (
    job_id varchar(200) PRIMARY KEY
        CHECK (job_id ~ '^registry-publication-[0-9a-f]{64}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 3 AND 200),
    catalog_scope varchar(120) NOT NULL
        CHECK (catalog_scope ~ '^[a-z][a-z0-9_.:-]{2,119}$'),
    registry_id varchar(80) NOT NULL
        CHECK (registry_id ~ '^[a-z][a-z0-9_]{2,79}$'),
    target_version bigint NOT NULL CHECK (target_version >= 1),
    proposal_id varchar(200) NOT NULL
        CHECK (length(trim(proposal_id)) BETWEEN 3 AND 200),
    proposal_fingerprint char(64) NOT NULL
        CHECK (proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    submitted_by varchar(200) NOT NULL
        CHECK (length(trim(submitted_by)) BETWEEN 1 AND 200),
    idempotency_digest char(64) NOT NULL
        CHECK (idempotency_digest ~ '^[0-9a-f]{64}$'),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    status varchar(24) NOT NULL
        CHECK (
            status IN (
                'queued',
                'leased',
                'awaiting_approval',
                'approved',
                'retry_wait',
                'cancel_requested',
                'activation_ready',
                'failed',
                'cancelled',
                'dead_lettered'
            )
        ),
    revision bigint NOT NULL CHECK (revision >= 1),
    attempt_count integer NOT NULL CHECK (attempt_count BETWEEN 0 AND 10),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    fencing_token bigint NOT NULL CHECK (fencing_token >= 0),
    available_at timestamptz NOT NULL,
    lease_owner_id varchar(200)
        CHECK (
            lease_owner_id IS NULL
            OR lease_owner_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        ),
    lease_capability_digest char(64)
        CHECK (
            lease_capability_digest IS NULL
            OR lease_capability_digest ~ '^[0-9a-f]{64}$'
        ),
    lease_acquired_at timestamptz,
    lease_heartbeat_at timestamptz,
    lease_expires_at timestamptz,
    candidate_fingerprint char(64)
        CHECK (
            candidate_fingerprint IS NULL
            OR candidate_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    authorization_id varchar(200)
        CHECK (
            authorization_id IS NULL
            OR authorization_id ~ '^registry-authorization-[0-9a-f]{64}$'
        ),
    observed_authorization_id varchar(200)
        CHECK (
            observed_authorization_id IS NULL
            OR observed_authorization_id ~ '^registry-authorization-[0-9a-f]{64}$'
        ),
    failure_code varchar(64)
        CHECK (
            failure_code IS NULL
            OR failure_code IN (
                'proposal_unavailable',
                'proposal_stale',
                'catalog_stale',
                'base_unavailable',
                'base_stale',
                'candidate_invalid',
                'authorization_expired',
                'authorization_mismatch',
                'datahub_unavailable',
                'datahub_permission_denied',
                'target_conflict',
                'readback_required',
                'unexpected_worker_failure'
            )
        ),
    cancel_requested_at timestamptz,
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 16777216
            AND payload->>'id' = job_id
            AND payload#>>'{scope,workspace_id}' = workspace_id
            AND payload#>>'{scope,catalog_scope}' = catalog_scope
            AND payload#>>'{scope,registry_id}' = registry_id
            AND (payload#>>'{proposal,target_registry_version}')::bigint = target_version
            AND payload#>>'{proposal,id}' = proposal_id
            AND payload#>>'{proposal,fingerprint}' = proposal_fingerprint
            AND payload->>'submitted_by' = submitted_by
            AND payload->>'idempotency_digest' = idempotency_digest
            AND payload->>'request_fingerprint' = request_fingerprint
            AND payload->>'status' = status
            AND (payload->>'revision')::bigint = revision
            AND (payload->>'attempts')::integer = attempt_count
            AND (payload->>'max_attempts')::integer = max_attempts
            AND (payload->>'last_fencing_token')::bigint = fencing_token
            AND coalesce(payload#>>'{candidate,fingerprint}', '')
                = coalesce(candidate_fingerprint::text, '')
            AND coalesce(payload#>>'{authorization,id}', '')
                = coalesce(authorization_id, '')
            AND coalesce(payload#>>'{receipt,observed_authorization_id}', '')
                = coalesce(observed_authorization_id, '')
            AND coalesce(payload->>'failure_code', '')
                = coalesce(failure_code, '')
            AND (payload->>'submitted_at')::timestamptz = submitted_at
            AND (payload->>'available_at')::timestamptz = available_at
            AND (payload->>'updated_at')::timestamptz = updated_at
            AND coalesce((payload->>'cancel_requested_at')::timestamptz, '-infinity')
                = coalesce(cancel_requested_at, '-infinity')
            AND coalesce(payload#>>'{lease,worker_id}', '')
                = coalesce(lease_owner_id, '')
            AND coalesce(payload#>>'{lease,token_digest}', '')
                = coalesce(lease_capability_digest::text, '')
            AND coalesce((payload#>>'{lease,acquired_at}')::timestamptz, '-infinity')
                = coalesce(lease_acquired_at, '-infinity')
            AND coalesce((payload#>>'{lease,heartbeat_at}')::timestamptz, '-infinity')
                = coalesce(lease_heartbeat_at, '-infinity')
            AND coalesce((payload#>>'{lease,expires_at}')::timestamptz, '-infinity')
                = coalesce(lease_expires_at, '-infinity')
        ),
    submitted_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    CONSTRAINT registry_publication_target_unique
        UNIQUE (workspace_id, catalog_scope, registry_id, target_version),
    CONSTRAINT registry_publication_workspace_job_unique
        UNIQUE (workspace_id, job_id),
    CONSTRAINT registry_publication_idempotency_unique
        UNIQUE (workspace_id, submitted_by, idempotency_digest),
    CONSTRAINT registry_publication_proposal_fk
        FOREIGN KEY (workspace_id, proposal_id)
        REFERENCES schemabridge_control.semantic_onboarding_proposals (
            workspace_id,
            proposal_id
        ),
    CONSTRAINT registry_publication_time_order
        CHECK (
            updated_at >= submitted_at
            AND available_at >= submitted_at
            AND (completed_at IS NULL OR completed_at >= submitted_at)
        ),
    CONSTRAINT registry_publication_counter_shape
        CHECK (
            attempt_count <= max_attempts
            AND fencing_token = attempt_count
            AND (attempt_count >= 1 OR status IN ('queued', 'cancelled'))
        ),
    CONSTRAINT registry_publication_lease_shape
        CHECK (
            (
                status IN ('leased', 'cancel_requested')
                AND lease_owner_id IS NOT NULL
                AND lease_capability_digest IS NOT NULL
                AND lease_acquired_at IS NOT NULL
                AND lease_heartbeat_at IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND lease_acquired_at <= lease_heartbeat_at
                AND lease_heartbeat_at < lease_expires_at
                AND lease_expires_at <= lease_heartbeat_at + interval '5 minutes'
            )
            OR
            (
                status NOT IN ('leased', 'cancel_requested')
                AND lease_owner_id IS NULL
                AND lease_capability_digest IS NULL
                AND lease_acquired_at IS NULL
                AND lease_heartbeat_at IS NULL
                AND lease_expires_at IS NULL
            )
        ),
    CONSTRAINT registry_publication_candidate_shape
        CHECK (
            (
                status = 'queued'
                AND candidate_fingerprint IS NULL
                AND authorization_id IS NULL
            )
            OR (
                status IN ('leased', 'cancel_requested', 'retry_wait', 'failed', 'dead_lettered')
                AND (
                    (
                        candidate_fingerprint IS NULL
                        AND authorization_id IS NULL
                    )
                    OR (
                        candidate_fingerprint IS NOT NULL
                        AND authorization_id IS NOT NULL
                    )
                )
            )
            OR (
                status = 'awaiting_approval'
                AND candidate_fingerprint IS NOT NULL
                AND authorization_id IS NULL
            )
            OR (
                status IN ('approved', 'activation_ready')
                AND candidate_fingerprint IS NOT NULL
                AND authorization_id IS NOT NULL
            )
            OR (
                status = 'cancelled'
                AND authorization_id IS NULL
            )
        ),
    CONSTRAINT registry_publication_receipt_shape
        CHECK (
            (status = 'activation_ready')
                = (observed_authorization_id IS NOT NULL)
        ),
    CONSTRAINT registry_publication_failure_shape
        CHECK (
            (status IN ('retry_wait', 'failed', 'dead_lettered'))
                = (failure_code IS NOT NULL)
        ),
    CONSTRAINT registry_publication_cancel_shape
        CHECK (
            (status <> 'cancel_requested' OR cancel_requested_at IS NOT NULL)
            AND (
                cancel_requested_at IS NULL
                OR status = 'cancel_requested'
                OR (
                    status = 'retry_wait'
                    AND failure_code = 'readback_required'
                    AND candidate_fingerprint IS NOT NULL
                )
            )
        ),
    CONSTRAINT registry_publication_completed_shape
        CHECK (
            (status IN ('activation_ready', 'failed', 'cancelled', 'dead_lettered'))
                = (completed_at IS NOT NULL)
            AND (
                completed_at IS NULL
                OR completed_at = updated_at
            )
        )
);

CREATE INDEX registry_publication_claim_idx
    ON schemabridge_control.registry_publication_jobs (
        status,
        available_at,
        submitted_at,
        job_id
    )
    WHERE status IN ('queued', 'approved', 'retry_wait');

CREATE INDEX registry_publication_expired_lease_idx
    ON schemabridge_control.registry_publication_jobs (
        lease_expires_at,
        submitted_at,
        job_id
    )
    WHERE status IN ('leased', 'cancel_requested');

CREATE INDEX registry_publication_workspace_updated_idx
    ON schemabridge_control.registry_publication_jobs (
        workspace_id,
        updated_at DESC,
        job_id DESC
    );

CREATE OR REPLACE VIEW schemabridge_control.operational_queue_snapshot (
    queue,
    depth,
    oldest_due_age_seconds
)
WITH (
    security_barrier = true,
    security_invoker = false
)
AS
SELECT
    'execution'::text AS queue,
    pg_catalog.count(*)::bigint AS depth,
    COALESCE(
        GREATEST(
            0::numeric,
            pg_catalog.floor(
                EXTRACT(
                    EPOCH FROM (
                        pg_catalog.statement_timestamp()
                        - pg_catalog.min(available_at) FILTER (
                            WHERE available_at <= pg_catalog.statement_timestamp()
                        )
                    )
                )
            )
        )::bigint,
        0::bigint
    ) AS oldest_due_age_seconds
FROM schemabridge_control.execution_jobs
WHERE status IN ('queued', 'retry_wait')

UNION ALL

SELECT
    'catalog'::text AS queue,
    pg_catalog.count(*)::bigint AS depth,
    COALESCE(
        GREATEST(
            0::numeric,
            pg_catalog.floor(
                EXTRACT(
                    EPOCH FROM (
                        pg_catalog.statement_timestamp()
                        - pg_catalog.min(requested_at) FILTER (
                            WHERE requested_at <= pg_catalog.statement_timestamp()
                        )
                    )
                )
            )
        )::bigint,
        0::bigint
    ) AS oldest_due_age_seconds
FROM schemabridge_control.catalog_refresh_runs
WHERE status = 'requested'

UNION ALL

SELECT
    'profile'::text AS queue,
    pg_catalog.count(*)::bigint AS depth,
    COALESCE(
        GREATEST(
            0::numeric,
            pg_catalog.floor(
                EXTRACT(
                    EPOCH FROM (
                        pg_catalog.statement_timestamp()
                        - pg_catalog.min(available_at) FILTER (
                            WHERE available_at <= pg_catalog.statement_timestamp()
                        )
                    )
                )
            )
        )::bigint,
        0::bigint
    ) AS oldest_due_age_seconds
FROM schemabridge_control.semantic_join_profile_jobs
WHERE status IN ('requested', 'retry_wait')

UNION ALL

SELECT
    'publication'::text AS queue,
    pg_catalog.count(*)::bigint AS depth,
    COALESCE(
        GREATEST(
            0::numeric,
            pg_catalog.floor(
                EXTRACT(
                    EPOCH FROM (
                        pg_catalog.statement_timestamp()
                        - pg_catalog.min(available_at) FILTER (
                            WHERE available_at <= pg_catalog.statement_timestamp()
                        )
                    )
                )
            )
        )::bigint,
        0::bigint
    ) AS oldest_due_age_seconds
FROM schemabridge_control.registry_publication_jobs
WHERE status IN ('queued', 'approved', 'retry_wait')

UNION ALL

SELECT
    'reconciliation'::text AS queue,
    pg_catalog.count(*)::bigint AS depth,
    COALESCE(
        GREATEST(
            0::numeric,
            pg_catalog.floor(
                EXTRACT(
                    EPOCH FROM (
                        pg_catalog.statement_timestamp()
                        - pg_catalog.min(available_at) FILTER (
                            WHERE available_at <= pg_catalog.statement_timestamp()
                        )
                    )
                )
            )
        )::bigint,
        0::bigint
    ) AS oldest_due_age_seconds
FROM schemabridge_control.semantic_change_scan_requests
WHERE status IN ('requested', 'retry_wait');

CREATE TABLE schemabridge_control.registry_publication_events (
    sequence bigserial PRIMARY KEY,
    event_id varchar(200) NOT NULL UNIQUE
        CHECK (event_id ~ '^registry-publication-event-[0-9a-f]{64}$'),
    workspace_id varchar(200) NOT NULL,
    job_id varchar(200) NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    event_kind varchar(32) NOT NULL
        CHECK (
            event_kind IN (
                'submitted',
                'leased',
                'recovery_leased',
                'candidate_ready',
                'authorized',
                'authorization_expired',
                'cancel_requested',
                'cancelled',
                'retry_scheduled',
                'activation_ready',
                'failed',
                'dead_lettered'
            )
        ),
    status varchar(24) NOT NULL,
    actor_id varchar(200)
        CHECK (actor_id IS NULL OR length(trim(actor_id)) BETWEEN 1 AND 200),
    worker_id varchar(200)
        CHECK (
            worker_id IS NULL
            OR worker_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        ),
    failure_code varchar(64),
    occurred_at timestamptz NOT NULL,
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 65536
            AND payload->>'id' = event_id
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'job_id' = job_id
            AND (payload->>'revision')::bigint = revision
            AND payload->>'kind' = event_kind
            AND payload->>'status' = status
            AND coalesce(payload->>'actor_id', '') = coalesce(actor_id, '')
            AND coalesce(payload->>'worker_id', '') = coalesce(worker_id, '')
            AND coalesce(payload->>'failure_code', '') = coalesce(failure_code, '')
        ),
    CONSTRAINT registry_publication_event_job_fk
        FOREIGN KEY (workspace_id, job_id)
        REFERENCES schemabridge_control.registry_publication_jobs (
            workspace_id,
            job_id
        ),
    UNIQUE (workspace_id, job_id, revision)
);

CREATE INDEX registry_publication_events_job_idx
    ON schemabridge_control.registry_publication_events (
        workspace_id,
        job_id,
        sequence
    );

CREATE FUNCTION schemabridge_control.enforce_registry_publication_job_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    proposal_record record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'registry publication jobs cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
            RAISE EXCEPTION 'only the API may submit registry publication jobs'
                USING ERRCODE = '42501';
        END IF;
        IF (
            NEW.status <> 'queued'
            OR NEW.revision <> 1
            OR NEW.attempt_count <> 0
            OR NEW.fencing_token <> 0
            OR NEW.available_at <> NEW.submitted_at
            OR NEW.updated_at <> NEW.submitted_at
        ) THEN
            RAISE EXCEPTION 'new registry publication job state is invalid'
                USING ERRCODE = '55000';
        END IF;
        SELECT proposal.fingerprint,
               proposal.target_registry_version,
               proposal.payload,
               proposal.payload#>>'{scope,catalog_scope}' AS catalog_scope,
               proposal.payload#>>'{scope,registry_id}' AS registry_id,
               draft.status AS draft_status,
               draft.prepared_proposal_id,
               draft.prepared_proposal_fingerprint
        INTO proposal_record
        FROM schemabridge_control.semantic_onboarding_proposals AS proposal
        JOIN schemabridge_control.semantic_onboarding_drafts AS draft
          ON draft.workspace_id = proposal.workspace_id
         AND draft.draft_id = proposal.draft_id
        WHERE proposal.workspace_id = NEW.workspace_id
          AND proposal.proposal_id = NEW.proposal_id
        FOR SHARE OF draft;
        IF NOT FOUND OR (
            proposal_record.fingerprint <> NEW.proposal_fingerprint
            OR proposal_record.target_registry_version <> NEW.target_version
            OR proposal_record.payload IS DISTINCT FROM NEW.payload->'proposal'
            OR proposal_record.catalog_scope <> NEW.catalog_scope
            OR proposal_record.registry_id <> NEW.registry_id
            OR proposal_record.draft_status <> 'ready_for_publication'
            OR proposal_record.prepared_proposal_id <> NEW.proposal_id
            OR proposal_record.prepared_proposal_fingerprint <> NEW.proposal_fingerprint
        ) THEN
            RAISE EXCEPTION 'registry publication proposal authority is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF (
        NEW.job_id IS DISTINCT FROM OLD.job_id
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.catalog_scope IS DISTINCT FROM OLD.catalog_scope
        OR NEW.registry_id IS DISTINCT FROM OLD.registry_id
        OR NEW.target_version IS DISTINCT FROM OLD.target_version
        OR NEW.proposal_id IS DISTINCT FROM OLD.proposal_id
        OR NEW.proposal_fingerprint IS DISTINCT FROM OLD.proposal_fingerprint
        OR NEW.submitted_by IS DISTINCT FROM OLD.submitted_by
        OR NEW.idempotency_digest IS DISTINCT FROM OLD.idempotency_digest
        OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
        OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
        OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at
        OR NEW.payload->'proposal' IS DISTINCT FROM OLD.payload->'proposal'
    ) THEN
        RAISE EXCEPTION 'registry publication target reservation is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status IN ('activation_ready', 'failed', 'cancelled', 'dead_lettered') THEN
        RAISE EXCEPTION 'terminal registry publication jobs are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.revision <> OLD.revision + 1 OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'registry publication revision is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF (
        OLD.candidate_fingerprint IS NOT NULL
        AND NEW.candidate_fingerprint IS DISTINCT FROM OLD.candidate_fingerprint
    ) OR (
        OLD.candidate_fingerprint IS NULL
        AND NEW.candidate_fingerprint IS NOT NULL
        AND NOT (
            SESSION_USER IN ('schemabridge_publisher', 'schemabridge_migrator')
            AND OLD.status = 'leased'
            AND NEW.status = 'awaiting_approval'
        )
    ) THEN
        RAISE EXCEPTION 'registry publication candidate identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF (
        OLD.payload->'candidate' IS DISTINCT FROM 'null'::jsonb
        AND NEW.payload->'candidate' IS DISTINCT FROM OLD.payload->'candidate'
    ) THEN
        RAISE EXCEPTION 'registry publication candidate payload is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF (
        OLD.payload->'authorization' IS DISTINCT FROM 'null'::jsonb
        AND NEW.payload->'authorization' IS DISTINCT FROM 'null'::jsonb
        AND NEW.payload->'authorization' IS DISTINCT FROM OLD.payload->'authorization'
    ) THEN
        RAISE EXCEPTION 'registry publication authorization payload is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF SESSION_USER = 'schemabridge_api' THEN
        IF NOT (
            (OLD.status = 'awaiting_approval' AND NEW.status = 'approved')
            OR (
                OLD.status IN ('queued', 'awaiting_approval', 'approved', 'retry_wait')
                AND NEW.status = 'cancelled'
                AND (
                    OLD.status <> 'retry_wait'
                    OR OLD.cancel_requested_at IS NULL
                )
            )
            OR (OLD.status = 'leased' AND NEW.status = 'cancel_requested')
            OR (
                OLD.status = 'cancel_requested'
                AND NEW.status = 'cancel_requested'
                AND NEW.payload = OLD.payload
            )
        ) THEN
            RAISE EXCEPTION 'API registry publication transition is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSIF SESSION_USER IN ('schemabridge_publisher', 'schemabridge_migrator') THEN
        IF NOT (
            (OLD.status IN ('queued', 'approved') AND NEW.status = 'leased')
            OR (
                OLD.status = 'retry_wait'
                AND (
                    (OLD.cancel_requested_at IS NULL AND NEW.status = 'leased')
                    OR (
                        OLD.cancel_requested_at IS NOT NULL
                        AND NEW.status = 'cancel_requested'
                    )
                )
            )
            OR (OLD.status IN ('leased', 'cancel_requested') AND NEW.status = OLD.status)
            OR (
                OLD.status = 'leased'
                AND NEW.status IN (
                    'awaiting_approval',
                    'retry_wait',
                    'activation_ready',
                    'failed',
                    'dead_lettered'
                )
            )
            OR (
                OLD.status = 'cancel_requested'
                AND NEW.status IN (
                    'retry_wait',
                    'activation_ready',
                    'cancelled',
                    'failed',
                    'dead_lettered'
                )
            )
        ) THEN
            RAISE EXCEPTION 'publisher registry publication transition is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSE
        RAISE EXCEPTION 'registry publication job role is invalid'
            USING ERRCODE = '42501';
    END IF;

    IF NEW.attempt_count < OLD.attempt_count
        OR NEW.fencing_token < OLD.fencing_token
        OR NEW.attempt_count <> NEW.fencing_token THEN
        RAISE EXCEPTION 'registry publication counters cannot decrease'
            USING ERRCODE = '55000';
    END IF;
    IF (
        NEW.attempt_count IS DISTINCT FROM OLD.attempt_count
        OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
    ) AND NOT (
        (
            (
                OLD.status IN ('queued', 'approved')
                AND NEW.status = 'leased'
            )
            OR (
                OLD.status = 'retry_wait'
                AND (
                    (OLD.cancel_requested_at IS NULL AND NEW.status = 'leased')
                    OR (
                        OLD.cancel_requested_at IS NOT NULL
                        AND NEW.status = 'cancel_requested'
                    )
                )
            )
        )
        AND NEW.attempt_count = OLD.attempt_count + 1
        AND NEW.fencing_token = OLD.fencing_token + 1
    ) THEN
        RAISE EXCEPTION 'registry publication fencing may advance only on claim'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status IN ('leased', 'cancel_requested') AND OLD.status = NEW.status THEN
        IF NOT (
            NEW.attempt_count = OLD.attempt_count
            AND NEW.fencing_token = OLD.fencing_token
            AND NEW.lease_owner_id = OLD.lease_owner_id
            AND NEW.lease_capability_digest = OLD.lease_capability_digest
            AND NEW.lease_acquired_at = OLD.lease_acquired_at
            AND NEW.lease_heartbeat_at >= OLD.lease_heartbeat_at
            AND NEW.lease_expires_at > OLD.lease_expires_at
        ) THEN
            RAISE EXCEPTION 'registry publication heartbeat is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    IF SESSION_USER = 'schemabridge_publisher' AND NOT (
        NEW.authorization_id IS NOT DISTINCT FROM OLD.authorization_id
        OR (
            OLD.status = 'leased'
            AND NEW.status = 'awaiting_approval'
            AND NEW.authorization_id IS NULL
        )
    ) THEN
        RAISE EXCEPTION 'publisher cannot create registry publication authorization'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'retry_wait' AND (
        NEW.failure_code NOT IN ('base_unavailable', 'datahub_unavailable', 'readback_required')
        OR NEW.attempt_count >= NEW.max_attempts
        OR NEW.available_at <> NEW.updated_at
            + LEAST(5 * power(2, NEW.attempt_count - 1), 300) * interval '1 second'
    ) THEN
        RAISE EXCEPTION 'registry publication retry state is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'dead_lettered' AND NOT (
        NEW.failure_code IN ('target_conflict', 'unexpected_worker_failure')
        OR (
            NEW.failure_code IN ('base_unavailable', 'datahub_unavailable', 'readback_required')
            AND NEW.attempt_count >= NEW.max_attempts
        )
    ) THEN
        RAISE EXCEPTION 'registry publication dead letter is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'failed' AND NEW.failure_code NOT IN (
        'proposal_unavailable',
        'proposal_stale',
        'catalog_stale',
        'base_stale',
        'candidate_invalid',
        'authorization_mismatch',
        'datahub_permission_denied'
    ) THEN
        RAISE EXCEPTION 'registry publication terminal failure is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'activation_ready' AND (
        NEW.payload#>>'{receipt,candidate_id}'
            IS DISTINCT FROM NEW.payload#>>'{candidate,id}'
        OR NEW.payload#>>'{receipt,candidate_fingerprint}'
            IS DISTINCT FROM NEW.payload#>>'{candidate,fingerprint}'
        OR NEW.payload#>'{receipt,scope}'
            IS DISTINCT FROM NEW.payload#>'{candidate,scope}'
        OR NEW.payload#>>'{receipt,registry_version}'
            IS DISTINCT FROM NEW.payload#>>'{candidate,registry,version}'
        OR NEW.payload#>>'{receipt,registry_fingerprint}'
            IS DISTINCT FROM NEW.payload#>>'{authorization,registry_fingerprint}'
        OR NEW.payload#>>'{receipt,target}'
            IS DISTINCT FROM NEW.payload#>>'{candidate,target}'
        OR NEW.payload#>'{receipt,related_asset_urns}' IS DISTINCT FROM (
            SELECT jsonb_agg(to_jsonb(observed_urn) ORDER BY observed_urn)
            FROM (
                SELECT DISTINCT binding->>'observed_datahub_asset_urn' AS observed_urn
                FROM jsonb_array_elements(
                    NEW.payload#>'{candidate,registry,physical_bindings}'
                ) AS binding
            ) AS observed
        )
    ) THEN
        RAISE EXCEPTION 'registry publication receipt is not exact'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.enforce_registry_publication_event_append()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    current_job record;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'registry publication events are append-only'
            USING ERRCODE = '55000';
    END IF;
    IF SESSION_USER = 'schemabridge_api' THEN
        IF NEW.actor_id IS NULL OR NEW.worker_id IS NOT NULL THEN
            RAISE EXCEPTION 'API registry publication event actor is invalid'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.event_kind NOT IN ('submitted', 'authorized', 'cancel_requested', 'cancelled') THEN
            RAISE EXCEPTION 'API registry publication event kind is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSIF SESSION_USER = 'schemabridge_publisher' THEN
        IF NEW.actor_id IS NOT NULL OR NEW.worker_id IS NULL THEN
            RAISE EXCEPTION 'publisher registry publication event worker is invalid'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.event_kind NOT IN (
            'leased',
            'recovery_leased',
            'candidate_ready',
            'authorization_expired',
            'cancelled',
            'retry_scheduled',
            'activation_ready',
            'failed',
            'dead_lettered'
        ) THEN
            RAISE EXCEPTION 'publisher registry publication event kind is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSIF SESSION_USER <> 'schemabridge_migrator' THEN
        RAISE EXCEPTION 'registry publication event role is invalid'
            USING ERRCODE = '42501';
    END IF;

    SELECT status, revision, updated_at, failure_code
    INTO current_job
    FROM schemabridge_control.registry_publication_jobs
    WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id;
    IF NOT FOUND OR (
        NEW.status <> current_job.status
        OR NEW.revision <> current_job.revision
        OR NEW.occurred_at <> current_job.updated_at
        OR NEW.failure_code IS DISTINCT FROM current_job.failure_code
    ) THEN
        RAISE EXCEPTION 'registry publication event does not match current state'
            USING ERRCODE = '55000';
    END IF;
    IF NOT (
        (NEW.event_kind = 'submitted' AND NEW.status = 'queued')
        OR (NEW.event_kind = 'leased' AND NEW.status = 'leased')
        OR (NEW.event_kind = 'recovery_leased' AND NEW.status = 'cancel_requested')
        OR (NEW.event_kind = 'candidate_ready' AND NEW.status = 'awaiting_approval')
        OR (NEW.event_kind = 'authorized' AND NEW.status = 'approved')
        OR (NEW.event_kind = 'authorization_expired' AND NEW.status = 'awaiting_approval')
        OR (NEW.event_kind = 'cancel_requested' AND NEW.status = 'cancel_requested')
        OR (NEW.event_kind = 'cancelled' AND NEW.status = 'cancelled')
        OR (NEW.event_kind = 'retry_scheduled' AND NEW.status = 'retry_wait')
        OR (NEW.event_kind = 'activation_ready' AND NEW.status = 'activation_ready')
        OR (NEW.event_kind = 'failed' AND NEW.status = 'failed')
        OR (NEW.event_kind = 'dead_lettered' AND NEW.status = 'dead_lettered')
    ) THEN
        RAISE EXCEPTION 'registry publication event kind does not match state'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.load_registry_activation_ready_handoff(
    p_workspace_id varchar,
    p_catalog_scope varchar,
    p_registry_id varchar,
    p_target_version bigint,
    p_catalog_stale_after_seconds integer
)
RETURNS TABLE (
    job_id varchar(200),
    source_proposal_id varchar(200),
    source_proposal_fingerprint char(64),
    candidate_id varchar(200),
    candidate_fingerprint char(64),
    registry_fingerprint char(64),
    registry_target varchar(500),
    attempt_authorization_id varchar(200),
    observed_authorization_id varchar(200),
    catalog_authority_fingerprint char(64),
    observed_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    publication record;
    connection_binding record;
    physical_binding record;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry activation-ready handoff role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 3 AND 200
        OR p_catalog_scope !~ '^[a-z][a-z0-9_.:-]{2,119}$'
        OR p_registry_id !~ '^[a-z][a-z0-9_]{2,79}$'
        OR p_target_version IS NULL
        OR p_target_version < 1
        OR p_catalog_stale_after_seconds NOT BETWEEN 60 AND 2592000
    ) THEN
        RAISE EXCEPTION 'registry activation-ready handoff input is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT
        jobs.job_id,
        jobs.proposal_id,
        jobs.proposal_fingerprint,
        jobs.candidate_fingerprint,
        jobs.authorization_id,
        jobs.observed_authorization_id,
        jobs.payload,
        jobs.payload#>'{candidate,registry,physical_bindings}' AS bindings
    INTO publication
    FROM schemabridge_control.registry_publication_jobs AS jobs
    WHERE jobs.workspace_id = p_workspace_id
      AND jobs.catalog_scope = p_catalog_scope
      AND jobs.registry_id = p_registry_id
      AND jobs.target_version = p_target_version
      AND jobs.status = 'activation_ready'
    FOR SHARE OF jobs;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF (
        publication.payload->>'status' IS DISTINCT FROM 'activation_ready'
        OR publication.payload#>>'{scope,workspace_id}' IS DISTINCT FROM p_workspace_id
        OR publication.payload#>>'{scope,catalog_scope}' IS DISTINCT FROM p_catalog_scope
        OR publication.payload#>>'{scope,registry_id}' IS DISTINCT FROM p_registry_id
        OR publication.payload#>>'{proposal,id}'
            IS DISTINCT FROM publication.proposal_id
        OR publication.payload#>>'{proposal,fingerprint}'
            IS DISTINCT FROM publication.proposal_fingerprint::text
        OR publication.payload#>>'{candidate,source_proposal_id}'
            IS DISTINCT FROM publication.proposal_id
        OR publication.payload#>>'{candidate,source_proposal_fingerprint}'
            IS DISTINCT FROM publication.proposal_fingerprint::text
        OR publication.payload#>>'{candidate,scope,workspace_id}'
            IS DISTINCT FROM p_workspace_id
        OR publication.payload#>>'{candidate,scope,catalog_scope}'
            IS DISTINCT FROM p_catalog_scope
        OR publication.payload#>>'{candidate,scope,registry_id}'
            IS DISTINCT FROM p_registry_id
        OR publication.payload#>>'{candidate,registry,format_version}'
            IS DISTINCT FROM '2'
        OR publication.payload#>>'{candidate,registry,catalog_scope}'
            IS DISTINCT FROM p_catalog_scope
        OR publication.payload#>>'{candidate,registry,registry_id}'
            IS DISTINCT FROM p_registry_id
        OR publication.payload#>>'{candidate,registry,version}'
            IS DISTINCT FROM p_target_version::text
        OR publication.payload#>>'{candidate,fingerprint}'
            IS DISTINCT FROM publication.candidate_fingerprint::text
        OR publication.payload#>>'{authorization,id}'
            IS DISTINCT FROM publication.authorization_id
        OR publication.payload#>>'{authorization,candidate_id}'
            IS DISTINCT FROM publication.payload#>>'{candidate,id}'
        OR publication.payload#>>'{authorization,candidate_fingerprint}'
            IS DISTINCT FROM publication.candidate_fingerprint::text
        OR publication.payload#>>'{authorization,source_proposal_id}'
            IS DISTINCT FROM publication.proposal_id
        OR publication.payload#>>'{authorization,source_proposal_fingerprint}'
            IS DISTINCT FROM publication.proposal_fingerprint::text
        OR publication.payload#>>'{authorization,scope,workspace_id}'
            IS DISTINCT FROM p_workspace_id
        OR publication.payload#>>'{authorization,scope,catalog_scope}'
            IS DISTINCT FROM p_catalog_scope
        OR publication.payload#>>'{authorization,scope,registry_id}'
            IS DISTINCT FROM p_registry_id
        OR publication.payload#>>'{receipt,candidate_id}'
            IS DISTINCT FROM publication.payload#>>'{candidate,id}'
        OR publication.payload#>>'{receipt,candidate_fingerprint}'
            IS DISTINCT FROM publication.candidate_fingerprint::text
        OR publication.payload#>>'{receipt,scope,workspace_id}'
            IS DISTINCT FROM p_workspace_id
        OR publication.payload#>>'{receipt,scope,catalog_scope}'
            IS DISTINCT FROM p_catalog_scope
        OR publication.payload#>>'{receipt,scope,registry_id}'
            IS DISTINCT FROM p_registry_id
        OR publication.payload#>>'{receipt,registry_version}'
            IS DISTINCT FROM p_target_version::text
        OR publication.payload#>>'{receipt,registry_fingerprint}'
            IS DISTINCT FROM publication.payload#>>'{authorization,registry_fingerprint}'
        OR publication.payload#>>'{receipt,target}'
            IS DISTINCT FROM publication.payload#>>'{candidate,target}'
        OR publication.payload#>>'{receipt,observed_authorization_id}'
            IS DISTINCT FROM publication.observed_authorization_id
        OR publication.payload#>'{receipt,related_asset_urns}' IS DISTINCT FROM (
            SELECT jsonb_agg(to_jsonb(observed_urn) ORDER BY observed_urn)
            FROM (
                SELECT DISTINCT binding->>'observed_datahub_asset_urn' AS observed_urn
                FROM jsonb_array_elements(publication.bindings) AS binding
            ) AS observed
        )
        OR jsonb_typeof(publication.bindings) IS DISTINCT FROM 'array'
        OR jsonb_array_length(publication.bindings) NOT BETWEEN 1 AND 2000
        OR NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(publication.bindings) AS binding
            WHERE binding->>'source_proposal_id' = publication.proposal_id
              AND binding->>'source_proposal_fingerprint'
                    = publication.proposal_fingerprint::text
        )
    ) THEN
        RETURN;
    END IF;

    FOR connection_binding IN
        SELECT DISTINCT binding->>'connection_id' AS connection_id
        FROM jsonb_array_elements(publication.bindings) AS binding
    LOOP
        PERFORM 1
        FROM schemabridge_control.catalog_connections AS catalog_connection
        WHERE catalog_connection.workspace_id = p_workspace_id
          AND catalog_connection.connection_id = connection_binding.connection_id
          AND catalog_connection.catalog_scope = p_catalog_scope
          AND catalog_connection.status = 'enabled'
        FOR SHARE OF catalog_connection;
        IF NOT FOUND THEN
            RETURN;
        END IF;
    END LOOP;

    FOR physical_binding IN
        SELECT binding, ordinality
        FROM jsonb_array_elements(publication.bindings)
            WITH ORDINALITY AS requested(binding, ordinality)
    LOOP
        IF (
            physical_binding.binding->>'workspace_id' IS DISTINCT FROM p_workspace_id
            OR physical_binding.binding->>'catalog_scope'
                IS DISTINCT FROM p_catalog_scope
            OR physical_binding.binding#>>'{locator,asset,workspace_id}'
                IS DISTINCT FROM p_workspace_id
            OR physical_binding.binding#>>'{locator,asset,connection_id}'
                IS DISTINCT FROM physical_binding.binding->>'connection_id'
            OR jsonb_typeof(physical_binding.binding#>'{locator,field_path}')
                IS DISTINCT FROM 'array'
            OR jsonb_array_length(physical_binding.binding#>'{locator,field_path}') <> 1
        ) THEN
            RETURN;
        END IF;

        PERFORM 1
        FROM schemabridge_control.catalog_connections AS catalog_connection
        JOIN schemabridge_control.catalog_assets AS asset
          ON asset.workspace_id = catalog_connection.workspace_id
         AND asset.connection_id = catalog_connection.connection_id
         AND asset.generation = catalog_connection.active_generation
        JOIN schemabridge_control.catalog_fields AS field
          ON field.workspace_id = asset.workspace_id
         AND field.connection_id = asset.connection_id
         AND field.generation = asset.generation
         AND field.asset_key = asset.asset_key
        WHERE catalog_connection.workspace_id = p_workspace_id
          AND catalog_connection.connection_id
                = physical_binding.binding->>'connection_id'
          AND catalog_connection.catalog_scope = p_catalog_scope
          AND catalog_connection.status = 'enabled'
          AND catalog_connection.active_generation
                = (physical_binding.binding->>'catalog_generation')::bigint
          AND catalog_connection.active_generation_fingerprint
                = physical_binding.binding->>'catalog_generation_fingerprint'
          AND catalog_connection.active_generation_completed_at
                >= clock_timestamp()
                    - make_interval(secs => p_catalog_stale_after_seconds)
          AND asset.asset_id
                = physical_binding.binding#>>'{locator,asset,asset_id}'
          AND asset.asset_id
                = physical_binding.binding->>'observed_datahub_asset_urn'
          AND asset.metadata_fingerprint
                = physical_binding.binding->>'asset_metadata_fingerprint'
          AND asset.schema_name IS NOT NULL
          AND asset.table_name IS NOT NULL
          AND field.field_path = ARRAY(
                SELECT jsonb_array_elements_text(
                    physical_binding.binding#>'{locator,field_path}'
                )
          )::varchar(200)[]
          AND field.metadata_fingerprint
                = physical_binding.binding->>'field_metadata_fingerprint'
          AND field.normalized_type
                = physical_binding.binding->>'physical_type'
          AND physical_binding.binding->>'physical_field' = concat_ws(
                '.',
                asset.schema_name,
                asset.table_name,
                array_to_string(field.field_path, '.')
          );
        IF NOT FOUND THEN
            RETURN;
        END IF;
    END LOOP;

    RETURN QUERY
    SELECT
        publication.job_id::varchar(200),
        publication.proposal_id::varchar(200),
        publication.proposal_fingerprint::char(64),
        (publication.payload#>>'{candidate,id}')::varchar(200),
        publication.candidate_fingerprint::char(64),
        (publication.payload#>>'{receipt,registry_fingerprint}')::char(64),
        (publication.payload#>>'{receipt,target}')::varchar(500),
        publication.authorization_id::varchar(200),
        publication.observed_authorization_id::varchar(200),
        encode(sha256(convert_to(publication.bindings::text, 'UTF8')), 'hex')::char(64),
        (publication.payload#>>'{receipt,observed_at}')::timestamptz;
END;
$$;

CREATE TRIGGER registry_publication_jobs_lifecycle
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.registry_publication_jobs
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.enforce_registry_publication_job_lifecycle();

CREATE TRIGGER registry_publication_events_append_only
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.registry_publication_events
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.enforce_registry_publication_event_append();

REVOKE ALL ON
    schemabridge_control.registry_publication_jobs,
    schemabridge_control.registry_publication_events
    FROM PUBLIC;

REVOKE ALL ON SEQUENCE
    schemabridge_control.registry_publication_events_sequence_seq
    FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION
    schemabridge_control.enforce_registry_publication_job_lifecycle(),
    schemabridge_control.enforce_registry_publication_event_append(),
    schemabridge_control.load_registry_activation_ready_handoff(
        varchar,
        varchar,
        varchar,
        bigint,
        integer
    )
    FROM PUBLIC;

GRANT SELECT ON schemabridge_control.schema_migrations
    TO schemabridge_publisher;

GRANT SELECT ON
    schemabridge_control.registry_publication_jobs,
    schemabridge_control.registry_publication_events
    TO schemabridge_api, schemabridge_publisher;

GRANT INSERT (
    job_id,
    workspace_id,
    catalog_scope,
    registry_id,
    target_version,
    proposal_id,
    proposal_fingerprint,
    submitted_by,
    idempotency_digest,
    request_fingerprint,
    status,
    revision,
    attempt_count,
    max_attempts,
    fencing_token,
    available_at,
    payload,
    submitted_at,
    updated_at
) ON schemabridge_control.registry_publication_jobs
    TO schemabridge_api;

GRANT UPDATE (
    status,
    revision,
    available_at,
    authorization_id,
    failure_code,
    cancel_requested_at,
    payload,
    updated_at,
    completed_at
) ON schemabridge_control.registry_publication_jobs
    TO schemabridge_api;

GRANT UPDATE (
    status,
    revision,
    attempt_count,
    fencing_token,
    available_at,
    lease_owner_id,
    lease_capability_digest,
    lease_acquired_at,
    lease_heartbeat_at,
    lease_expires_at,
    candidate_fingerprint,
    authorization_id,
    observed_authorization_id,
    failure_code,
    cancel_requested_at,
    payload,
    updated_at,
    completed_at
) ON schemabridge_control.registry_publication_jobs
    TO schemabridge_publisher;

GRANT INSERT ON schemabridge_control.registry_publication_events
    TO schemabridge_api, schemabridge_publisher;

GRANT USAGE ON SEQUENCE
    schemabridge_control.registry_publication_events_sequence_seq
    TO schemabridge_api, schemabridge_publisher;

GRANT SELECT ON
    schemabridge_control.semantic_onboarding_drafts,
    schemabridge_control.semantic_onboarding_proposals,
    schemabridge_control.catalog_connections,
    schemabridge_control.catalog_assets,
    schemabridge_control.catalog_fields,
    schemabridge_control.registry_active_pointers
    TO schemabridge_publisher;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.enforce_registry_publication_job_lifecycle(),
    schemabridge_control.enforce_registry_publication_event_append()
    TO schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_registry_activation_ready_handoff(
        varchar,
        varchar,
        varchar,
        bigint,
        integer
    )
    TO schemabridge_runtime, schemabridge_migrator;
