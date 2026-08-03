-- M35 is additive. Historical M26, M33, and M34 payload bytes remain untouched.

CREATE TABLE schemabridge_control.semantic_profile_sources (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 3 AND 200),
    scan_id varchar(80) NOT NULL
        CHECK (scan_id ~ '^scan_[0-9a-f]{64}$'),
    source_kind varchar(40) NOT NULL
        CHECK (
            source_kind IN (
                'semantic_change_scan_v1',
                'registry_join_profile_v1',
                'registry_model_join_profile_v1'
            )
        ),
    source_fingerprint char(64) NOT NULL
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, scan_id),
    UNIQUE (workspace_id, scan_id, source_kind),
    UNIQUE (workspace_id, scan_id, source_fingerprint)
);

INSERT INTO schemabridge_control.semantic_profile_sources (
    workspace_id,
    scan_id,
    source_kind,
    source_fingerprint,
    created_at
)
SELECT
    workspace_id,
    scan_id,
    'semantic_change_scan_v1',
    source_fingerprint,
    requested_at
FROM schemabridge_control.semantic_change_scan_requests;

CREATE FUNCTION schemabridge_control.register_semantic_change_profile_source()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'semantic change profile source operation is invalid'
            USING ERRCODE = '55000';
    END IF;
    INSERT INTO schemabridge_control.semantic_profile_sources (
        workspace_id,
        scan_id,
        source_kind,
        source_fingerprint,
        created_at
    ) VALUES (
        NEW.workspace_id,
        NEW.scan_id,
        'semantic_change_scan_v1',
        NEW.source_fingerprint,
        NEW.requested_at
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER semantic_change_scan_profile_source
AFTER INSERT
ON schemabridge_control.semantic_change_scan_requests
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.register_semantic_change_profile_source();

ALTER TABLE schemabridge_control.semantic_join_profile_jobs
    DROP CONSTRAINT semantic_join_profile_job_scan_fk;

ALTER TABLE schemabridge_control.semantic_join_profile_jobs
    ADD CONSTRAINT semantic_join_profile_job_source_fk
    FOREIGN KEY (workspace_id, scan_id)
    REFERENCES schemabridge_control.semantic_profile_sources (
        workspace_id,
        scan_id
    );

CREATE TABLE schemabridge_control.registry_join_profile_requests (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 3 AND 200),
    change_id varchar(80) NOT NULL
        CHECK (change_id ~ '^[a-z][a-z0-9_-]{2,79}$'),
    scan_id varchar(80) NOT NULL
        CHECK (scan_id ~ '^scan_[0-9a-f]{64}$'),
    catalog_scope varchar(120) NOT NULL
        CHECK (catalog_scope ~ '^[a-z][a-z0-9_.:-]{2,119}$'),
    registry_id varchar(80) NOT NULL
        CHECK (registry_id ~ '^[a-z][a-z0-9_]{2,79}$'),
    base_registry_version bigint NOT NULL CHECK (base_registry_version >= 1),
    base_registry_fingerprint char(64) NOT NULL
        CHECK (base_registry_fingerprint ~ '^[0-9a-f]{64}$'),
    connection_id varchar(200) NOT NULL
        CHECK (connection_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    proposal_fingerprint char(64) NOT NULL
        CHECK (proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    execution_target_fingerprint char(64) NOT NULL
        CHECK (execution_target_fingerprint ~ '^[0-9a-f]{64}$'),
    profile_request_fingerprint char(64) NOT NULL
        CHECK (profile_request_fingerprint ~ '^[0-9a-f]{64}$'),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    requested_by varchar(200) NOT NULL
        CHECK (length(trim(requested_by)) BETWEEN 1 AND 200),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 262144
            AND payload->>'id' = change_id
            AND payload->>'workspace_id' = workspace_id
            AND payload#>>'{request,scan_id}' = scan_id
            AND payload#>>'{request,scope,workspace_id}' = workspace_id
            AND payload#>>'{request,scope,catalog_scope}' = catalog_scope
            AND payload#>>'{request,scope,registry_id}' = registry_id
            AND (payload#>>'{request,base_evidence,base_registry,registry_version}')::bigint
                = base_registry_version
            AND payload#>>'{request,base_evidence,base_registry,registry_fingerprint}'
                = base_registry_fingerprint
            AND payload#>>'{request,proposal,connection_id}' = connection_id
            AND payload#>>'{request,execution_target,target_fingerprint}'
                = execution_target_fingerprint
            AND payload#>>'{request,fingerprint}' = profile_request_fingerprint
            AND payload->>'fingerprint' = request_fingerprint
            AND payload->>'owner_actor_id' = requested_by
            AND payload#>>'{request,external_writes_performed}' = 'false'
            AND payload->>'external_writes_performed' = 'false'
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    requested_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, scan_id),
    UNIQUE (workspace_id, change_id),
    UNIQUE (workspace_id, requested_by, request_fingerprint),
    CONSTRAINT registry_join_profile_request_fk
        FOREIGN KEY (workspace_id, scan_id)
        REFERENCES schemabridge_control.semantic_profile_sources (
            workspace_id,
            scan_id
        ) DEFERRABLE INITIALLY DEFERRED
);

CREATE FUNCTION schemabridge_control.guard_registry_join_profile_request()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'registry join profile request is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry join profile request role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF NEW.requested_at > clock_timestamp() + interval '1 minute' THEN
        RAISE EXCEPTION 'registry join profile request time is invalid'
            USING ERRCODE = '55000';
    END IF;
    INSERT INTO schemabridge_control.semantic_profile_sources (
        workspace_id,
        scan_id,
        source_kind,
        source_fingerprint,
        created_at
    ) VALUES (
        NEW.workspace_id,
        NEW.scan_id,
        'registry_join_profile_v1',
        NEW.profile_request_fingerprint,
        NEW.requested_at
    ) ON CONFLICT DO NOTHING;
    PERFORM 1
    FROM schemabridge_control.semantic_profile_sources AS source
    WHERE source.workspace_id = NEW.workspace_id
      AND source.scan_id = NEW.scan_id
      AND source.source_kind = 'registry_join_profile_v1'
      AND source.source_fingerprint = NEW.profile_request_fingerprint
      AND source.created_at = NEW.requested_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'registry join profile source identity conflicts'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER registry_join_profile_requests_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.registry_join_profile_requests
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_registry_join_profile_request();

CREATE OR REPLACE FUNCTION schemabridge_control.guard_profile_job_connector_target()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    resolved_contract_version bigint;
BEGIN
    IF SESSION_USER NOT IN (
        'schemabridge_api',
        'schemabridge_reconciler',
        'schemabridge_worker',
        'schemabridge_migrator'
    ) THEN
        RAISE EXCEPTION 'profile job connector target role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF (
            NEW.connector_route_revision IS NULL
            OR NEW.connector_route_fingerprint IS NULL
            OR NEW.connector_target_fingerprint IS NULL
        ) THEN
            RAISE EXCEPTION 'new profile job connector target is required'
                USING ERRCODE = '55000';
        END IF;
        SELECT head.contract_version
        INTO resolved_contract_version
        FROM schemabridge_control.connector_route_heads AS head
        JOIN schemabridge_control.catalog_connections AS connection
          ON connection.workspace_id = head.workspace_id
         AND connection.connection_id = head.connection_id
        JOIN schemabridge_control.connector_contract_revisions AS contract
          ON contract.workspace_id = head.workspace_id
         AND contract.connection_id = head.connection_id
         AND contract.contract_version = head.contract_version
        JOIN schemabridge_control.catalog_generations AS generation
          ON generation.workspace_id = connection.workspace_id
         AND generation.connection_id = connection.connection_id
         AND generation.generation = connection.active_generation
         AND generation.status = 'completed'
         AND generation.source_identity_fingerprint
                = contract.source_identity_fingerprint
         AND generation.catalog_identity_fingerprint
                = contract.catalog_identity_fingerprint
         AND generation.type_contract_fingerprint
                = contract.type_contract_fingerprint
        WHERE head.workspace_id = NEW.workspace_id
          AND head.connection_id = NEW.connection_id
          AND head.route_revision = NEW.connector_route_revision
          AND head.route_fingerprint = NEW.connector_route_fingerprint
          AND head.target_fingerprint = NEW.connector_target_fingerprint
          AND head.status = 'enabled'
          AND connection.status = 'enabled'
          AND contract.type_contract_version = 1
          AND contract.type_contract_fingerprint
                = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589';
        IF (
            resolved_contract_version IS NULL
            OR (
                NEW.connector_contract_version IS NOT NULL
                AND NEW.connector_contract_version <> resolved_contract_version
            )
        ) THEN
            RAISE EXCEPTION 'profile job connector target is not current'
                USING ERRCODE = '55000';
        END IF;
        NEW.connector_contract_version := resolved_contract_version;
    ELSIF TG_OP = 'UPDATE' THEN
        IF (
            OLD.status = 'requested'
            AND NEW.status = 'leased'
            AND EXISTS (
                SELECT 1
                FROM schemabridge_control.semantic_profile_sources AS source
                WHERE source.workspace_id = NEW.workspace_id
                  AND source.scan_id = NEW.scan_id
                  AND source.source_kind = 'registry_join_profile_v1'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM schemabridge_control.registry_join_profile_requests AS request
                JOIN schemabridge_control.semantic_registry_change_audit AS audit
                  ON audit.workspace_id = request.workspace_id
                 AND audit.draft_id = request.change_id
                 AND audit.event = 'profile_job_bound'
                 AND audit.payload->>'profile_job_id' = NEW.job_id
                 AND audit.payload->>'profile_request_fingerprint'
                        = request.profile_request_fingerprint
                JOIN schemabridge_control.semantic_registry_change_operations AS operation
                 ON operation.workspace_id = request.workspace_id
                 AND operation.draft_id = request.change_id
                 AND operation.operation = 'request_profile'
                 AND operation.response_authoring->>'fingerprint'
                        = request.request_fingerprint
                 AND operation.response_job_id = NEW.job_id
                 AND operation.response_job->>'job_id' = NEW.job_id
                WHERE request.workspace_id = NEW.workspace_id
                  AND request.scan_id = NEW.scan_id
                  AND request.proposal_fingerprint = NEW.proposal_fingerprint
                  AND request.requested_by = operation.actor_id
                  AND audit.actor_id = operation.actor_id
            )
        ) THEN
            RAISE EXCEPTION 'registry join profile job is not durably bound'
                USING ERRCODE = '55000';
        END IF;
        IF (
            (
                (OLD.status IN ('requested', 'retry_wait') AND NEW.status = 'leased')
                OR (OLD.status = 'leased' AND NEW.status = 'leased')
            )
            AND NOT EXISTS (
                SELECT 1
                FROM schemabridge_control.registry_model_authority_reaper_sessions AS session
                WHERE session.backend_pid = pg_backend_pid()
                  AND session.transaction_id = txid_current()
            )
            AND EXISTS (
                SELECT 1
                FROM schemabridge_control.semantic_profile_sources AS source
                WHERE source.workspace_id = NEW.workspace_id
                  AND source.scan_id = NEW.scan_id
                  AND source.source_kind = 'registry_model_join_profile_v1'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM schemabridge_control.registry_model_join_profile_requests AS request
                JOIN schemabridge_control.registry_model_join_profile_audit AS audit
                  ON audit.workspace_id = request.workspace_id
                 AND audit.request_id = request.request_id
                 AND audit.event = 'job_bound'
                 AND audit.job_id = NEW.job_id
                JOIN schemabridge_control.registry_model_join_profile_operations AS operation
                  ON operation.workspace_id = request.workspace_id
                 AND operation.request_id = request.request_id
                 AND operation.operation = 'request_model_join_profile'
                 AND operation.response_authoring = request.payload
                 AND operation.response_job_id = NEW.job_id
                 AND operation.response_job->>'job_id' = NEW.job_id
                 AND operation.actor_id = audit.actor_id
                JOIN schemabridge_control.semantic_onboarding_proposals AS source_proposal
                  ON source_proposal.workspace_id = request.workspace_id
                 AND source_proposal.proposal_id = request.source_proposal_id
                 AND source_proposal.fingerprint = request.source_proposal_fingerprint
                JOIN schemabridge_control.semantic_onboarding_drafts AS source_draft
                  ON source_draft.workspace_id = source_proposal.workspace_id
                 AND source_draft.draft_id = source_proposal.draft_id
                 AND source_draft.prepared_proposal_id = source_proposal.proposal_id
                 AND source_draft.prepared_proposal_fingerprint = source_proposal.fingerprint
                 AND source_draft.status = 'ready_for_publication'
                JOIN schemabridge_control.registry_active_pointers AS pointer
                  ON pointer.workspace_id = request.workspace_id
                 AND pointer.catalog_scope = request.catalog_scope
                 AND pointer.registry_id = request.registry_id
                 AND pointer.generation
                        = (request.payload#>>'{request,base,base_registry,activation_generation}')::bigint
                 AND pointer.registry_version = request.base_registry_version
                 AND pointer.registry_fingerprint = request.base_registry_fingerprint
                 AND pointer.transition_id
                        = request.payload#>>'{request,base,dependency_context,pointer_transition_id}'
                JOIN schemabridge_control.semantic_dependency_index_states AS dependency
                  ON dependency.workspace_id = pointer.workspace_id
                 AND dependency.catalog_scope = pointer.catalog_scope
                 AND dependency.registry_id = pointer.registry_id
                 AND dependency.registry_generation = pointer.generation
                 AND dependency.registry_version = pointer.registry_version
                 AND dependency.registry_fingerprint = pointer.registry_fingerprint
                 AND dependency.pointer_transition_id = pointer.transition_id
                 AND dependency.watermark
                        = (request.payload#>>'{request,base,dependency_context,dependency_index,watermark}')::bigint
                 AND dependency.index_fingerprint
                        = request.payload#>>'{request,base,dependency_context,dependency_index,fingerprint}'
                 AND dependency.complete
                WHERE request.workspace_id = NEW.workspace_id
                  AND request.scan_id = NEW.scan_id
                  AND request.proposal_fingerprint = NEW.proposal_fingerprint
                  AND source_proposal.payload = request.payload#>'{request,replacement_source,proposal}'
                  AND source_proposal.payload#>'{base_registry}'
                        = request.payload#>'{request,base,base_registry}'
                  AND schemabridge_control.registry_model_authority_current(
                      request.workspace_id,
                      request.payload#>'{request,base}',
                      request.payload#>'{request,replacement_source,proposal}'
                  )
                  AND schemabridge_control.registry_model_profile_proposal_authorized(
                      request.payload
                  )
            )
        ) THEN
            RAISE EXCEPTION 'registry model join profile authority is stale'
                USING ERRCODE = '55000';
        END IF;
        IF (
            NEW.connector_contract_version IS DISTINCT FROM OLD.connector_contract_version
            OR NEW.connector_route_revision IS DISTINCT FROM OLD.connector_route_revision
            OR NEW.connector_route_fingerprint IS DISTINCT FROM OLD.connector_route_fingerprint
            OR NEW.connector_target_fingerprint IS DISTINCT FROM OLD.connector_target_fingerprint
        ) THEN
            RAISE EXCEPTION 'profile job connector target is immutable'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE FUNCTION schemabridge_control.enqueue_registry_join_profile(
    p_workspace_id varchar,
    p_scan_id varchar,
    p_request_fingerprint char(64),
    p_max_attempts integer DEFAULT 5
)
RETURNS varchar(80)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    request_record record;
    computed_job_id varchar(80);
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry join profile enqueue role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 3 AND 200
        OR p_scan_id !~ '^scan_[0-9a-f]{64}$'
        OR p_request_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_max_attempts NOT BETWEEN 1 AND 100
    ) THEN
        RAISE EXCEPTION 'registry join profile enqueue input is invalid'
            USING ERRCODE = '22023';
    END IF;
    SELECT request.*
    INTO request_record
    FROM schemabridge_control.registry_join_profile_requests AS request
    WHERE request.workspace_id = p_workspace_id
      AND request.scan_id = p_scan_id
      AND request.request_fingerprint = p_request_fingerprint
      AND request.requested_at >= clock_timestamp() - interval '15 minutes'
    FOR SHARE OF request;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'registry join profile request is unavailable'
            USING ERRCODE = '55000';
    END IF;
    computed_job_id := 'profile_job_' || encode(
        sha256(
            convert_to(
                concat_ws(
                    '|',
                    'semantic_join_profile_job_v1',
                    p_workspace_id,
                    p_scan_id,
                    request_record.proposal_fingerprint
                ),
                'UTF8'
            )
        ),
        'hex'
    );
    INSERT INTO schemabridge_control.semantic_join_profile_jobs (
        job_id,
        workspace_id,
        scan_id,
        connection_id,
        connector_route_revision,
        connector_route_fingerprint,
        connector_target_fingerprint,
        proposal_fingerprint,
        proposal_json,
        status,
        attempt_count,
        max_attempts,
        available_at,
        fencing_token,
        requested_at,
        updated_at
    ) VALUES (
        computed_job_id,
        p_workspace_id,
        p_scan_id,
        request_record.connection_id,
        (request_record.payload#>>'{request,execution_target,route_revision}')::bigint,
        request_record.payload#>>'{request,execution_target,route_fingerprint}',
        request_record.execution_target_fingerprint,
        request_record.proposal_fingerprint,
        request_record.payload#>'{request,proposal,proposal}',
        'requested',
        0,
        p_max_attempts,
        request_record.requested_at,
        0,
        request_record.requested_at,
        request_record.requested_at
    )
    ON CONFLICT (workspace_id, scan_id, proposal_fingerprint) DO NOTHING;
    RETURN computed_job_id;
END;
$$;

CREATE FUNCTION schemabridge_control.load_registry_join_profile_job(
    p_workspace_id varchar,
    p_scan_id varchar,
    p_request_fingerprint char(64)
)
RETURNS SETOF schemabridge_control.semantic_join_profile_jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry join profile read role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 3 AND 200
        OR p_scan_id !~ '^scan_[0-9a-f]{64}$'
        OR p_request_fingerprint !~ '^[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'registry join profile read input is invalid'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT job.*
    FROM schemabridge_control.registry_join_profile_requests AS request
    JOIN schemabridge_control.semantic_join_profile_jobs AS job
      ON job.workspace_id = request.workspace_id
     AND job.scan_id = request.scan_id
     AND job.proposal_fingerprint = request.proposal_fingerprint
    WHERE request.workspace_id = p_workspace_id
      AND request.scan_id = p_scan_id
      AND request.request_fingerprint = p_request_fingerprint
      AND job.status <> 'leased';
END;
$$;

CREATE TABLE schemabridge_control.registry_publication_sources (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 3 AND 200),
    proposal_kind varchar(40) NOT NULL
        CHECK (
            proposal_kind IN (
                'onboarding_additive_v1',
                'add_join_v1',
                'replace_model_v1'
            )
        ),
    proposal_id varchar(200) NOT NULL
        CHECK (length(trim(proposal_id)) BETWEEN 3 AND 200),
    proposal_fingerprint char(64) NOT NULL
        CHECK (proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    catalog_scope varchar(120) NOT NULL
        CHECK (catalog_scope ~ '^[a-z][a-z0-9_.:-]{2,119}$'),
    registry_id varchar(80) NOT NULL
        CHECK (registry_id ~ '^[a-z][a-z0-9_]{2,79}$'),
    target_version bigint NOT NULL CHECK (target_version >= 1),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, proposal_kind, proposal_id),
    UNIQUE (workspace_id, proposal_id),
    UNIQUE (workspace_id, proposal_kind, proposal_id, proposal_fingerprint)
);

INSERT INTO schemabridge_control.registry_publication_sources (
    workspace_id,
    proposal_kind,
    proposal_id,
    proposal_fingerprint,
    catalog_scope,
    registry_id,
    target_version,
    created_at
)
SELECT
    workspace_id,
    'onboarding_additive_v1',
    proposal_id,
    fingerprint,
    payload#>>'{scope,catalog_scope}',
    payload#>>'{scope,registry_id}',
    target_registry_version,
    prepared_at
FROM schemabridge_control.semantic_onboarding_proposals;

CREATE FUNCTION schemabridge_control.register_onboarding_publication_source()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'onboarding publication source operation is invalid'
            USING ERRCODE = '55000';
    END IF;
    INSERT INTO schemabridge_control.registry_publication_sources (
        workspace_id,
        proposal_kind,
        proposal_id,
        proposal_fingerprint,
        catalog_scope,
        registry_id,
        target_version,
        created_at
    ) VALUES (
        NEW.workspace_id,
        'onboarding_additive_v1',
        NEW.proposal_id,
        NEW.fingerprint,
        NEW.payload#>>'{scope,catalog_scope}',
        NEW.payload#>>'{scope,registry_id}',
        NEW.target_registry_version,
        NEW.prepared_at
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER semantic_onboarding_publication_source
AFTER INSERT
ON schemabridge_control.semantic_onboarding_proposals
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.register_onboarding_publication_source();

ALTER TABLE schemabridge_control.registry_publication_jobs
    ADD COLUMN proposal_kind varchar(40)
    GENERATED ALWAYS AS (
        coalesce(payload#>>'{proposal,proposal_kind}', 'onboarding_additive_v1')
    ) STORED;

ALTER TABLE schemabridge_control.registry_publication_jobs
    ADD CONSTRAINT registry_publication_proposal_kind_check
    CHECK (
        proposal_kind IN (
            'onboarding_additive_v1',
            'add_join_v1',
            'replace_model_v1'
        )
    );

ALTER TABLE schemabridge_control.registry_publication_jobs
    DROP CONSTRAINT registry_publication_proposal_fk;

ALTER TABLE schemabridge_control.registry_publication_jobs
    ADD CONSTRAINT registry_publication_source_fk
    FOREIGN KEY (workspace_id, proposal_kind, proposal_id)
    REFERENCES schemabridge_control.registry_publication_sources (
        workspace_id,
        proposal_kind,
        proposal_id
    );

CREATE TABLE schemabridge_control.semantic_registry_change_drafts (
    workspace_id varchar(200) NOT NULL,
    draft_id varchar(80) NOT NULL
        CHECK (draft_id ~ '^[a-z][a-z0-9_-]{2,79}$'),
    change_kind varchar(40) NOT NULL CHECK (change_kind = 'add_join_v1'),
    catalog_scope varchar(120) NOT NULL,
    registry_id varchar(80) NOT NULL,
    owner_actor_id varchar(200) NOT NULL,
    status varchar(32) NOT NULL
        CHECK (status IN ('needs_review', 'ready_for_publication', 'superseded')),
    revision bigint NOT NULL CHECK (revision >= 1),
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    prepared_proposal_id varchar(200),
    prepared_proposal_fingerprint char(64),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 524288
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = draft_id
            AND payload#>>'{scope,catalog_scope}' = catalog_scope
            AND payload#>>'{scope,registry_id}' = registry_id
            AND payload->>'owner_actor_id' = owner_actor_id
            AND payload->>'status' = status
            AND (payload->>'revision')::bigint = revision
            AND payload->>'fingerprint' = fingerprint
            AND coalesce(payload->>'prepared_proposal_id', '')
                = coalesce(prepared_proposal_id, '')
            AND coalesce(payload->>'prepared_proposal_fingerprint', '')
                = coalesce(prepared_proposal_fingerprint::text, '')
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, draft_id),
    UNIQUE (workspace_id, draft_id, fingerprint),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.registry_join_profile_requests (
            workspace_id,
            change_id
        ),
    CHECK (updated_at >= created_at),
    CHECK (
        (prepared_proposal_id IS NULL) = (prepared_proposal_fingerprint IS NULL)
    )
);

CREATE TABLE schemabridge_control.semantic_registry_change_decisions (
    workspace_id varchar(200) NOT NULL,
    decision_id varchar(200) NOT NULL,
    draft_id varchar(80) NOT NULL,
    resulting_revision bigint NOT NULL CHECK (resulting_revision >= 2),
    actor_id varchar(200) NOT NULL,
    action varchar(16) NOT NULL CHECK (action IN ('approve', 'reject')),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 65536
            AND payload->>'id' = decision_id
            AND payload->>'actor' = actor_id
            AND payload->>'action' = action
            AND (payload->>'resulting_version')::bigint = resulting_revision
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    decided_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, decision_id),
    UNIQUE (workspace_id, draft_id),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.semantic_registry_change_drafts (
            workspace_id,
            draft_id
        )
);

CREATE TABLE schemabridge_control.semantic_registry_change_proposals (
    workspace_id varchar(200) NOT NULL,
    proposal_id varchar(200) NOT NULL,
    proposal_kind varchar(40) NOT NULL CHECK (proposal_kind = 'add_join_v1'),
    draft_id varchar(80) NOT NULL,
    draft_revision bigint NOT NULL CHECK (draft_revision >= 2),
    target_registry_version bigint NOT NULL CHECK (target_registry_version >= 2),
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    prepared_by varchar(200) NOT NULL,
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 524288
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = proposal_id
            AND payload->>'proposal_kind' = proposal_kind
            AND payload->>'draft_id' = draft_id
            AND (payload->>'draft_revision')::bigint = draft_revision
            AND (payload->>'target_registry_version')::bigint
                = target_registry_version
            AND payload->>'fingerprint' = fingerprint
            AND payload->>'prepared_by' = prepared_by
            AND payload->>'external_writes_performed' = 'false'
            AND payload->>'owner_actor_id' <> prepared_by
            AND payload#>>'{decision,actor}' <> prepared_by
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    prepared_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, proposal_id),
    UNIQUE (workspace_id, draft_id, target_registry_version),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.semantic_registry_change_drafts (
            workspace_id,
            draft_id
        )
);

CREATE TABLE schemabridge_control.semantic_registry_change_audit (
    workspace_id varchar(200) NOT NULL,
    audit_id varchar(200) NOT NULL,
    draft_id varchar(80) NOT NULL,
    source_revision bigint NOT NULL CHECK (source_revision >= 0),
    resulting_revision bigint NOT NULL CHECK (resulting_revision >= 0),
    event varchar(40) NOT NULL
        CHECK (
            event IN (
                'profile_requested',
                'profile_job_bound',
                'draft_finalized',
                'decision_recorded',
                'publication_prepared'
            )
        ),
    actor_id varchar(200) NOT NULL,
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 65536
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = audit_id
            AND payload->>'change_id' = draft_id
            AND (payload->>'source_revision')::bigint = source_revision
            AND (payload->>'resulting_revision')::bigint = resulting_revision
            AND payload->>'event' = event
            AND payload->>'actor_id' = actor_id
            AND payload->>'fingerprint' = fingerprint
            AND payload->>'external_writes_performed' = 'false'
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, audit_id),
    UNIQUE (workspace_id, draft_id, event, resulting_revision),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.registry_join_profile_requests (
            workspace_id,
            change_id
        )
);

CREATE TABLE schemabridge_control.semantic_registry_change_operations (
    workspace_id varchar(200) NOT NULL,
    actor_id varchar(200) NOT NULL,
    idempotency_digest char(64) NOT NULL
        CHECK (idempotency_digest ~ '^[0-9a-f]{64}$'),
    operation varchar(64) NOT NULL
        CHECK (
            operation IN (
                'request_profile',
                'finalize_draft',
                'record_decision',
                'prepare_publication'
            )
        ),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    draft_id varchar(80) NOT NULL,
    response_revision bigint NOT NULL CHECK (response_revision >= 0),
    response_fingerprint char(64) NOT NULL
        CHECK (response_fingerprint ~ '^[0-9a-f]{64}$'),
    response_authoring jsonb NOT NULL
        CHECK (
            jsonb_typeof(response_authoring) = 'object'
            AND octet_length(response_authoring::text) <= 524288
            AND response_authoring->>'workspace_id' = workspace_id
            AND response_authoring->>'id' = draft_id
            AND response_authoring->>'external_writes_performed' = 'false'
        ),
    response_job_id varchar(80)
        CHECK (
            response_job_id IS NULL
            OR response_job_id ~ '^profile_job_[0-9a-f]{64}$'
        ),
    response_job jsonb
        CHECK (
            response_job IS NULL
            OR (
                jsonb_typeof(response_job) = 'object'
                AND octet_length(response_job::text) <= 524288
                AND response_job->>'workspace_id' = workspace_id
                AND response_job->>'job_id' = response_job_id
                AND response_job->>'status' = 'requested'
                AND response_job->>'lease' IS NULL
                AND NOT schemabridge_control.semantic_json_has_protected_keys(response_job)
            )
        ),
    response_draft jsonb
        CHECK (
            response_draft IS NULL
            OR (
                jsonb_typeof(response_draft) = 'object'
                AND octet_length(response_draft::text) <= 524288
                AND response_draft->>'workspace_id' = workspace_id
                AND response_draft->>'id' = draft_id
            )
        ),
    response_proposal jsonb
        CHECK (
            response_proposal IS NULL
            OR (
                jsonb_typeof(response_proposal) = 'object'
                AND octet_length(response_proposal::text) <= 524288
                AND response_proposal->>'workspace_id' = workspace_id
                AND response_proposal->>'draft_id' = draft_id
                AND response_proposal->>'external_writes_performed' = 'false'
            )
        ),
    proposal_id varchar(200),
    proposal_fingerprint char(64),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, idempotency_digest),
    UNIQUE (workspace_id, actor_id, operation, request_fingerprint),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.registry_join_profile_requests (
            workspace_id,
            change_id
        ),
    CHECK ((proposal_id IS NULL) = (proposal_fingerprint IS NULL)),
    CHECK ((response_proposal IS NULL) = (proposal_id IS NULL)),
    CHECK ((response_job_id IS NULL) = (response_job IS NULL)),
    CHECK (response_job_id IS NULL OR operation = 'request_profile'),
    CHECK (
        (
            operation = 'request_profile'
            AND actor_id = response_authoring->>'owner_actor_id'
            AND response_revision = 0
            AND response_fingerprint = response_authoring->>'fingerprint'
            AND response_draft IS NULL
            AND response_proposal IS NULL
            AND proposal_id IS NULL
            AND proposal_fingerprint IS NULL
        )
        OR (
            operation IN ('finalize_draft', 'record_decision')
            AND response_job_id IS NULL
            AND response_job IS NULL
            AND response_draft IS NOT NULL
            AND response_fingerprint = response_draft->>'fingerprint'
            AND (response_draft->>'revision')::bigint = response_revision
            AND response_proposal IS NULL
            AND proposal_id IS NULL
            AND proposal_fingerprint IS NULL
        )
        OR (
            operation = 'prepare_publication'
            AND response_job_id IS NULL
            AND response_job IS NULL
            AND response_draft IS NOT NULL
            AND response_fingerprint = response_draft->>'fingerprint'
            AND (response_draft->>'revision')::bigint = response_revision
            AND response_proposal IS NOT NULL
            AND response_proposal->>'id' = proposal_id
            AND response_proposal->>'fingerprint' = proposal_fingerprint
        )
    )
);

CREATE FUNCTION schemabridge_control.guard_semantic_registry_change_draft()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'semantic registry change draft role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'semantic registry change draft cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'needs_review' OR NEW.revision <> 1 THEN
            RAISE EXCEPTION 'new semantic registry change draft is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.draft_id IS DISTINCT FROM OLD.draft_id
        OR NEW.change_kind IS DISTINCT FROM OLD.change_kind
        OR NEW.catalog_scope IS DISTINCT FROM OLD.catalog_scope
        OR NEW.registry_id IS DISTINCT FROM OLD.registry_id
        OR NEW.owner_actor_id IS DISTINCT FROM OLD.owner_actor_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.updated_at <= OLD.updated_at
        OR NEW.fingerprint = OLD.fingerprint
        OR NOT (
            (
                OLD.status = 'needs_review'
                AND NEW.status = 'needs_review'
                AND NEW.revision = OLD.revision + 1
            )
            OR (
                OLD.status = 'needs_review'
                AND NEW.status = 'ready_for_publication'
                AND NEW.revision = OLD.revision
            )
            OR (
                OLD.status = 'ready_for_publication'
                AND NEW.status = 'superseded'
                AND NEW.revision = OLD.revision
            )
        )
    ) THEN
        RAISE EXCEPTION 'semantic registry change draft transition is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_semantic_registry_change_history()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'semantic registry change history is append-only'
            USING ERRCODE = '55000';
    END IF;
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'semantic registry change history role is invalid'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_semantic_registry_change_operation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'semantic registry change operation role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'semantic registry change operation cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.response_job_id IS NOT NULL OR NEW.response_job IS NOT NULL THEN
            RAISE EXCEPTION 'new semantic registry change operation cannot pre-bind a job'
                USING ERRCODE = '55000';
        END IF;
        IF (
            NOT EXISTS (
                SELECT 1
                FROM schemabridge_control.registry_join_profile_requests AS request
                WHERE request.workspace_id = NEW.workspace_id
                  AND request.change_id = NEW.draft_id
                  AND request.payload = NEW.response_authoring
                  AND request.request_fingerprint
                        = NEW.response_authoring->>'fingerprint'
            )
            OR NOT EXISTS (
                SELECT 1
                FROM schemabridge_control.semantic_registry_change_audit AS audit
                WHERE audit.workspace_id = NEW.workspace_id
                  AND audit.draft_id = NEW.draft_id
                  AND audit.actor_id = NEW.actor_id
                  AND audit.event = CASE NEW.operation
                      WHEN 'request_profile' THEN 'profile_requested'
                      WHEN 'finalize_draft' THEN 'draft_finalized'
                      WHEN 'record_decision' THEN 'decision_recorded'
                      WHEN 'prepare_publication' THEN 'publication_prepared'
                  END
                  AND audit.resulting_revision = NEW.response_revision
                  AND audit.payload->>'resulting_fingerprint'
                        = NEW.response_fingerprint
            )
            OR (
                NEW.response_draft IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM schemabridge_control.semantic_registry_change_drafts AS draft
                    WHERE draft.workspace_id = NEW.workspace_id
                      AND draft.draft_id = NEW.draft_id
                      AND draft.payload = NEW.response_draft
                      AND draft.fingerprint = NEW.response_fingerprint
                )
            )
            OR (
                NEW.response_proposal IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM schemabridge_control.semantic_registry_change_proposals AS proposal
                    WHERE proposal.workspace_id = NEW.workspace_id
                      AND proposal.proposal_id = NEW.proposal_id
                      AND proposal.payload = NEW.response_proposal
                      AND proposal.fingerprint = NEW.proposal_fingerprint
                )
            )
        ) THEN
            RAISE EXCEPTION 'semantic registry change operation witness is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF (
        OLD.operation <> 'request_profile'
        OR OLD.response_job_id IS NOT NULL
        OR OLD.response_job IS NOT NULL
        OR NEW.response_job_id IS NULL
        OR NEW.response_job IS NULL
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
        OR NEW.idempotency_digest IS DISTINCT FROM OLD.idempotency_digest
        OR NEW.operation IS DISTINCT FROM OLD.operation
        OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
        OR NEW.draft_id IS DISTINCT FROM OLD.draft_id
        OR NEW.response_revision IS DISTINCT FROM OLD.response_revision
        OR NEW.response_fingerprint IS DISTINCT FROM OLD.response_fingerprint
        OR NEW.response_authoring IS DISTINCT FROM OLD.response_authoring
        OR NEW.response_draft IS DISTINCT FROM OLD.response_draft
        OR NEW.response_proposal IS DISTINCT FROM OLD.response_proposal
        OR NEW.proposal_id IS DISTINCT FROM OLD.proposal_id
        OR NEW.proposal_fingerprint IS DISTINCT FROM OLD.proposal_fingerprint
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NOT EXISTS (
            SELECT 1
            FROM schemabridge_control.registry_join_profile_requests AS request
            JOIN schemabridge_control.semantic_join_profile_jobs AS job
              ON job.workspace_id = request.workspace_id
             AND job.scan_id = request.scan_id
             AND job.proposal_fingerprint = request.proposal_fingerprint
            JOIN schemabridge_control.semantic_registry_change_audit AS audit
              ON audit.workspace_id = request.workspace_id
             AND audit.draft_id = request.change_id
             AND audit.event = 'profile_job_bound'
             AND audit.actor_id = NEW.actor_id
             AND audit.payload->>'profile_job_id' = job.job_id
             AND audit.payload->>'profile_request_fingerprint'
                    = request.profile_request_fingerprint
            WHERE request.workspace_id = NEW.workspace_id
              AND request.change_id = NEW.draft_id
              AND request.requested_by = NEW.actor_id
              AND request.request_fingerprint
                    = NEW.response_authoring->>'fingerprint'
              AND job.job_id = NEW.response_job_id
              AND job.status = 'requested'
              AND NEW.response_job->>'scan_id' = request.scan_id
              AND NEW.response_job->>'proposal_fingerprint'
                    = request.proposal_fingerprint
        )
    ) THEN
        RAISE EXCEPTION 'semantic registry change operation binding is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.register_registry_change_publication_source()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'registry change publication source operation is invalid'
            USING ERRCODE = '55000';
    END IF;
    INSERT INTO schemabridge_control.registry_publication_sources (
        workspace_id,
        proposal_kind,
        proposal_id,
        proposal_fingerprint,
        catalog_scope,
        registry_id,
        target_version,
        created_at
    ) VALUES (
        NEW.workspace_id,
        NEW.proposal_kind,
        NEW.proposal_id,
        NEW.fingerprint,
        NEW.payload#>>'{scope,catalog_scope}',
        NEW.payload#>>'{scope,registry_id}',
        NEW.target_registry_version,
        NEW.prepared_at
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER semantic_registry_change_drafts_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_registry_change_drafts
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_semantic_registry_change_draft();

CREATE TRIGGER semantic_registry_change_decisions_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_registry_change_decisions
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_semantic_registry_change_history();

CREATE TRIGGER semantic_registry_change_proposals_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_registry_change_proposals
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_semantic_registry_change_history();

CREATE TRIGGER semantic_registry_change_audit_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_registry_change_audit
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_semantic_registry_change_history();

CREATE TRIGGER semantic_registry_change_operations_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_registry_change_operations
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_semantic_registry_change_operation();

CREATE TRIGGER semantic_registry_change_publication_source
AFTER INSERT
ON schemabridge_control.semantic_registry_change_proposals
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.register_registry_change_publication_source();

REVOKE ALL ON
    schemabridge_control.semantic_profile_sources,
    schemabridge_control.registry_join_profile_requests,
    schemabridge_control.registry_publication_sources,
    schemabridge_control.semantic_registry_change_drafts,
    schemabridge_control.semantic_registry_change_decisions,
    schemabridge_control.semantic_registry_change_proposals,
    schemabridge_control.semantic_registry_change_audit,
    schemabridge_control.semantic_registry_change_operations
    FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION
    schemabridge_control.register_semantic_change_profile_source(),
    schemabridge_control.guard_registry_join_profile_request(),
    schemabridge_control.guard_profile_job_connector_target(),
    schemabridge_control.enqueue_registry_join_profile(varchar, varchar, char, integer),
    schemabridge_control.load_registry_join_profile_job(varchar, varchar, char),
    schemabridge_control.register_onboarding_publication_source(),
    schemabridge_control.guard_semantic_registry_change_draft(),
    schemabridge_control.guard_semantic_registry_change_history(),
    schemabridge_control.guard_semantic_registry_change_operation(),
    schemabridge_control.register_registry_change_publication_source()
    FROM PUBLIC;

GRANT SELECT ON
    schemabridge_control.registry_join_profile_requests,
    schemabridge_control.semantic_registry_change_drafts,
    schemabridge_control.semantic_registry_change_decisions,
    schemabridge_control.semantic_registry_change_proposals,
    schemabridge_control.semantic_registry_change_audit,
    schemabridge_control.semantic_registry_change_operations
    TO schemabridge_api, schemabridge_publisher;

GRANT SELECT ON schemabridge_control.registry_publication_sources
    TO schemabridge_api, schemabridge_publisher, schemabridge_runtime;

GRANT SELECT ON schemabridge_control.semantic_profile_sources
    TO schemabridge_reconciler, schemabridge_worker;

GRANT INSERT ON schemabridge_control.registry_join_profile_requests
    TO schemabridge_api;

GRANT INSERT, UPDATE ON schemabridge_control.semantic_registry_change_drafts
    TO schemabridge_api;

GRANT INSERT ON
    schemabridge_control.semantic_registry_change_decisions,
    schemabridge_control.semantic_registry_change_proposals,
    schemabridge_control.semantic_registry_change_audit,
    schemabridge_control.semantic_registry_change_operations
    TO schemabridge_api;

GRANT UPDATE (response_job_id, response_job)
    ON schemabridge_control.semantic_registry_change_operations
    TO schemabridge_api;

GRANT EXECUTE ON FUNCTION schemabridge_control.enqueue_registry_join_profile(
    varchar,
    varchar,
    char,
    integer
) TO schemabridge_api;

GRANT EXECUTE ON FUNCTION schemabridge_control.load_registry_join_profile_job(
    varchar,
    varchar,
    char
) TO schemabridge_api;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.semantic_json_has_protected_keys(jsonb)
    TO schemabridge_api;

-- Phase B: an M33 replacement is authored as a distinct, immutable outer change.
-- Candidate relationship profiles are persisted before enqueue and retain every
-- aggregate-only witness so an expired observation can be refreshed safely.

CREATE TABLE schemabridge_control.registry_model_join_profile_requests (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 3 AND 200),
    request_id varchar(80) NOT NULL
        CHECK (request_id ~ '^[a-z][a-z0-9_-]{2,79}$'),
    change_id varchar(80) NOT NULL
        CHECK (change_id ~ '^[a-z][a-z0-9_-]{2,79}$'),
    scan_id varchar(80) NOT NULL
        CHECK (scan_id ~ '^scan_[0-9a-f]{64}$'),
    source_proposal_id varchar(200) NOT NULL,
    source_proposal_fingerprint char(64) NOT NULL
        CHECK (source_proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    catalog_scope varchar(120) NOT NULL,
    registry_id varchar(80) NOT NULL,
    base_registry_version bigint NOT NULL CHECK (base_registry_version >= 1),
    base_registry_fingerprint char(64) NOT NULL
        CHECK (base_registry_fingerprint ~ '^[0-9a-f]{64}$'),
    base_fingerprint char(64) NOT NULL
        CHECK (base_fingerprint ~ '^[0-9a-f]{64}$'),
    incident_join_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    proposal_fingerprint char(64) NOT NULL
        CHECK (proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    execution_target_fingerprint char(64) NOT NULL
        CHECK (execution_target_fingerprint ~ '^[0-9a-f]{64}$'),
    authoring_fingerprint char(64) NOT NULL
        CHECK (authoring_fingerprint ~ '^[0-9a-f]{64}$'),
    requested_by varchar(200) NOT NULL,
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 16777216
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = request_id
            AND payload->>'owner_actor_id' = requested_by
            AND payload->>'fingerprint' = authoring_fingerprint
            AND payload#>>'{request,workspace_id}' = workspace_id
            AND payload#>>'{request,change_id}' = change_id
            AND payload#>>'{request,scan_id}' = scan_id
            AND payload#>>'{request,replacement_source,proposal,id}' = source_proposal_id
            AND payload#>>'{request,replacement_source,proposal,fingerprint}'
                = source_proposal_fingerprint
            AND payload#>>'{request,base,scope,catalog_scope}' = catalog_scope
            AND payload#>>'{request,base,scope,registry_id}' = registry_id
            AND (payload#>>'{request,base,base_registry,registry_version}')::bigint
                = base_registry_version
            AND payload#>>'{request,base,base_registry,registry_fingerprint}'
                = base_registry_fingerprint
            AND payload#>>'{request,base,fingerprint}' = base_fingerprint
            AND payload#>>'{request,incident_join_id}' = incident_join_id
            AND payload#>>'{request,connection_id}' = connection_id
            AND payload#>>'{request,execution_target,target_fingerprint}'
                = execution_target_fingerprint
            AND payload#>>'{request,external_writes_performed}' = 'false'
            AND payload->>'external_writes_performed' = 'false'
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    requested_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, request_id),
    UNIQUE (workspace_id, scan_id),
    UNIQUE (workspace_id, requested_by, authoring_fingerprint),
    FOREIGN KEY (workspace_id, scan_id)
        REFERENCES schemabridge_control.semantic_profile_sources (
            workspace_id,
            scan_id
        ) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE schemabridge_control.registry_model_join_profile_audit (
    workspace_id varchar(200) NOT NULL,
    audit_id varchar(200) NOT NULL,
    request_id varchar(80) NOT NULL,
    event varchar(32) NOT NULL
        CHECK (event IN ('request_persisted', 'job_bound', 'witness_recorded')),
    actor_id varchar(200) NOT NULL,
    job_id varchar(80),
    witness_fingerprint char(64),
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 131072
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = audit_id
            AND payload->>'request_id' = request_id
            AND payload->>'event' = event
            AND payload->>'actor_id' = actor_id
            AND coalesce(payload->>'job_id', '') = coalesce(job_id, '')
            AND coalesce(payload->>'witness_fingerprint', '')
                = coalesce(witness_fingerprint::text, '')
            AND payload->>'fingerprint' = fingerprint
            AND payload->>'external_writes_performed' = 'false'
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, audit_id),
    UNIQUE (workspace_id, request_id, event),
    FOREIGN KEY (workspace_id, request_id)
        REFERENCES schemabridge_control.registry_model_join_profile_requests (
            workspace_id,
            request_id
        )
);

CREATE TABLE schemabridge_control.registry_model_join_profile_witnesses (
    workspace_id varchar(200) NOT NULL,
    request_id varchar(80) NOT NULL,
    change_id varchar(80) NOT NULL,
    source_proposal_id varchar(200) NOT NULL,
    incident_join_id varchar(200) NOT NULL,
    job_id varchar(80) NOT NULL
        CHECK (job_id ~ '^profile_job_[0-9a-f]{64}$'),
    result_fingerprint char(64) NOT NULL
        CHECK (result_fingerprint ~ '^[0-9a-f]{64}$'),
    witness_fingerprint char(64) NOT NULL
        CHECK (witness_fingerprint ~ '^[0-9a-f]{64}$'),
    completed_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > completed_at),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 16777216
            AND payload->>'change_id' = change_id
            AND payload->>'source_replacement_proposal_id' = source_proposal_id
            AND payload#>>'{proposal,id}' = incident_join_id
            AND payload#>>'{result,fingerprint}' = result_fingerprint
            AND (payload#>>'{result,completed_at}')::timestamptz = completed_at
            AND (payload->>'expires_at')::timestamptz = expires_at
            AND payload->>'fingerprint' = witness_fingerprint
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, request_id),
    UNIQUE (workspace_id, change_id, source_proposal_id, incident_join_id, completed_at),
    FOREIGN KEY (workspace_id, request_id)
        REFERENCES schemabridge_control.registry_model_join_profile_requests (
            workspace_id,
            request_id
        )
);

CREATE INDEX registry_model_join_profile_current_idx
    ON schemabridge_control.registry_model_join_profile_witnesses (
        workspace_id,
        change_id,
        source_proposal_id,
        incident_join_id,
        completed_at DESC,
        request_id DESC
    );

CREATE INDEX registry_model_join_profile_request_order_idx
    ON schemabridge_control.registry_model_join_profile_requests (
        workspace_id,
        change_id,
        source_proposal_id,
        incident_join_id,
        requested_at DESC,
        request_id DESC
    );

CREATE TABLE schemabridge_control.registry_model_join_profile_rejections (
    workspace_id varchar(200) NOT NULL,
    job_id varchar(80) NOT NULL,
    request_id varchar(80) NOT NULL,
    rejection_code varchar(64) NOT NULL CHECK (rejection_code = 'authority_stale'),
    rejected_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, job_id),
    FOREIGN KEY (workspace_id, request_id)
        REFERENCES schemabridge_control.registry_model_join_profile_requests (
            workspace_id,
            request_id
        )
);

CREATE TABLE schemabridge_control.registry_model_authority_reaper_sessions (
    backend_pid integer NOT NULL,
    transaction_id bigint NOT NULL,
    opened_at timestamptz NOT NULL,
    PRIMARY KEY (backend_pid, transaction_id)
);

CREATE TABLE schemabridge_control.registry_model_join_profile_operations (
    workspace_id varchar(200) NOT NULL,
    actor_id varchar(200) NOT NULL,
    idempotency_digest char(64) NOT NULL
        CHECK (idempotency_digest ~ '^[0-9a-f]{64}$'),
    operation varchar(64) NOT NULL
        CHECK (
            operation IN (
                'request_model_join_profile',
                'finalize_model_join_profile'
            )
        ),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    request_id varchar(80) NOT NULL,
    response_authoring jsonb NOT NULL,
    response_job_id varchar(80),
    response_job jsonb,
    response_witness jsonb,
    witness_fingerprint char(64),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, idempotency_digest),
    UNIQUE (workspace_id, actor_id, operation, request_fingerprint),
    FOREIGN KEY (workspace_id, request_id)
        REFERENCES schemabridge_control.registry_model_join_profile_requests (
            workspace_id,
            request_id
        ),
    CHECK (
        jsonb_typeof(response_authoring) = 'object'
        AND response_authoring->>'workspace_id' = workspace_id
        AND response_authoring->>'id' = request_id
        AND response_authoring->>'external_writes_performed' = 'false'
        AND NOT schemabridge_control.semantic_json_has_protected_keys(response_authoring)
    ),
    CHECK ((response_job_id IS NULL) = (response_job IS NULL)),
    CHECK ((witness_fingerprint IS NULL) = (response_witness IS NULL)),
    CHECK (
        response_job IS NULL
        OR (
            jsonb_typeof(response_job) = 'object'
            AND response_job->>'job_id' = response_job_id
            AND response_job->>'workspace_id' = workspace_id
            AND response_job->>'lease' IS NULL
            AND NOT schemabridge_control.semantic_json_has_protected_keys(response_job)
            AND (
                (
                    operation = 'request_model_join_profile'
                    AND response_job->>'status' = 'requested'
                    AND response_job->>'result' IS NULL
                )
                OR (
                    operation = 'finalize_model_join_profile'
                    AND response_job->>'status' = 'completed'
                    AND jsonb_typeof(response_job->'result') = 'object'
                    AND response_job#>>'{result,completed_at}' IS NOT NULL
                )
            )
        )
    ),
    CHECK (
        response_witness IS NULL
        OR (
            jsonb_typeof(response_witness) = 'object'
            AND response_witness->>'fingerprint' = witness_fingerprint
            AND NOT schemabridge_control.semantic_json_has_protected_keys(response_witness)
        )
    ),
    CHECK (
        (operation = 'request_model_join_profile' AND response_witness IS NULL)
        OR (
            operation = 'finalize_model_join_profile'
            AND response_job IS NOT NULL
            AND response_witness IS NOT NULL
        )
    )
);

CREATE TABLE schemabridge_control.semantic_registry_model_change_drafts (
    workspace_id varchar(200) NOT NULL,
    draft_id varchar(80) NOT NULL
        CHECK (draft_id ~ '^[a-z][a-z0-9_-]{2,79}$'),
    source_proposal_id varchar(200) NOT NULL,
    source_proposal_fingerprint char(64) NOT NULL
        CHECK (source_proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    catalog_scope varchar(120) NOT NULL,
    registry_id varchar(80) NOT NULL,
    owner_actor_id varchar(200) NOT NULL,
    status varchar(32) NOT NULL
        CHECK (status IN ('needs_review', 'approved', 'rejected', 'ready_for_publication')),
    revision bigint NOT NULL CHECK (revision >= 1),
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    prepared_proposal_id varchar(200),
    prepared_proposal_fingerprint char(64),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 16777216
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = draft_id
            AND payload#>>'{source,proposal,id}' = source_proposal_id
            AND payload#>>'{source,proposal,fingerprint}' = source_proposal_fingerprint
            AND payload#>>'{base,scope,catalog_scope}' = catalog_scope
            AND payload#>>'{base,scope,registry_id}' = registry_id
            AND payload->>'owner_actor_id' = owner_actor_id
            AND payload->>'status' = status
            AND (payload->>'revision')::bigint = revision
            AND payload->>'fingerprint' = fingerprint
            AND coalesce(payload->>'prepared_proposal_id', '')
                = coalesce(prepared_proposal_id, '')
            AND coalesce(payload->>'prepared_proposal_fingerprint', '')
                = coalesce(prepared_proposal_fingerprint::text, '')
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
    PRIMARY KEY (workspace_id, draft_id),
    UNIQUE (workspace_id, source_proposal_id),
    UNIQUE (workspace_id, draft_id, fingerprint),
    CHECK (
        (prepared_proposal_id IS NULL) = (prepared_proposal_fingerprint IS NULL)
    )
);

CREATE INDEX semantic_registry_model_change_owner_idx
    ON schemabridge_control.semantic_registry_model_change_drafts (
        workspace_id,
        owner_actor_id,
        updated_at DESC,
        draft_id DESC
    );

CREATE TABLE schemabridge_control.semantic_registry_model_change_decisions (
    workspace_id varchar(200) NOT NULL,
    decision_id varchar(200) NOT NULL,
    draft_id varchar(80) NOT NULL,
    resulting_revision bigint NOT NULL CHECK (resulting_revision >= 2),
    actor_id varchar(200) NOT NULL,
    action varchar(16) NOT NULL CHECK (action IN ('approve', 'reject')),
    status varchar(16) NOT NULL CHECK (status IN ('approved', 'rejected')),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 131072
            AND payload->>'id' = decision_id
            AND payload->>'actor' = actor_id
            AND payload->>'action' = action
            AND payload->>'status' = status
            AND (payload->>'resulting_version')::bigint = resulting_revision
            AND length(trim(payload->>'rationale')) >= 12
            AND jsonb_array_length(payload->'evidence') >= 1
            AND jsonb_array_length(payload->'risks') >= 1
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    decided_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, decision_id),
    UNIQUE (workspace_id, draft_id),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.semantic_registry_model_change_drafts (
            workspace_id,
            draft_id
        )
);

CREATE TABLE schemabridge_control.semantic_registry_model_change_proposals (
    workspace_id varchar(200) NOT NULL,
    proposal_id varchar(200) NOT NULL,
    proposal_kind varchar(40) NOT NULL CHECK (proposal_kind = 'replace_model_v1'),
    draft_id varchar(80) NOT NULL,
    draft_revision bigint NOT NULL CHECK (draft_revision >= 2),
    draft_fingerprint char(64) NOT NULL
        CHECK (draft_fingerprint ~ '^[0-9a-f]{64}$'),
    source_proposal_id varchar(200) NOT NULL,
    source_proposal_fingerprint char(64) NOT NULL
        CHECK (source_proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    target_registry_version bigint NOT NULL CHECK (target_registry_version >= 2),
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    prepared_by varchar(200) NOT NULL,
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 16777216
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = proposal_id
            AND payload->>'proposal_kind' = proposal_kind
            AND payload->>'draft_id' = draft_id
            AND (payload->>'draft_revision')::bigint = draft_revision
            AND payload->>'draft_fingerprint' = draft_fingerprint
            AND payload->>'source_replacement_proposal_id' = source_proposal_id
            AND payload->>'source_replacement_proposal_fingerprint'
                = source_proposal_fingerprint
            AND (payload->>'target_registry_version')::bigint
                = target_registry_version
            AND payload->>'fingerprint' = fingerprint
            AND payload->>'prepared_by' = prepared_by
            AND payload->>'external_writes_performed' = 'false'
            AND payload->>'owner_actor_id' <> prepared_by
            AND payload#>>'{replacement,prepared_by}' <> prepared_by
            AND payload#>>'{outer_decision,action}' = 'approve'
            AND payload#>>'{outer_decision,status}' = 'approved'
            AND payload#>>'{outer_decision,actor}' <> prepared_by
            AND payload#>>'{model_decision,actor}' <> prepared_by
            AND NOT jsonb_path_exists(
                payload,
                '$.mapping_decisions[*] ? (@.actor == $actor)',
                jsonb_build_object('actor', prepared_by)
            )
            AND NOT jsonb_path_exists(
                payload,
                '$.incident_join_changes[*] ? (@.decision.actor == $actor)',
                jsonb_build_object('actor', prepared_by)
            )
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    prepared_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, proposal_id),
    UNIQUE (workspace_id, draft_id),
    UNIQUE (workspace_id, source_proposal_id),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.semantic_registry_model_change_drafts (
            workspace_id,
            draft_id
        )
);

CREATE TABLE schemabridge_control.semantic_registry_model_change_audit (
    workspace_id varchar(200) NOT NULL,
    audit_id varchar(200) NOT NULL,
    draft_id varchar(80) NOT NULL,
    source_revision bigint NOT NULL CHECK (source_revision >= 0),
    resulting_revision bigint NOT NULL CHECK (resulting_revision >= 1),
    event varchar(32) NOT NULL
        CHECK (event IN ('draft_created', 'decision_recorded', 'publication_prepared')),
    actor_id varchar(200) NOT NULL,
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 131072
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = audit_id
            AND payload->>'change_id' = draft_id
            AND (payload->>'source_revision')::bigint = source_revision
            AND (payload->>'resulting_revision')::bigint = resulting_revision
            AND payload->>'event' = event
            AND payload->>'actor_id' = actor_id
            AND payload->>'fingerprint' = fingerprint
            AND payload->>'external_writes_performed' = 'false'
            AND NOT schemabridge_control.semantic_json_has_protected_keys(payload)
        ),
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, audit_id),
    UNIQUE (workspace_id, draft_id, event, resulting_revision),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.semantic_registry_model_change_drafts (
            workspace_id,
            draft_id
        )
);

CREATE TABLE schemabridge_control.semantic_registry_model_change_operations (
    workspace_id varchar(200) NOT NULL,
    actor_id varchar(200) NOT NULL,
    idempotency_digest char(64) NOT NULL
        CHECK (idempotency_digest ~ '^[0-9a-f]{64}$'),
    operation varchar(64) NOT NULL
        CHECK (
            operation IN (
                'create_model_change',
                'decide_model_change',
                'prepare_model_change_publication'
            )
        ),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    draft_id varchar(80) NOT NULL,
    response_revision bigint NOT NULL CHECK (response_revision >= 1),
    response_fingerprint char(64) NOT NULL
        CHECK (response_fingerprint ~ '^[0-9a-f]{64}$'),
    response_draft jsonb NOT NULL,
    response_proposal jsonb,
    proposal_id varchar(200),
    proposal_fingerprint char(64),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, idempotency_digest),
    UNIQUE (workspace_id, actor_id, operation, request_fingerprint),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.semantic_registry_model_change_drafts (
            workspace_id,
            draft_id
        ),
    CHECK (
        jsonb_typeof(response_draft) = 'object'
        AND response_draft->>'workspace_id' = workspace_id
        AND response_draft->>'id' = draft_id
        AND response_draft->>'fingerprint' = response_fingerprint
        AND (response_draft->>'revision')::bigint = response_revision
        AND NOT schemabridge_control.semantic_json_has_protected_keys(response_draft)
    ),
    CHECK ((response_proposal IS NULL) = (proposal_id IS NULL)),
    CHECK ((response_proposal IS NULL) = (proposal_fingerprint IS NULL)),
    CHECK (
        (operation <> 'prepare_model_change_publication' AND response_proposal IS NULL)
        OR (
            operation = 'prepare_model_change_publication'
            AND jsonb_typeof(response_proposal) = 'object'
            AND response_proposal->>'id' = proposal_id
            AND response_proposal->>'fingerprint' = proposal_fingerprint
            AND response_proposal->>'draft_id' = draft_id
            AND NOT schemabridge_control.semantic_json_has_protected_keys(response_proposal)
        )
    )
);

CREATE FUNCTION schemabridge_control.registry_model_authority_current(
    p_workspace_id varchar,
    p_base jsonb,
    p_source_proposal jsonb
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT (
    jsonb_typeof(p_base) = 'object'
    AND jsonb_typeof(p_source_proposal) = 'object'
    AND p_source_proposal->>'workspace_id' = p_workspace_id
    AND EXISTS (
        SELECT 1
        FROM schemabridge_control.semantic_onboarding_proposals AS proposal
        JOIN schemabridge_control.semantic_onboarding_drafts AS draft
          ON draft.workspace_id = proposal.workspace_id
         AND draft.draft_id = proposal.draft_id
         AND draft.status = 'ready_for_publication'
         AND draft.prepared_proposal_id = proposal.proposal_id
         AND draft.prepared_proposal_fingerprint = proposal.fingerprint
        JOIN schemabridge_control.registry_active_pointers AS pointer
          ON pointer.workspace_id = proposal.workspace_id
         AND pointer.catalog_scope = p_base#>>'{scope,catalog_scope}'
         AND pointer.registry_id = p_base#>>'{scope,registry_id}'
         AND pointer.generation
                = (p_base#>>'{base_registry,activation_generation}')::bigint
         AND pointer.registry_version
                = (p_base#>>'{base_registry,registry_version}')::bigint
         AND pointer.registry_fingerprint
                = p_base#>>'{base_registry,registry_fingerprint}'
         AND pointer.transition_id
                = p_base#>>'{dependency_context,pointer_transition_id}'
        JOIN schemabridge_control.semantic_dependency_index_states AS dependency
          ON dependency.workspace_id = pointer.workspace_id
         AND dependency.catalog_scope = pointer.catalog_scope
         AND dependency.registry_id = pointer.registry_id
         AND dependency.registry_generation = pointer.generation
         AND dependency.registry_version = pointer.registry_version
         AND dependency.registry_fingerprint = pointer.registry_fingerprint
         AND dependency.pointer_transition_id = pointer.transition_id
         AND dependency.watermark
                = (p_base#>>'{dependency_context,dependency_index,watermark}')::bigint
         AND dependency.index_fingerprint
                = p_base#>>'{dependency_context,dependency_index,fingerprint}'
         AND dependency.complete
        WHERE proposal.workspace_id = p_workspace_id
          AND proposal.proposal_id = p_source_proposal->>'id'
          AND proposal.fingerprint = p_source_proposal->>'fingerprint'
          AND proposal.payload = p_source_proposal
          AND proposal.payload#>'{base_registry}' = p_base#>'{base_registry}'
    )
    AND NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_base->'target_bindings') AS requested(binding)
        WHERE NOT EXISTS (
            SELECT 1
            FROM schemabridge_control.catalog_connections AS connection
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
              AND connection.catalog_scope = p_base#>>'{scope,catalog_scope}'
              AND connection.connection_id = requested.binding->>'connection_id'
              AND connection.status = 'enabled'
              AND connection.active_generation
                    = (requested.binding->>'catalog_generation')::bigint
              AND connection.active_generation_fingerprint
                    = requested.binding->>'catalog_generation_fingerprint'
              AND asset.asset_id = requested.binding#>>'{locator,asset,asset_id}'
              AND asset.asset_id = requested.binding->>'observed_datahub_asset_urn'
              AND asset.metadata_fingerprint
                    = requested.binding->>'asset_metadata_fingerprint'
              AND to_jsonb(field.field_path)
                    = requested.binding#>'{locator,field_path}'
              AND field.metadata_fingerprint
                    = requested.binding->>'field_metadata_fingerprint'
              AND field.normalized_type = requested.binding->>'physical_type'
        )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_source_proposal->'mappings') AS requested(mapping)
        WHERE NOT EXISTS (
            SELECT 1
            FROM schemabridge_control.catalog_connections AS connection
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
              AND connection.catalog_scope
                    = requested.mapping#>>'{observation,catalog_scope}'
              AND connection.connection_id
                    = requested.mapping#>>'{observation,locator,asset,connection_id}'
              AND connection.status = 'enabled'
              AND connection.active_generation
                    = (requested.mapping#>>'{observation,generation}')::bigint
              AND connection.active_generation_fingerprint
                    = requested.mapping#>>'{observation,generation_fingerprint}'
              AND asset.asset_id
                    = requested.mapping#>>'{observation,locator,asset,asset_id}'
              AND asset.asset_id
                    = requested.mapping#>>'{observation,observed_datahub_asset_urn}'
              AND asset.metadata_fingerprint
                    = requested.mapping#>>'{observation,asset_metadata_fingerprint}'
              AND to_jsonb(field.field_path)
                    = requested.mapping#>'{observation,locator,field_path}'
              AND field.metadata_fingerprint
                    = requested.mapping#>>'{observation,field_metadata_fingerprint}'
              AND field.normalized_type
                    = requested.mapping#>>'{observation,physical_type}'
        )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_base->'incident_joins') AS requested(incident)
        CROSS JOIN LATERAL (
            VALUES (requested.incident->'left'), (requested.incident->'right')
        ) AS endpoint(value)
        WHERE endpoint.value#>>'{key,logical_field}'
                IS DISTINCT FROM endpoint.value#>>'{mapping,mapping,logical_field}'
           OR endpoint.value#>>'{key,physical_field}'
                IS DISTINCT FROM endpoint.value#>>'{mapping,mapping,physical_field}'
           OR endpoint.value#>'{key,transformation_plan}'
                IS DISTINCT FROM endpoint.value#>'{mapping,mapping,transformation_plan}'
           OR endpoint.value#>>'{key,logical_field}'
                IS DISTINCT FROM endpoint.value#>>'{binding,logical_field}'
           OR endpoint.value#>>'{key,physical_field}'
                IS DISTINCT FROM endpoint.value#>>'{binding,physical_field}'
           OR endpoint.value#>>'{mapping,physical_type}'
                IS DISTINCT FROM endpoint.value#>>'{binding,physical_type}'
           OR endpoint.value#>>'{binding,workspace_id}' IS DISTINCT FROM p_workspace_id
           OR endpoint.value#>>'{binding,catalog_scope}'
                IS DISTINCT FROM p_base#>>'{scope,catalog_scope}'
           OR endpoint.value#>>'{binding,connection_id}'
                IS DISTINCT FROM endpoint.value#>>'{binding,locator,asset,connection_id}'
           OR NOT EXISTS (
                SELECT 1
                FROM schemabridge_control.catalog_connections AS connection
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
                  AND connection.catalog_scope = p_base#>>'{scope,catalog_scope}'
                  AND connection.connection_id
                        = endpoint.value#>>'{binding,connection_id}'
                  AND connection.status = 'enabled'
                  AND connection.active_generation
                        = (endpoint.value#>>'{binding,catalog_generation}')::bigint
                  AND connection.active_generation_fingerprint
                        = endpoint.value#>>'{binding,catalog_generation_fingerprint}'
                  AND asset.asset_id
                        = endpoint.value#>>'{binding,locator,asset,asset_id}'
                  AND asset.asset_id
                        = endpoint.value#>>'{binding,observed_datahub_asset_urn}'
                  AND asset.metadata_fingerprint
                        = endpoint.value#>>'{binding,asset_metadata_fingerprint}'
                  AND to_jsonb(field.field_path)
                        = endpoint.value#>'{binding,locator,field_path}'
                  AND field.metadata_fingerprint
                        = endpoint.value#>>'{binding,field_metadata_fingerprint}'
                  AND field.normalized_type
                        = endpoint.value#>>'{binding,physical_type}'
                  AND endpoint.value#>>'{binding,physical_field}' = concat_ws(
                      '.',
                      asset.schema_name,
                      asset.table_name,
                      array_to_string(field.field_path, '.')
                  )
           )
    )
)
$$;

CREATE FUNCTION schemabridge_control.registry_model_profile_proposal_authorized(
    p_request jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
WITH exact_incident AS (
    SELECT incident
    FROM jsonb_array_elements(p_request#>'{request,base,incident_joins}') AS item(incident)
    WHERE incident#>>'{contract,id}' = p_request#>>'{request,incident_join_id}'
), checked AS (
    SELECT
        incident,
        p_request#>'{request,proposal,left_key}' AS left_key,
        p_request#>'{request,proposal,right_key}' AS right_key,
        p_request#>>'{request,base,target_model,id}' AS target_model_id,
        p_request#>>'{request,connection_id}' AS connection_id
    FROM exact_incident
)
SELECT (
    (SELECT count(*) FROM checked) = 1
    AND p_request#>>'{request,proposal,id}'
        = p_request#>>'{request,incident_join_id}'
    AND EXISTS (
        SELECT 1
        FROM checked
        WHERE target_model_id IN (
            incident#>>'{summary,left_model}',
            incident#>>'{summary,right_model}'
        )
          AND (
              (
                  split_part(left_key->>'logical_field', '.', 1) = target_model_id
                  AND EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements(
                          p_request#>'{request,replacement_source,proposal,mappings}'
                      ) AS candidate(mapping)
                      WHERE candidate.mapping->>'logical_field'
                                = left_key->>'logical_field'
                        AND candidate.mapping#>>'{observation,physical_field}'
                                = left_key->>'physical_field'
                        AND candidate.mapping->'transformation_plan'
                                = left_key->'transformation_plan'
                        AND candidate.mapping#>>'{observation,locator,asset,connection_id}'
                                = connection_id
                  )
              )
              OR (
                  split_part(left_key->>'logical_field', '.', 1) <> target_model_id
                  AND incident#>'{left,key}' = left_key
                  AND incident#>>'{left,binding,connection_id}' = connection_id
              )
          )
          AND (
              (
                  split_part(right_key->>'logical_field', '.', 1) = target_model_id
                  AND EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements(
                          p_request#>'{request,replacement_source,proposal,mappings}'
                      ) AS candidate(mapping)
                      WHERE candidate.mapping->>'logical_field'
                                = right_key->>'logical_field'
                        AND candidate.mapping#>>'{observation,physical_field}'
                                = right_key->>'physical_field'
                        AND candidate.mapping->'transformation_plan'
                                = right_key->'transformation_plan'
                        AND candidate.mapping#>>'{observation,locator,asset,connection_id}'
                                = connection_id
                  )
              )
              OR (
                  split_part(right_key->>'logical_field', '.', 1) <> target_model_id
                  AND incident#>'{right,key}' = right_key
                  AND incident#>>'{right,binding,connection_id}' = connection_id
              )
          )
    )
)
$$;

CREATE FUNCTION schemabridge_control.guard_registry_model_history()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'registry model history is append-only' USING ERRCODE = '55000';
    END IF;
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model history role is invalid' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_registry_model_draft()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    requested_binding record;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model draft role is invalid' USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'registry model draft cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF NOT schemabridge_control.registry_model_authority_current(
        NEW.workspace_id,
        NEW.payload->'base',
        NEW.payload#>'{source,proposal}'
    ) THEN
        RAISE EXCEPTION 'registry model draft authority is stale'
            USING ERRCODE = '55000';
    END IF;
    FOR requested_binding IN
        SELECT binding
        FROM jsonb_array_elements(NEW.payload#>'{base,target_bindings}') AS item(binding)
    LOOP
        PERFORM 1
        FROM schemabridge_control.catalog_connections AS connection
        WHERE connection.workspace_id = NEW.workspace_id
          AND connection.catalog_scope = NEW.catalog_scope
          AND connection.connection_id = requested_binding.binding->>'connection_id'
          AND connection.status = 'enabled'
          AND connection.active_generation
                = (requested_binding.binding->>'catalog_generation')::bigint
          AND connection.active_generation_fingerprint
                = requested_binding.binding->>'catalog_generation_fingerprint'
        FOR SHARE OF connection;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'registry model catalog authority is stale'
                USING ERRCODE = '55000';
        END IF;
    END LOOP;
    IF NEW.payload#>>'{authority,kind}' = 'm26_remediation' AND NOT EXISTS (
        SELECT 1
        FROM schemabridge_control.semantic_change_reports AS report
        JOIN schemabridge_control.semantic_change_heads AS head
          ON head.workspace_id = report.workspace_id
         AND head.catalog_scope = report.catalog_scope
         AND head.registry_id = report.registry_id
         AND head.current_report_id = report.report_id
         AND head.current_report_fingerprint = report.report_fingerprint
         AND head.state = 'blocked'
         AND head.registry_generation
                = (NEW.payload#>>'{base,base_registry,activation_generation}')::bigint
         AND head.registry_version
                = (NEW.payload#>>'{base,base_registry,registry_version}')::bigint
         AND head.registry_fingerprint
                = NEW.payload#>>'{base,base_registry,registry_fingerprint}'
         AND head.pointer_transition_id
                = NEW.payload#>>'{base,dependency_context,pointer_transition_id}'
         AND head.dependency_index_watermark
                = (NEW.payload#>>'{base,dependency_context,dependency_index,watermark}')::bigint
         AND head.dependency_index_fingerprint
                = NEW.payload#>>'{base,dependency_context,dependency_index,fingerprint}'
         AND head.dependency_index_complete
        WHERE report.workspace_id = NEW.workspace_id
          AND report.catalog_scope = NEW.catalog_scope
          AND report.registry_id = NEW.registry_id
          AND report.report_id = NEW.payload#>>'{authority,report,id}'
          AND report.report_fingerprint = NEW.payload#>>'{authority,report,fingerprint}'
          AND report.context_fingerprint
                = NEW.payload#>>'{base,dependency_context,fingerprint}'
          AND report.outcome = 'blocked'
          AND report.report_json = NEW.payload#>'{authority,report}'
          AND report.impact_set_fingerprint
                = NEW.payload#>>'{authority,impacts,fingerprint}'
          AND report.impact_count
                = jsonb_array_length(NEW.payload#>'{authority,impacts,impacts}')
          AND coalesce(
              (
                  SELECT jsonb_agg(
                      persisted.impact_json
                      ORDER BY
                          persisted.artifact_kind,
                          persisted.artifact_id,
                          coalesce(persisted.artifact_version, 0)
                  )
                  FROM schemabridge_control.semantic_change_impacts AS persisted
                  WHERE persisted.workspace_id = report.workspace_id
                    AND persisted.report_id = report.report_id
              ),
              '[]'::jsonb
          ) = NEW.payload#>'{authority,impacts,impacts}'
          AND NOT EXISTS (
              SELECT 1
              FROM schemabridge_control.semantic_change_reports AS newer
              WHERE newer.workspace_id = report.workspace_id
                AND newer.catalog_scope = report.catalog_scope
                AND newer.registry_id = report.registry_id
                AND newer.context_fingerprint = report.context_fingerprint
                AND (newer.inspected_at, newer.report_id)
                    > (report.inspected_at, report.report_id)
          )
          AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements(
                  NEW.payload#>'{authority,impacts,impacts}'
              ) AS requested(impact)
              WHERE NOT EXISTS (
                  SELECT 1
                  FROM schemabridge_control.semantic_change_impacts AS persisted
                  WHERE persisted.workspace_id = report.workspace_id
                    AND persisted.report_id = report.report_id
                    AND persisted.impact_json = requested.impact
              )
          )
    ) THEN
        RAISE EXCEPTION 'registry model remediation authority is stale'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(NEW.payload->'incident_intents') AS intent(value)
        WHERE intent.value->>'action' = 'upsert_fresh'
          AND NOT EXISTS (
              SELECT 1
              FROM schemabridge_control.registry_model_join_profile_witnesses AS witness
              JOIN schemabridge_control.registry_model_join_profile_requests AS request
                ON request.workspace_id = witness.workspace_id
               AND request.request_id = witness.request_id
              WHERE witness.workspace_id = NEW.workspace_id
                AND witness.change_id = NEW.draft_id
                AND witness.source_proposal_id = NEW.source_proposal_id
                AND witness.incident_join_id = intent.value->>'base_join_id'
                AND witness.payload = intent.value->'profile_witness'
                AND witness.expires_at > clock_timestamp()
                AND NOT EXISTS (
                    SELECT 1
                    FROM schemabridge_control.registry_model_join_profile_witnesses AS newer_witness
                    JOIN schemabridge_control.registry_model_join_profile_requests AS newer_request
                      ON newer_request.workspace_id = newer_witness.workspace_id
                     AND newer_request.request_id = newer_witness.request_id
                    WHERE newer_witness.workspace_id = witness.workspace_id
                      AND newer_witness.change_id = witness.change_id
                      AND newer_witness.source_proposal_id = witness.source_proposal_id
                      AND newer_witness.incident_join_id = witness.incident_join_id
                      AND (newer_request.requested_at, newer_request.request_id)
                            > (request.requested_at, request.request_id)
                )
          )
    ) THEN
        RAISE EXCEPTION 'registry model profile witness is stale'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'needs_review' OR NEW.revision <> 1 THEN
            RAISE EXCEPTION 'new registry model draft is invalid' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.draft_id IS DISTINCT FROM OLD.draft_id
        OR NEW.source_proposal_id IS DISTINCT FROM OLD.source_proposal_id
        OR NEW.source_proposal_fingerprint IS DISTINCT FROM OLD.source_proposal_fingerprint
        OR NEW.catalog_scope IS DISTINCT FROM OLD.catalog_scope
        OR NEW.registry_id IS DISTINCT FROM OLD.registry_id
        OR NEW.owner_actor_id IS DISTINCT FROM OLD.owner_actor_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.updated_at <= OLD.updated_at
        OR NEW.revision <> OLD.revision + 1
        OR NEW.fingerprint = OLD.fingerprint
        OR NOT (
            (OLD.status = 'needs_review' AND NEW.status IN ('approved', 'rejected'))
            OR (OLD.status = 'approved' AND NEW.status = 'ready_for_publication')
        )
    ) THEN
        RAISE EXCEPTION 'registry model draft transition is invalid' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_registry_model_profile_request()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'registry model profile request is immutable' USING ERRCODE = '55000';
    END IF;
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model profile request role is invalid' USING ERRCODE = '42501';
    END IF;
    IF NEW.requested_at > clock_timestamp() + interval '1 minute' THEN
        RAISE EXCEPTION 'registry model profile request time is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF (
        NOT schemabridge_control.registry_model_profile_proposal_authorized(NEW.payload)
        OR NOT schemabridge_control.registry_model_authority_current(
            NEW.workspace_id,
            NEW.payload#>'{request,base}',
            NEW.payload#>'{request,replacement_source,proposal}'
        )
    ) THEN
        RAISE EXCEPTION 'registry model profile request authority is stale or invalid'
            USING ERRCODE = '55000';
    END IF;
    INSERT INTO schemabridge_control.semantic_profile_sources (
        workspace_id, scan_id, source_kind, source_fingerprint, created_at
    ) VALUES (
        NEW.workspace_id,
        NEW.scan_id,
        'registry_model_join_profile_v1',
        NEW.authoring_fingerprint,
        NEW.requested_at
    ) ON CONFLICT DO NOTHING;
    PERFORM 1 FROM schemabridge_control.semantic_profile_sources AS source
    WHERE source.workspace_id = NEW.workspace_id
      AND source.scan_id = NEW.scan_id
      AND source.source_kind = 'registry_model_join_profile_v1'
      AND source.source_fingerprint = NEW.authoring_fingerprint
      AND source.created_at = NEW.requested_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'registry model profile source conflicts' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_registry_model_profile_operation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model profile operation role is invalid' USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'registry model profile operation cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF (
            NEW.response_job IS NOT NULL
            AND NEW.operation = 'request_model_join_profile'
        ) OR NOT EXISTS (
            SELECT 1
            FROM schemabridge_control.registry_model_join_profile_requests AS request
            JOIN schemabridge_control.registry_model_join_profile_audit AS audit
              ON audit.workspace_id = request.workspace_id
             AND audit.request_id = request.request_id
             AND audit.actor_id = NEW.actor_id
             AND audit.event = CASE NEW.operation
                 WHEN 'request_model_join_profile' THEN 'request_persisted'
                 ELSE 'witness_recorded'
             END
            WHERE request.workspace_id = NEW.workspace_id
              AND request.request_id = NEW.request_id
              AND request.payload = NEW.response_authoring
              AND (
                  NEW.operation = 'request_model_join_profile'
                  OR EXISTS (
                      SELECT 1
                      FROM schemabridge_control.registry_model_join_profile_witnesses AS witness
                      WHERE witness.workspace_id = NEW.workspace_id
                        AND witness.request_id = NEW.request_id
                        AND witness.payload = NEW.response_witness
                        AND witness.witness_fingerprint = NEW.witness_fingerprint
                  )
              )
        ) THEN
            RAISE EXCEPTION 'registry model profile operation witness is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF (
        OLD.operation <> 'request_model_join_profile'
        OR OLD.response_job IS NOT NULL
        OR NEW.response_job IS NULL
        OR NEW.response_witness IS NOT NULL
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
        OR NEW.idempotency_digest IS DISTINCT FROM OLD.idempotency_digest
        OR NEW.operation IS DISTINCT FROM OLD.operation
        OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
        OR NEW.request_id IS DISTINCT FROM OLD.request_id
        OR NEW.response_authoring IS DISTINCT FROM OLD.response_authoring
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NOT EXISTS (
            SELECT 1
            FROM schemabridge_control.registry_model_join_profile_requests AS request
            JOIN schemabridge_control.semantic_join_profile_jobs AS job
              ON job.workspace_id = request.workspace_id
             AND job.scan_id = request.scan_id
             AND job.proposal_fingerprint = request.proposal_fingerprint
            JOIN schemabridge_control.registry_model_join_profile_audit AS audit
              ON audit.workspace_id = request.workspace_id
             AND audit.request_id = request.request_id
             AND audit.event = 'job_bound'
             AND audit.actor_id = NEW.actor_id
             AND audit.job_id = job.job_id
            WHERE request.workspace_id = NEW.workspace_id
              AND request.request_id = NEW.request_id
              AND job.job_id = NEW.response_job_id
              AND job.status = 'requested'
        )
    ) THEN
        RAISE EXCEPTION 'registry model profile job binding is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_registry_model_profile_witness()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'registry model profile witness is append-only'
            USING ERRCODE = '55000';
    END IF;
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model profile witness role is invalid'
            USING ERRCODE = '42501';
    END IF;
    PERFORM 1
    FROM schemabridge_control.registry_model_join_profile_requests AS request
    JOIN schemabridge_control.semantic_join_profile_jobs AS job
      ON job.workspace_id = request.workspace_id
     AND job.scan_id = request.scan_id
     AND job.proposal_fingerprint = request.proposal_fingerprint
     AND job.job_id = NEW.job_id
     AND job.status = 'completed'
     AND job.result_profile_fingerprint = NEW.result_fingerprint
     AND job.completed_at = NEW.completed_at
    JOIN schemabridge_control.registry_model_join_profile_operations AS operation
      ON operation.workspace_id = request.workspace_id
     AND operation.request_id = request.request_id
     AND operation.operation = 'request_model_join_profile'
     AND operation.response_job_id = job.job_id
    JOIN schemabridge_control.registry_model_join_profile_audit AS audit
      ON audit.workspace_id = request.workspace_id
     AND audit.request_id = request.request_id
     AND audit.event = 'job_bound'
     AND audit.job_id = job.job_id
     AND audit.actor_id = operation.actor_id
    WHERE request.workspace_id = NEW.workspace_id
      AND request.request_id = NEW.request_id
      AND request.change_id = NEW.change_id
      AND request.source_proposal_id = NEW.source_proposal_id
      AND request.incident_join_id = NEW.incident_join_id
      AND NEW.payload#>'{scope}' = request.payload#>'{request,base,scope}'
      AND NEW.payload#>'{base_registry}'
            = request.payload#>'{request,base,base_registry}'
      AND NEW.payload->>'base_fingerprint' = request.base_fingerprint
      AND NEW.payload#>>'{target_model_id}'
            = request.payload#>>'{request,base,target_model,id}'
      AND NEW.payload#>>'{target_model_version}'
            = request.payload#>>'{request,replacement_source,proposal,model,definition,version}'
      AND NEW.payload->>'connection_id' = request.connection_id
      AND NEW.payload#>'{proposal}' = request.payload#>'{request,proposal}'
      AND NEW.payload#>'{execution_target}'
            = request.payload#>'{request,execution_target}'
      AND NEW.payload#>'{result,profile}' = job.result_profile_json
      AND NEW.payload#>>'{result,fingerprint}' = job.result_profile_fingerprint
      AND (NEW.payload#>>'{result,completed_at}')::timestamptz = job.completed_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'registry model profile witness crossed its durable request'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_registry_model_profile_rejection()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'registry model profile rejection is append-only'
            USING ERRCODE = '55000';
    END IF;
    IF SESSION_USER NOT IN ('schemabridge_worker', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model profile rejection role is invalid'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_registry_model_operation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'registry model operation is immutable' USING ERRCODE = '55000';
    END IF;
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model operation role is invalid' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM schemabridge_control.semantic_registry_model_change_drafts AS draft
        JOIN schemabridge_control.semantic_registry_model_change_audit AS audit
          ON audit.workspace_id = draft.workspace_id
         AND audit.draft_id = draft.draft_id
         AND audit.actor_id = NEW.actor_id
         AND audit.event = CASE NEW.operation
             WHEN 'create_model_change' THEN 'draft_created'
             WHEN 'decide_model_change' THEN 'decision_recorded'
             ELSE 'publication_prepared'
         END
         AND audit.resulting_revision = NEW.response_revision
        WHERE draft.workspace_id = NEW.workspace_id
          AND draft.draft_id = NEW.draft_id
          AND draft.payload = NEW.response_draft
          AND (
              NEW.response_proposal IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM schemabridge_control.semantic_registry_model_change_proposals AS proposal
                  WHERE proposal.workspace_id = NEW.workspace_id
                    AND proposal.proposal_id = NEW.proposal_id
                    AND proposal.payload = NEW.response_proposal
              )
          )
    ) THEN
        RAISE EXCEPTION 'registry model operation witness is invalid' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_registry_model_decision()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'registry model decision is append-only' USING ERRCODE = '55000';
    END IF;
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model decision role is invalid' USING ERRCODE = '42501';
    END IF;
    PERFORM 1
    FROM schemabridge_control.semantic_registry_model_change_drafts AS draft
    WHERE draft.workspace_id = NEW.workspace_id
      AND draft.draft_id = NEW.draft_id
      AND draft.revision = NEW.resulting_revision
      AND draft.payload->'outer_decision' = NEW.payload
      AND draft.payload->>'reviewed_by' = NEW.actor_id
      AND (draft.payload->>'reviewed_at')::timestamptz = NEW.decided_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'registry model decision crossed its draft' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_registry_model_proposal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'registry model proposal is append-only' USING ERRCODE = '55000';
    END IF;
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model proposal role is invalid' USING ERRCODE = '42501';
    END IF;
    PERFORM 1
    FROM schemabridge_control.semantic_registry_model_change_drafts AS draft
    JOIN schemabridge_control.semantic_registry_model_change_decisions AS decision
      ON decision.workspace_id = draft.workspace_id
     AND decision.draft_id = draft.draft_id
     AND decision.action = 'approve'
     AND decision.status = 'approved'
     AND decision.payload = NEW.payload->'outer_decision'
    JOIN schemabridge_control.semantic_registry_model_change_audit AS audit
      ON audit.workspace_id = draft.workspace_id
     AND audit.draft_id = draft.draft_id
     AND audit.event = 'publication_prepared'
     AND audit.payload->>'proposal_id' = NEW.proposal_id
     AND audit.source_revision = NEW.draft_revision
     AND audit.payload->>'previous_fingerprint' = NEW.draft_fingerprint
     AND audit.resulting_revision = draft.revision
    WHERE draft.workspace_id = NEW.workspace_id
      AND draft.draft_id = NEW.draft_id
      AND draft.status = 'ready_for_publication'
      AND draft.prepared_proposal_id = NEW.proposal_id
      AND draft.prepared_proposal_fingerprint = NEW.fingerprint
      AND draft.source_proposal_id = NEW.source_proposal_id
      AND draft.source_proposal_fingerprint = NEW.source_proposal_fingerprint
      AND draft.payload#>'{source,proposal}' = NEW.payload->'replacement'
      AND draft.owner_actor_id = NEW.payload->>'owner_actor_id'
      AND draft.payload->'base' = NEW.payload->'base'
      AND draft.payload->'outer_decision' = NEW.payload->'outer_decision'
      AND draft.payload->'model_decision' = NEW.payload->'model_decision'
      AND draft.payload->'mapping_decisions' = NEW.payload->'mapping_decisions'
      AND draft.payload->'authority' = NEW.payload->'authority'
      AND draft.payload->'incident_changes' = NEW.payload->'incident_join_changes'
      AND draft.payload->'risks' = NEW.payload->'risks'
      AND draft.payload->>'created_at' = NEW.payload->>'created_at'
      AND draft.payload->>'prepared_by' = NEW.prepared_by
      AND draft.updated_at = NEW.prepared_at
      AND NEW.draft_revision = draft.revision - 1
      AND schemabridge_control.registry_model_authority_current(
          draft.workspace_id,
          draft.payload->'base',
          draft.payload#>'{source,proposal}'
      );
    IF NOT FOUND THEN
        RAISE EXCEPTION 'registry model proposal crossed its approved draft'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER registry_model_profile_requests_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.registry_model_join_profile_requests
FOR EACH ROW EXECUTE FUNCTION schemabridge_control.guard_registry_model_profile_request();

CREATE TRIGGER registry_model_profile_audit_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.registry_model_join_profile_audit
FOR EACH ROW EXECUTE FUNCTION schemabridge_control.guard_registry_model_history();

CREATE TRIGGER registry_model_profile_witnesses_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.registry_model_join_profile_witnesses
FOR EACH ROW EXECUTE FUNCTION schemabridge_control.guard_registry_model_profile_witness();

CREATE TRIGGER registry_model_profile_operations_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.registry_model_join_profile_operations
FOR EACH ROW EXECUTE FUNCTION schemabridge_control.guard_registry_model_profile_operation();

CREATE TRIGGER registry_model_profile_rejections_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.registry_model_join_profile_rejections
FOR EACH ROW EXECUTE FUNCTION schemabridge_control.guard_registry_model_profile_rejection();

CREATE TRIGGER semantic_registry_model_drafts_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_registry_model_change_drafts
FOR EACH ROW EXECUTE FUNCTION schemabridge_control.guard_registry_model_draft();

CREATE TRIGGER semantic_registry_model_proposals_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_registry_model_change_proposals
FOR EACH ROW EXECUTE FUNCTION schemabridge_control.guard_registry_model_proposal();

CREATE TRIGGER semantic_registry_model_change_publication_source
AFTER INSERT
ON schemabridge_control.semantic_registry_model_change_proposals
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.register_registry_change_publication_source();

CREATE TRIGGER semantic_registry_model_decisions_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_registry_model_change_decisions
FOR EACH ROW EXECUTE FUNCTION schemabridge_control.guard_registry_model_decision();

CREATE TRIGGER semantic_registry_model_audit_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_registry_model_change_audit
FOR EACH ROW EXECUTE FUNCTION schemabridge_control.guard_registry_model_history();

CREATE TRIGGER semantic_registry_model_operations_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_registry_model_change_operations
FOR EACH ROW EXECUTE FUNCTION schemabridge_control.guard_registry_model_operation();

-- Migration 0014 validated every publication INSERT against the only proposal family that
-- existed at that point. Preserve its transition guard byte-for-byte, but split INSERT authority
-- so the closed v15 proposal kinds are checked against their own immutable draft/proposal tables.
ALTER FUNCTION schemabridge_control.enforce_registry_publication_job_lifecycle()
    RENAME TO enforce_registry_publication_job_transition;

DROP TRIGGER registry_publication_jobs_lifecycle
    ON schemabridge_control.registry_publication_jobs;

CREATE FUNCTION schemabridge_control.enforce_registry_publication_job_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    proposal_kind_value varchar(40);
    proposal_record record;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'registry publication insert guard operation is invalid'
            USING ERRCODE = '55000';
    END IF;
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

    proposal_kind_value := coalesce(
        NEW.payload#>>'{proposal,proposal_kind}',
        'onboarding_additive_v1'
    );
    IF proposal_kind_value NOT IN (
        'onboarding_additive_v1',
        'add_join_v1',
        'replace_model_v1'
    ) THEN
        RAISE EXCEPTION 'registry publication proposal kind is invalid'
            USING ERRCODE = '55000';
    END IF;

    PERFORM 1
    FROM schemabridge_control.registry_publication_sources AS source
    WHERE source.workspace_id = NEW.workspace_id
      AND source.proposal_kind = proposal_kind_value
      AND source.proposal_id = NEW.proposal_id
      AND source.proposal_fingerprint = NEW.proposal_fingerprint
      AND source.catalog_scope = NEW.catalog_scope
      AND source.registry_id = NEW.registry_id
      AND source.target_version = NEW.target_version;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'registry publication source authority is invalid'
            USING ERRCODE = '55000';
    END IF;

    IF proposal_kind_value = 'onboarding_additive_v1' THEN
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
    ELSIF proposal_kind_value = 'add_join_v1' THEN
        SELECT proposal.fingerprint,
               proposal.target_registry_version,
               proposal.payload,
               proposal.payload#>>'{scope,catalog_scope}' AS catalog_scope,
               proposal.payload#>>'{scope,registry_id}' AS registry_id,
               draft.status AS draft_status,
               draft.prepared_proposal_id,
               draft.prepared_proposal_fingerprint
        INTO proposal_record
        FROM schemabridge_control.semantic_registry_change_proposals AS proposal
        JOIN schemabridge_control.semantic_registry_change_drafts AS draft
          ON draft.workspace_id = proposal.workspace_id
         AND draft.draft_id = proposal.draft_id
        WHERE proposal.workspace_id = NEW.workspace_id
          AND proposal.proposal_id = NEW.proposal_id
        FOR SHARE OF draft;
    ELSE
        SELECT proposal.fingerprint,
               proposal.target_registry_version,
               proposal.payload,
               proposal.payload#>>'{scope,catalog_scope}' AS catalog_scope,
               proposal.payload#>>'{scope,registry_id}' AS registry_id,
               draft.status AS draft_status,
               draft.prepared_proposal_id,
               draft.prepared_proposal_fingerprint
        INTO proposal_record
        FROM schemabridge_control.semantic_registry_model_change_proposals AS proposal
        JOIN schemabridge_control.semantic_registry_model_change_drafts AS draft
          ON draft.workspace_id = proposal.workspace_id
         AND draft.draft_id = proposal.draft_id
        WHERE proposal.workspace_id = NEW.workspace_id
          AND proposal.proposal_id = NEW.proposal_id
        FOR SHARE OF draft;
    END IF;

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
END;
$$;

CREATE TRIGGER registry_publication_jobs_lifecycle
BEFORE INSERT
ON schemabridge_control.registry_publication_jobs
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.enforce_registry_publication_job_lifecycle();

CREATE TRIGGER registry_publication_jobs_transition
BEFORE UPDATE OR DELETE
ON schemabridge_control.registry_publication_jobs
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.enforce_registry_publication_job_transition();

CREATE FUNCTION schemabridge_control.enqueue_registry_model_join_profile(
    p_workspace_id varchar,
    p_scan_id varchar,
    p_authoring_fingerprint char(64),
    p_max_attempts integer DEFAULT 5
)
RETURNS varchar(80)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    request_record record;
    computed_job_id varchar(80);
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model profile enqueue role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF p_max_attempts NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'registry model profile enqueue input is invalid'
            USING ERRCODE = '22023';
    END IF;
    SELECT request.* INTO request_record
    FROM schemabridge_control.registry_model_join_profile_requests AS request
    JOIN schemabridge_control.registry_model_join_profile_operations AS operation
      ON operation.workspace_id = request.workspace_id
     AND operation.request_id = request.request_id
     AND operation.operation = 'request_model_join_profile'
     AND operation.response_authoring = request.payload
    JOIN schemabridge_control.registry_model_join_profile_audit AS audit
      ON audit.workspace_id = request.workspace_id
     AND audit.request_id = request.request_id
     AND audit.event = 'request_persisted'
     AND audit.actor_id = operation.actor_id
    WHERE request.workspace_id = p_workspace_id
      AND request.scan_id = p_scan_id
      AND request.authoring_fingerprint = p_authoring_fingerprint
    FOR SHARE OF request;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'registry model profile request is unavailable'
            USING ERRCODE = '55000';
    END IF;
    computed_job_id := 'profile_job_' || encode(
        sha256(convert_to(concat_ws(
            '|',
            'semantic_join_profile_job_v1',
            p_workspace_id,
            p_scan_id,
            request_record.proposal_fingerprint
        ), 'UTF8')),
        'hex'
    );
    INSERT INTO schemabridge_control.semantic_join_profile_jobs (
        job_id, workspace_id, scan_id, connection_id,
        connector_route_revision, connector_route_fingerprint,
        connector_target_fingerprint, proposal_fingerprint, proposal_json,
        status, attempt_count, max_attempts, available_at, fencing_token,
        requested_at, updated_at
    ) VALUES (
        computed_job_id,
        p_workspace_id,
        p_scan_id,
        request_record.connection_id,
        (request_record.payload#>>'{request,execution_target,route_revision}')::bigint,
        request_record.payload#>>'{request,execution_target,route_fingerprint}',
        request_record.execution_target_fingerprint,
        request_record.proposal_fingerprint,
        request_record.payload#>'{request,proposal}',
        'requested', 0, p_max_attempts, request_record.requested_at, 0,
        request_record.requested_at, request_record.requested_at
    ) ON CONFLICT (workspace_id, scan_id, proposal_fingerprint) DO NOTHING;
    RETURN computed_job_id;
END;
$$;

CREATE FUNCTION schemabridge_control.load_registry_model_join_profile_job(
    p_workspace_id varchar,
    p_scan_id varchar,
    p_authoring_fingerprint char(64)
)
RETURNS SETOF schemabridge_control.semantic_join_profile_jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model profile read role is invalid'
            USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    SELECT job.*
    FROM schemabridge_control.registry_model_join_profile_requests AS request
    JOIN schemabridge_control.semantic_join_profile_jobs AS job
      ON job.workspace_id = request.workspace_id
     AND job.scan_id = request.scan_id
     AND job.proposal_fingerprint = request.proposal_fingerprint
    WHERE request.workspace_id = p_workspace_id
      AND request.scan_id = p_scan_id
      AND request.authoring_fingerprint = p_authoring_fingerprint
      AND job.status <> 'leased';
END;
$$;

CREATE FUNCTION schemabridge_control.reject_stale_registry_model_profile_jobs(
    p_limit integer DEFAULT 100
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    candidate record;
    rejection_time timestamptz;
    rejected integer := 0;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_worker', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model profile rejection role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF p_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'registry model profile rejection limit is invalid'
            USING ERRCODE = '22023';
    END IF;
    INSERT INTO schemabridge_control.registry_model_authority_reaper_sessions (
        backend_pid,
        transaction_id,
        opened_at
    ) VALUES (pg_backend_pid(), txid_current(), clock_timestamp());
    FOR candidate IN
        SELECT
            job.workspace_id,
            job.job_id,
            job.status,
            job.attempt_count,
            job.fencing_token,
            job.updated_at,
            request.request_id
        FROM schemabridge_control.semantic_join_profile_jobs AS job
        JOIN schemabridge_control.registry_model_join_profile_requests AS request
          ON request.workspace_id = job.workspace_id
         AND request.scan_id = job.scan_id
         AND request.proposal_fingerprint = job.proposal_fingerprint
        WHERE job.status IN ('requested', 'retry_wait')
          AND job.available_at <= clock_timestamp()
          AND job.attempt_count < job.max_attempts
          AND (
              NOT schemabridge_control.registry_model_authority_current(
                  request.workspace_id,
                  request.payload#>'{request,base}',
                  request.payload#>'{request,replacement_source,proposal}'
              )
              OR NOT schemabridge_control.registry_model_profile_proposal_authorized(
                  request.payload
              )
          )
        ORDER BY job.available_at, job.requested_at, job.job_id
        LIMIT p_limit
        FOR UPDATE OF job SKIP LOCKED
    LOOP
        rejection_time := greatest(
            clock_timestamp(),
            candidate.updated_at + interval '1 microsecond'
        );
        UPDATE schemabridge_control.semantic_join_profile_jobs
        SET status = 'leased',
            attempt_count = candidate.attempt_count + 1,
            lease_owner_id = 'registry-model-authority-reaper',
            lease_capability_digest = repeat('0', 64),
            fencing_token = candidate.fencing_token + 1,
            lease_acquired_at = rejection_time,
            lease_heartbeat_at = rejection_time,
            lease_expires_at = rejection_time + interval '10 seconds',
            failure_code = NULL,
            updated_at = rejection_time
        WHERE workspace_id = candidate.workspace_id
          AND job_id = candidate.job_id
          AND status = candidate.status
          AND attempt_count = candidate.attempt_count
          AND fencing_token = candidate.fencing_token;
        IF FOUND THEN
            rejection_time := rejection_time + interval '1 microsecond';
            UPDATE schemabridge_control.semantic_join_profile_jobs
            SET status = 'failed',
                lease_owner_id = NULL,
                lease_capability_digest = NULL,
                lease_acquired_at = NULL,
                lease_heartbeat_at = NULL,
                lease_expires_at = NULL,
                failure_code = 'evidence_invalid',
                updated_at = rejection_time,
                completed_at = rejection_time,
                retain_until = rejection_time + interval '30 days'
            WHERE workspace_id = candidate.workspace_id
              AND job_id = candidate.job_id
              AND status = 'leased'
              AND fencing_token = candidate.fencing_token + 1;
            INSERT INTO schemabridge_control.registry_model_join_profile_rejections (
                workspace_id, job_id, request_id, rejection_code, rejected_at
            ) VALUES (
                candidate.workspace_id,
                candidate.job_id,
                candidate.request_id,
                'authority_stale',
                rejection_time
            ) ON CONFLICT DO NOTHING;
            rejected := rejected + 1;
        END IF;
    END LOOP;
    DELETE FROM schemabridge_control.registry_model_authority_reaper_sessions
    WHERE backend_pid = pg_backend_pid()
      AND transaction_id = txid_current();
    RETURN rejected;
END;
$$;

CREATE FUNCTION schemabridge_control.registry_model_profile_job_claimable(
    p_workspace_id varchar,
    p_job_id varchar,
    p_scan_id varchar,
    p_proposal_fingerprint char(64)
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_worker', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'registry model profile claim role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM schemabridge_control.semantic_profile_sources AS source
        WHERE source.workspace_id = p_workspace_id
          AND source.scan_id = p_scan_id
          AND source.source_kind = 'registry_model_join_profile_v1'
    ) THEN
        RETURN true;
    END IF;
    RETURN EXISTS (
        SELECT 1
        FROM schemabridge_control.registry_model_join_profile_requests AS request
        JOIN schemabridge_control.registry_model_join_profile_audit AS audit
          ON audit.workspace_id = request.workspace_id
         AND audit.request_id = request.request_id
         AND audit.event = 'job_bound'
         AND audit.job_id = p_job_id
        JOIN schemabridge_control.registry_model_join_profile_operations AS operation
          ON operation.workspace_id = request.workspace_id
         AND operation.request_id = request.request_id
         AND operation.operation = 'request_model_join_profile'
         AND operation.response_authoring = request.payload
         AND operation.response_job_id = p_job_id
         AND operation.response_job->>'job_id' = p_job_id
         AND operation.actor_id = audit.actor_id
        WHERE request.workspace_id = p_workspace_id
          AND request.scan_id = p_scan_id
          AND request.proposal_fingerprint = p_proposal_fingerprint
          AND schemabridge_control.registry_model_authority_current(
              request.workspace_id,
              request.payload#>'{request,base}',
              request.payload#>'{request,replacement_source,proposal}'
          )
          AND schemabridge_control.registry_model_profile_proposal_authorized(
              request.payload
          )
    );
END;
$$;

CREATE FUNCTION schemabridge_control.load_semantic_dependency_index_state(
    p_workspace_id varchar,
    p_catalog_scope varchar,
    p_registry_id varchar
)
RETURNS TABLE (watermark bigint, index_fingerprint char(64), complete boolean)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF SESSION_USER NOT IN (
        'schemabridge_api',
        'schemabridge_publisher',
        'schemabridge_reconciler',
        'schemabridge_migrator'
    ) THEN
        RAISE EXCEPTION 'semantic dependency index read role is invalid'
            USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    SELECT state.watermark, state.index_fingerprint, state.complete
    FROM schemabridge_control.semantic_dependency_index_states AS state
    WHERE state.workspace_id = p_workspace_id
      AND state.catalog_scope = p_catalog_scope
      AND state.registry_id = p_registry_id;
END;
$$;

CREATE FUNCTION schemabridge_control.load_registry_model_replacement_source(
    p_workspace_id varchar,
    p_proposal_id varchar
)
RETURNS TABLE (proposal jsonb, owner_actor_id varchar, decisions jsonb)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT
    prepared.payload,
    draft.owner_actor_id,
    jsonb_agg(decision.payload ORDER BY decision.decision_id)
FROM schemabridge_control.semantic_onboarding_proposals AS prepared
JOIN schemabridge_control.semantic_onboarding_drafts AS draft
  ON draft.workspace_id = prepared.workspace_id
 AND draft.draft_id = prepared.draft_id
 AND draft.status = 'ready_for_publication'
 AND draft.prepared_proposal_id = prepared.proposal_id
 AND draft.prepared_proposal_fingerprint = prepared.fingerprint
JOIN schemabridge_control.semantic_onboarding_decisions AS decision
  ON decision.workspace_id = draft.workspace_id
 AND decision.draft_id = draft.draft_id
WHERE prepared.workspace_id = p_workspace_id
  AND prepared.proposal_id = p_proposal_id
GROUP BY prepared.payload, draft.owner_actor_id
HAVING count(*) = jsonb_array_length(prepared.payload->'decision_ids')
   AND jsonb_agg(to_jsonb(decision.decision_id) ORDER BY decision.decision_id)
        = prepared.payload->'decision_ids'
$$;

CREATE FUNCTION schemabridge_control.load_current_registry_model_remediation(
    p_workspace_id varchar,
    p_catalog_scope varchar,
    p_registry_id varchar,
    p_report_id varchar,
    p_context_fingerprint char(64)
)
RETURNS TABLE (report jsonb, impacts jsonb)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT
    current_report.report_json,
    coalesce(
        jsonb_agg(
            impact.impact_json
            ORDER BY
                impact.artifact_kind,
                impact.artifact_id,
                coalesce(impact.artifact_version, 0)
        ) FILTER (WHERE impact.impact_id IS NOT NULL),
        '[]'::jsonb
    )
FROM schemabridge_control.semantic_change_heads AS head
JOIN schemabridge_control.semantic_change_reports AS current_report
  ON current_report.workspace_id = head.workspace_id
 AND current_report.catalog_scope = head.catalog_scope
 AND current_report.registry_id = head.registry_id
 AND current_report.report_id = head.current_report_id
 AND current_report.report_fingerprint = head.current_report_fingerprint
LEFT JOIN schemabridge_control.semantic_change_impacts AS impact
  ON impact.workspace_id = current_report.workspace_id
 AND impact.report_id = current_report.report_id
WHERE head.workspace_id = p_workspace_id
  AND head.catalog_scope = p_catalog_scope
  AND head.registry_id = p_registry_id
  AND head.state = 'blocked'
  AND head.current_report_id = p_report_id
  AND current_report.context_fingerprint = p_context_fingerprint
  AND head.registry_generation = current_report.registry_generation
  AND head.registry_version = current_report.registry_version
  AND head.registry_fingerprint = current_report.registry_fingerprint
  AND head.pointer_transition_id = current_report.pointer_transition_id
  AND head.dependency_index_watermark = current_report.dependency_index_watermark
  AND head.dependency_index_fingerprint = current_report.dependency_index_fingerprint
  AND head.dependency_index_complete = current_report.dependency_index_complete
GROUP BY current_report.report_json, current_report.impact_count
HAVING count(impact.impact_id) = current_report.impact_count
$$;

CREATE FUNCTION schemabridge_control.load_current_registry_model_join_witness(
    p_workspace_id varchar,
    p_change_id varchar,
    p_source_proposal_id varchar,
    p_incident_join_id varchar
)
RETURNS TABLE (witness jsonb, request_id varchar, requested_at timestamptz)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT witness.payload, request.request_id, request.requested_at
FROM schemabridge_control.registry_model_join_profile_witnesses AS witness
JOIN schemabridge_control.registry_model_join_profile_requests AS request
  ON request.workspace_id = witness.workspace_id
 AND request.request_id = witness.request_id
WHERE witness.workspace_id = p_workspace_id
  AND witness.change_id = p_change_id
  AND witness.source_proposal_id = p_source_proposal_id
  AND witness.incident_join_id = p_incident_join_id
ORDER BY request.requested_at DESC, request.request_id DESC
LIMIT 1
$$;

REVOKE ALL ON
    schemabridge_control.registry_model_join_profile_requests,
    schemabridge_control.registry_model_join_profile_audit,
    schemabridge_control.registry_model_join_profile_witnesses,
    schemabridge_control.registry_model_join_profile_operations,
    schemabridge_control.registry_model_join_profile_rejections,
    schemabridge_control.registry_model_authority_reaper_sessions,
    schemabridge_control.semantic_registry_model_change_drafts,
    schemabridge_control.semantic_registry_model_change_decisions,
    schemabridge_control.semantic_registry_model_change_proposals,
    schemabridge_control.semantic_registry_model_change_audit,
    schemabridge_control.semantic_registry_model_change_operations
    FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION
    schemabridge_control.enforce_registry_publication_job_lifecycle(),
    schemabridge_control.enforce_registry_publication_job_transition(),
    schemabridge_control.guard_registry_model_history(),
    schemabridge_control.guard_registry_model_draft(),
    schemabridge_control.guard_registry_model_profile_request(),
    schemabridge_control.guard_registry_model_profile_operation(),
    schemabridge_control.guard_registry_model_profile_witness(),
    schemabridge_control.guard_registry_model_profile_rejection(),
    schemabridge_control.guard_registry_model_operation(),
    schemabridge_control.guard_registry_model_decision(),
    schemabridge_control.guard_registry_model_proposal(),
    schemabridge_control.registry_model_authority_current(varchar, jsonb, jsonb),
    schemabridge_control.registry_model_profile_proposal_authorized(jsonb),
    schemabridge_control.enqueue_registry_model_join_profile(varchar, varchar, char, integer),
    schemabridge_control.load_registry_model_join_profile_job(varchar, varchar, char),
    schemabridge_control.reject_stale_registry_model_profile_jobs(integer),
    schemabridge_control.registry_model_profile_job_claimable(varchar, varchar, varchar, char),
    schemabridge_control.load_semantic_dependency_index_state(varchar, varchar, varchar),
    schemabridge_control.load_registry_model_replacement_source(varchar, varchar),
    schemabridge_control.load_current_registry_model_remediation(
        varchar, varchar, varchar, varchar, char
    ),
    schemabridge_control.load_current_registry_model_join_witness(
        varchar, varchar, varchar, varchar
    )
    FROM PUBLIC;

GRANT SELECT ON
    schemabridge_control.registry_model_join_profile_requests,
    schemabridge_control.registry_model_join_profile_audit,
    schemabridge_control.registry_model_join_profile_witnesses,
    schemabridge_control.registry_model_join_profile_operations,
    schemabridge_control.registry_model_join_profile_rejections,
    schemabridge_control.semantic_registry_model_change_drafts,
    schemabridge_control.semantic_registry_model_change_decisions,
    schemabridge_control.semantic_registry_model_change_proposals,
    schemabridge_control.semantic_registry_model_change_audit,
    schemabridge_control.semantic_registry_model_change_operations
    TO schemabridge_api, schemabridge_publisher;

GRANT INSERT ON
    schemabridge_control.registry_model_join_profile_requests,
    schemabridge_control.registry_model_join_profile_audit,
    schemabridge_control.registry_model_join_profile_witnesses,
    schemabridge_control.registry_model_join_profile_operations,
    schemabridge_control.semantic_registry_model_change_decisions,
    schemabridge_control.semantic_registry_model_change_proposals,
    schemabridge_control.semantic_registry_model_change_audit,
    schemabridge_control.semantic_registry_model_change_operations
    TO schemabridge_api;

GRANT INSERT, UPDATE ON schemabridge_control.semantic_registry_model_change_drafts
    TO schemabridge_api;

GRANT UPDATE (response_job_id, response_job)
    ON schemabridge_control.registry_model_join_profile_operations
    TO schemabridge_api;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.enqueue_registry_model_join_profile(varchar, varchar, char, integer),
    schemabridge_control.load_registry_model_join_profile_job(varchar, varchar, char)
    TO schemabridge_api;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.reject_stale_registry_model_profile_jobs(integer),
    schemabridge_control.registry_model_profile_job_claimable(varchar, varchar, varchar, char)
    TO schemabridge_worker;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_semantic_dependency_index_state(varchar, varchar, varchar)
    TO schemabridge_api, schemabridge_publisher, schemabridge_reconciler;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_registry_model_replacement_source(varchar, varchar),
    schemabridge_control.load_current_registry_model_remediation(
        varchar, varchar, varchar, varchar, char
    ),
    schemabridge_control.load_current_registry_model_join_witness(
        varchar, varchar, varchar, varchar
    )
    TO schemabridge_api, schemabridge_publisher;

CREATE FUNCTION schemabridge_control.registry_join_publication_witness_valid(
    p_payload jsonb,
    p_proposal_id varchar,
    p_proposal_fingerprint char(64)
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
SELECT (
    p_payload#>>'{proposal,proposal_kind}' = 'add_join_v1'
    AND p_payload#>>'{proposal,id}' = p_proposal_id
    AND p_payload#>>'{proposal,fingerprint}' = p_proposal_fingerprint::text
    AND p_payload#>>'{proposal,decision,action}' = 'approve'
    AND p_payload#>>'{proposal,decision,status}' = 'approved'
    AND p_payload#>>'{proposal,decision,target_type}' = 'join_contract'
    AND p_payload#>>'{proposal,decision,id}'
        = p_payload#>>'{proposal,contract,approval_decision_id}'
    AND p_payload#>>'{proposal,decision,target_id}'
        = p_payload#>>'{proposal,contract,id}'
    AND jsonb_typeof(p_payload#>'{proposal,decision_ids}') = 'array'
    AND jsonb_array_length(p_payload#>'{proposal,decision_ids}') = 1
    AND p_payload#>>'{proposal,decision_ids,0}'
        = p_payload#>>'{proposal,decision,id}'
    AND (
        SELECT count(*) = 1
        FROM jsonb_array_elements(
            p_payload#>'{candidate,registry,join_contracts,contracts}'
        ) AS contract
        WHERE contract = p_payload#>'{proposal,contract}'
    )
    AND (
        SELECT count(*) = 1
        FROM jsonb_array_elements(
            p_payload#>'{candidate,registry,logical_context,joins}'
        ) AS logical_join
        WHERE logical_join->>'id' = p_payload#>>'{proposal,contract,id}'
          AND logical_join->>'approval_decision_id'
                = p_payload#>>'{proposal,decision,id}'
          AND logical_join->>'cardinality'
                = p_payload#>>'{proposal,contract,cardinality}'
          AND logical_join->>'fanout_policy'
                = p_payload#>>'{proposal,contract,fanout_policy}'
          AND logical_join->>'version'
                = p_payload#>>'{proposal,contract,version}'
    )
    AND (
        SELECT count(*) = 1
        FROM jsonb_array_elements(
            p_payload#>'{candidate,registry,provenance}'
        ) AS provenance
        WHERE provenance->>'kind' = 'join_contracts'
          AND jsonb_typeof(provenance->'decision_ids') = 'array'
          AND provenance->'decision_ids'
                @> jsonb_build_array(p_payload#>>'{proposal,decision,id}')
    )
)
$$;

CREATE FUNCTION schemabridge_control.registry_model_publication_witness_valid(
    p_payload jsonb,
    p_proposal_id varchar,
    p_proposal_fingerprint char(64)
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
SELECT (
    p_payload#>>'{proposal,proposal_kind}' = 'replace_model_v1'
    AND p_payload#>>'{proposal,id}' = p_proposal_id
    AND p_payload#>>'{proposal,fingerprint}' = p_proposal_fingerprint::text
    AND p_payload#>>'{proposal,source_replacement_proposal_id}'
        = p_payload#>>'{proposal,replacement,id}'
    AND p_payload#>>'{proposal,source_replacement_proposal_fingerprint}'
        = p_payload#>>'{proposal,replacement,fingerprint}'
    AND p_payload#>>'{proposal,outer_decision,action}' = 'approve'
    AND p_payload#>>'{proposal,outer_decision,status}' = 'approved'
    AND p_payload#>>'{proposal,outer_decision,target_type}' = 'logical_model'
    AND p_payload#>>'{proposal,outer_decision,target_id}'
        = p_payload#>>'{proposal,replacement,model,definition,id}'
    AND p_payload#>>'{proposal,model_decision,action}' = 'approve'
    AND p_payload#>>'{proposal,model_decision,status}' = 'approved'
    AND p_payload#>>'{proposal,model_decision,target_id}'
        = p_payload#>>'{proposal,replacement,model,definition,id}'
    AND jsonb_typeof(p_payload#>'{proposal,decision_ids}') = 'array'
    AND jsonb_typeof(p_payload#>'{candidate,review_decision_ids}') = 'array'
    AND (p_payload#>'{candidate,review_decision_ids}')
        @> (p_payload#>'{proposal,decision_ids}')
    AND p_payload#>>'{candidate,registry,version}'
        = p_payload#>>'{proposal,replacement,target_registry_version}'
    AND jsonb_typeof(p_payload#>'{candidate,registry,logical_context,models}') = 'array'
    AND jsonb_typeof(p_payload#>'{candidate,registry,mapping_set,mappings}') = 'array'
    AND jsonb_typeof(p_payload#>'{candidate,registry,physical_bindings}') = 'array'
    AND jsonb_typeof(p_payload#>'{proposal,replacement,model,definition,fields}') = 'array'
    AND jsonb_typeof(p_payload#>'{proposal,replacement,mappings}') = 'array'
    AND (
        SELECT count(*) = 1
        FROM jsonb_array_elements(
            p_payload#>'{candidate,registry,logical_context,models}'
        ) AS candidate_model(value)
        WHERE candidate_model.value->>'id'
                = p_payload#>>'{proposal,replacement,model,definition,id}'
          AND candidate_model.value->>'description'
                = p_payload#>>'{proposal,replacement,model,definition,description}'
          AND candidate_model.value->>'status' = 'approved'
          AND candidate_model.value->>'version'
                = p_payload#>>'{proposal,replacement,model,definition,version}'
          AND jsonb_typeof(candidate_model.value->'fields') = 'array'
          AND jsonb_array_length(candidate_model.value->'fields')
                = jsonb_array_length(
                    p_payload#>'{proposal,replacement,model,definition,fields}'
                )
          AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements(
                  p_payload#>'{proposal,replacement,model,definition,fields}'
              ) AS replacement_field(value)
              WHERE NOT EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(candidate_model.value->'fields')
                        AS candidate_field(value)
                  WHERE candidate_field.value->>'id' = replacement_field.value->>'id'
                    AND candidate_field.value->>'canonical_type'
                            = replacement_field.value->>'canonical_type'
                    AND candidate_field.value->>'role' = replacement_field.value->>'role'
                    AND candidate_field.value->>'definition'
                            = replacement_field.value->>'definition'
                    AND candidate_field.value->'allowed_values'
                            = replacement_field.value->'allowed_values'
                    AND candidate_field.value->>'status' = 'approved'
              )
          )
    )
    AND (
        SELECT count(*)
        FROM jsonb_array_elements(
            p_payload#>'{candidate,registry,mapping_set,mappings}'
        ) AS candidate_mapping(value)
        WHERE split_part(
            candidate_mapping.value#>>'{mapping,logical_field}',
            '.',
            1
        ) = p_payload#>>'{proposal,replacement,model,definition,id}'
    ) = jsonb_array_length(p_payload#>'{proposal,replacement,mappings}')
    AND NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_payload#>'{proposal,replacement,mappings}')
            AS replacement_mapping(value)
        WHERE NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                p_payload#>'{candidate,registry,mapping_set,mappings}'
            ) AS candidate_mapping(value)
            WHERE candidate_mapping.value#>>'{mapping,logical_field}'
                    = replacement_mapping.value->>'logical_field'
              AND candidate_mapping.value#>>'{mapping,physical_field}'
                    = replacement_mapping.value#>>'{observation,physical_field}'
              AND candidate_mapping.value#>'{mapping,transformation_plan}'
                    = replacement_mapping.value->'transformation_plan'
              AND candidate_mapping.value->>'physical_type'
                    = replacement_mapping.value#>>'{observation,physical_type}'
              AND candidate_mapping.value->>'approval_decision_id'
                    = replacement_mapping.value->>'decision_id'
        )
    )
    AND (
        SELECT count(*)
        FROM jsonb_array_elements(
            p_payload#>'{candidate,registry,physical_bindings}'
        ) AS candidate_binding(value)
        WHERE split_part(candidate_binding.value->>'logical_field', '.', 1)
                = p_payload#>>'{proposal,replacement,model,definition,id}'
    ) = jsonb_array_length(p_payload#>'{proposal,replacement,mappings}')
    AND NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_payload#>'{proposal,replacement,mappings}')
            AS replacement_mapping(value)
        WHERE NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                p_payload#>'{candidate,registry,physical_bindings}'
            ) AS candidate_binding(value)
            WHERE candidate_binding.value->>'logical_field'
                    = replacement_mapping.value->>'logical_field'
              AND candidate_binding.value->>'physical_field'
                    = replacement_mapping.value#>>'{observation,physical_field}'
              AND candidate_binding.value->>'physical_type'
                    = replacement_mapping.value#>>'{observation,physical_type}'
              AND candidate_binding.value->>'connection_id'
                    = replacement_mapping.value#>>'{observation,locator,asset,connection_id}'
              AND candidate_binding.value->>'catalog_generation'
                    = replacement_mapping.value#>>'{observation,generation}'
              AND candidate_binding.value->>'catalog_generation_fingerprint'
                    = replacement_mapping.value#>>'{observation,generation_fingerprint}'
              AND candidate_binding.value->'locator'
                    = replacement_mapping.value#>'{observation,locator}'
              AND candidate_binding.value->>'asset_metadata_fingerprint'
                    = replacement_mapping.value#>>'{observation,asset_metadata_fingerprint}'
              AND candidate_binding.value->>'field_metadata_fingerprint'
                    = replacement_mapping.value#>>'{observation,field_metadata_fingerprint}'
              AND candidate_binding.value->>'observed_datahub_asset_urn'
                    = replacement_mapping.value#>>'{observation,observed_datahub_asset_urn}'
              AND candidate_binding.value->>'source_proposal_id'
                    = p_payload#>>'{proposal,replacement,id}'
              AND candidate_binding.value->>'source_proposal_fingerprint'
                    = p_payload#>>'{proposal,replacement,fingerprint}'
        )
    )
    AND jsonb_typeof(p_payload#>'{proposal,incident_join_changes}') = 'array'
    AND NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_payload#>'{proposal,incident_join_changes}')
            AS incident(value)
        WHERE CASE incident.value->>'action'
            WHEN 'preserve_exact' THEN NOT (
                SELECT count(*) = 1
                FROM jsonb_array_elements(
                    p_payload#>'{candidate,registry,join_contracts,contracts}'
                ) AS candidate_contract(value)
                WHERE candidate_contract.value = incident.value#>'{base,contract}'
            )
            WHEN 'remove_explicit' THEN (
                EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                        p_payload#>'{candidate,registry,join_contracts,contracts}'
                    ) AS candidate_contract(value)
                    WHERE candidate_contract.value->>'id'
                            = incident.value#>>'{base,contract,id}'
                )
                OR NOT (
                    (p_payload#>'{candidate,review_decision_ids}')
                    @> jsonb_build_array(incident.value#>>'{decision,id}')
                )
            )
            WHEN 'upsert_fresh' THEN (
                NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                        p_payload#>'{candidate,registry,join_contracts,contracts}'
                    ) AS candidate_contract(value)
                    WHERE candidate_contract.value = incident.value->'contract'
                )
                OR NOT (
                    (p_payload#>'{candidate,review_decision_ids}')
                    @> jsonb_build_array(incident.value#>>'{decision,id}')
                )
            )
            ELSE true
        END
    )
)
$$;

CREATE OR REPLACE FUNCTION schemabridge_control.load_registry_activation_ready_handoff(
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
        jobs.proposal_kind,
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
        OR NOT (
            (
                publication.proposal_kind = 'onboarding_additive_v1'
                AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(publication.bindings) AS binding
                    WHERE binding->>'source_proposal_id' = publication.proposal_id
                      AND binding->>'source_proposal_fingerprint'
                            = publication.proposal_fingerprint::text
                )
            )
            OR (
                publication.proposal_kind = 'add_join_v1'
                AND schemabridge_control.registry_join_publication_witness_valid(
                    publication.payload,
                    publication.proposal_id,
                    publication.proposal_fingerprint
                )
            )
            OR (
                publication.proposal_kind = 'replace_model_v1'
                AND schemabridge_control.registry_model_publication_witness_valid(
                    publication.payload,
                    publication.proposal_id,
                    publication.proposal_fingerprint
                )
            )
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

REVOKE EXECUTE ON FUNCTION
    schemabridge_control.registry_join_publication_witness_valid(jsonb, varchar, char),
    schemabridge_control.registry_model_publication_witness_valid(jsonb, varchar, char)
    FROM PUBLIC;
