CREATE TABLE schemabridge_control.semantic_onboarding_drafts (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 3 AND 200),
    draft_id varchar(80) NOT NULL
        CHECK (draft_id ~ '^[a-z][a-z0-9_-]{2,79}$'),
    owner_actor_id varchar(200) NOT NULL
        CHECK (length(trim(owner_actor_id)) BETWEEN 1 AND 200),
    revision bigint NOT NULL CHECK (revision >= 1),
    status varchar(32) NOT NULL
        CHECK (status IN ('needs_review', 'ready_for_publication', 'superseded')),
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 2097152
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = draft_id
            AND payload->>'owner_actor_id' = owner_actor_id
            AND (payload->>'revision')::bigint = revision
            AND payload->>'status' = status
        ),
    prepared_proposal_id varchar(200),
    prepared_proposal_fingerprint char(64)
        CHECK (
            prepared_proposal_fingerprint IS NULL
            OR prepared_proposal_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
    PRIMARY KEY (workspace_id, draft_id),
    CHECK (
        (status IN ('ready_for_publication', 'superseded'))
        = (prepared_proposal_id IS NOT NULL AND prepared_proposal_fingerprint IS NOT NULL)
    )
);

CREATE INDEX semantic_onboarding_drafts_workspace_updated_idx
    ON schemabridge_control.semantic_onboarding_drafts (
        workspace_id,
        updated_at DESC,
        draft_id DESC
    );

CREATE INDEX semantic_onboarding_drafts_owner_updated_idx
    ON schemabridge_control.semantic_onboarding_drafts (
        workspace_id,
        owner_actor_id,
        updated_at DESC,
        draft_id DESC
    );

CREATE TABLE schemabridge_control.semantic_onboarding_decisions (
    workspace_id varchar(200) NOT NULL,
    decision_id varchar(200) NOT NULL,
    draft_id varchar(80) NOT NULL,
    resulting_revision bigint NOT NULL CHECK (resulting_revision >= 2),
    actor_id varchar(200) NOT NULL
        CHECK (length(trim(actor_id)) BETWEEN 1 AND 200),
    target_kind varchar(16) NOT NULL CHECK (target_kind IN ('model', 'mapping')),
    target_id varchar(200) NOT NULL
        CHECK (length(trim(target_id)) BETWEEN 3 AND 200),
    status varchar(16) NOT NULL CHECK (status IN ('approved', 'rejected')),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 65536
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = decision_id
            AND payload->>'draft_id' = draft_id
            AND (payload->>'resulting_revision')::bigint = resulting_revision
            AND payload->>'actor_id' = actor_id
            AND payload->>'target_kind' = target_kind
            AND payload->>'target_id' = target_id
            AND payload->>'status' = status
        ),
    decided_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, decision_id),
    UNIQUE (workspace_id, draft_id, resulting_revision),
    UNIQUE (workspace_id, draft_id, target_kind, target_id),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.semantic_onboarding_drafts (
            workspace_id,
            draft_id
        )
);

CREATE INDEX semantic_onboarding_decisions_draft_idx
    ON schemabridge_control.semantic_onboarding_decisions (
        workspace_id,
        draft_id,
        resulting_revision,
        decision_id
    );

CREATE TABLE schemabridge_control.semantic_onboarding_proposals (
    workspace_id varchar(200) NOT NULL,
    proposal_id varchar(200) NOT NULL,
    draft_id varchar(80) NOT NULL,
    draft_revision bigint NOT NULL CHECK (draft_revision >= 1),
    target_registry_version bigint NOT NULL CHECK (target_registry_version >= 1),
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    prepared_by varchar(200) NOT NULL
        CHECK (length(trim(prepared_by)) BETWEEN 1 AND 200),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 2097152
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = proposal_id
            AND payload->>'draft_id' = draft_id
            AND (payload->>'draft_revision')::bigint = draft_revision
            AND (payload->>'target_registry_version')::bigint = target_registry_version
            AND payload->>'fingerprint' = fingerprint
            AND payload->>'prepared_by' = prepared_by
            AND payload->>'external_writes_performed' = 'false'
        ),
    prepared_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, proposal_id),
    UNIQUE (workspace_id, draft_id, target_registry_version),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.semantic_onboarding_drafts (
            workspace_id,
            draft_id
        )
);

CREATE INDEX semantic_onboarding_proposals_draft_idx
    ON schemabridge_control.semantic_onboarding_proposals (
        workspace_id,
        draft_id,
        target_registry_version,
        proposal_id
    );

CREATE TABLE schemabridge_control.semantic_onboarding_audit (
    workspace_id varchar(200) NOT NULL,
    audit_id varchar(200) NOT NULL,
    draft_id varchar(80) NOT NULL,
    resulting_revision bigint NOT NULL CHECK (resulting_revision >= 1),
    event varchar(32) NOT NULL
        CHECK (event IN ('draft_created', 'decision_recorded', 'publication_prepared')),
    actor_id varchar(200) NOT NULL
        CHECK (length(trim(actor_id)) BETWEEN 1 AND 200),
    payload jsonb NOT NULL
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 65536
            AND payload->>'workspace_id' = workspace_id
            AND payload->>'id' = audit_id
            AND payload->>'draft_id' = draft_id
            AND (payload->>'resulting_revision')::bigint = resulting_revision
            AND payload->>'event' = event
            AND payload->>'actor_id' = actor_id
            AND payload->>'external_writes_performed' = 'false'
        ),
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, audit_id),
    UNIQUE (workspace_id, draft_id, resulting_revision),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.semantic_onboarding_drafts (
            workspace_id,
            draft_id
        )
);

CREATE INDEX semantic_onboarding_audit_draft_idx
    ON schemabridge_control.semantic_onboarding_audit (
        workspace_id,
        draft_id,
        resulting_revision,
        audit_id
    );

CREATE TABLE schemabridge_control.semantic_onboarding_operations (
    workspace_id varchar(200) NOT NULL,
    idempotency_digest char(64) NOT NULL
        CHECK (idempotency_digest ~ '^[0-9a-f]{64}$'),
    operation varchar(64) NOT NULL
        CHECK (operation IN ('create_draft', 'record_decision', 'prepare_publication')),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    actor_id varchar(200) NOT NULL
        CHECK (length(trim(actor_id)) BETWEEN 1 AND 200),
    draft_id varchar(80) NOT NULL,
    response_revision bigint NOT NULL CHECK (response_revision >= 1),
    response_fingerprint char(64) NOT NULL
        CHECK (response_fingerprint ~ '^[0-9a-f]{64}$'),
    response_draft jsonb
        CHECK (
            response_draft IS NULL
            OR (
                jsonb_typeof(response_draft) = 'object'
                AND octet_length(response_draft::text) <= 2097152
                AND response_draft->>'workspace_id' = workspace_id
                AND response_draft->>'id' = draft_id
                AND (response_draft->>'revision')::bigint = response_revision
            )
        ),
    response_proposal jsonb
        CHECK (
            response_proposal IS NULL
            OR (
                jsonb_typeof(response_proposal) = 'object'
                AND octet_length(response_proposal::text) <= 2097152
                AND response_proposal->>'workspace_id' = workspace_id
                AND response_proposal->>'draft_id' = draft_id
                AND response_proposal->>'external_writes_performed' = 'false'
            )
        ),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, idempotency_digest),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.semantic_onboarding_drafts (
            workspace_id,
            draft_id
        ),
    CHECK ((operation = 'record_decision') = (response_draft IS NULL)),
    CHECK ((operation = 'create_draft') = (response_revision = 1)),
    CHECK ((operation = 'prepare_publication') = (response_proposal IS NOT NULL))
);

CREATE INDEX semantic_onboarding_operations_draft_idx
    ON schemabridge_control.semantic_onboarding_operations (
        workspace_id,
        draft_id,
        created_at,
        idempotency_digest
    );

CREATE UNIQUE INDEX semantic_onboarding_operations_create_draft_idx
    ON schemabridge_control.semantic_onboarding_operations (workspace_id, draft_id)
    WHERE operation = 'create_draft';

CREATE FUNCTION schemabridge_control.reject_semantic_onboarding_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'semantic onboarding record is immutable'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER semantic_onboarding_decisions_immutable
BEFORE UPDATE OR DELETE ON schemabridge_control.semantic_onboarding_decisions
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_onboarding_immutable_mutation();

CREATE TRIGGER semantic_onboarding_proposals_immutable
BEFORE UPDATE OR DELETE ON schemabridge_control.semantic_onboarding_proposals
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_onboarding_immutable_mutation();

CREATE TRIGGER semantic_onboarding_audit_immutable
BEFORE UPDATE OR DELETE ON schemabridge_control.semantic_onboarding_audit
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_onboarding_immutable_mutation();

CREATE TRIGGER semantic_onboarding_operations_immutable
BEFORE UPDATE OR DELETE ON schemabridge_control.semantic_onboarding_operations
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_onboarding_immutable_mutation();

REVOKE ALL ON
    schemabridge_control.semantic_onboarding_drafts,
    schemabridge_control.semantic_onboarding_decisions,
    schemabridge_control.semantic_onboarding_proposals,
    schemabridge_control.semantic_onboarding_audit,
    schemabridge_control.semantic_onboarding_operations
    FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION
    schemabridge_control.reject_semantic_onboarding_immutable_mutation()
    FROM PUBLIC;

GRANT SELECT, INSERT ON
    schemabridge_control.semantic_onboarding_drafts,
    schemabridge_control.semantic_onboarding_decisions,
    schemabridge_control.semantic_onboarding_proposals,
    schemabridge_control.semantic_onboarding_audit,
    schemabridge_control.semantic_onboarding_operations
    TO schemabridge_api;

-- The API may inspect only the authoritative active pointer needed to bind a
-- draft to its exact base. Immutable registry payloads and publication state
-- remain outside this capability.
GRANT SELECT ON schemabridge_control.registry_active_pointers
    TO schemabridge_api;

GRANT UPDATE (
    revision,
    status,
    fingerprint,
    payload,
    prepared_proposal_id,
    prepared_proposal_fingerprint,
    updated_at
) ON schemabridge_control.semantic_onboarding_drafts
    TO schemabridge_api;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.reject_semantic_onboarding_immutable_mutation()
    TO schemabridge_migrator;
