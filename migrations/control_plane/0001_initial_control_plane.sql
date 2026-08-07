CREATE SCHEMA schemabridge_control
    AUTHORIZATION schemabridge_migrator;

REVOKE ALL ON SCHEMA schemabridge_control FROM PUBLIC;
GRANT ALL ON SCHEMA schemabridge_control TO schemabridge_migrator;
GRANT USAGE ON SCHEMA schemabridge_control
    TO schemabridge_runtime, schemabridge_reconciler;

CREATE TABLE schemabridge_control.schema_migrations (
    version integer PRIMARY KEY CHECK (version > 0),
    name varchar(120) NOT NULL
        CHECK (name ~ '^[a-z][a-z0-9_]{0,119}$'),
    checksum char(64) NOT NULL
        CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL,
    applied_by varchar(120) NOT NULL
        CHECK (length(trim(applied_by)) BETWEEN 1 AND 120)
);

CREATE TABLE schemabridge_control.registry_activation_transitions (
    transition_id varchar(200) PRIMARY KEY
        CHECK (transition_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    catalog_scope varchar(120) NOT NULL
        CHECK (length(trim(catalog_scope)) BETWEEN 1 AND 120),
    registry_id varchar(80) NOT NULL
        CHECK (length(trim(registry_id)) BETWEEN 1 AND 80),
    generation bigint NOT NULL CHECK (generation >= 1),
    action varchar(16) NOT NULL CHECK (action IN ('activate', 'rollback')),
    expected_generation bigint NOT NULL CHECK (expected_generation >= 0),
    expected_registry_version bigint CHECK (expected_registry_version >= 1),
    expected_registry_fingerprint char(64)
        CHECK (
            expected_registry_fingerprint IS NULL
            OR expected_registry_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    expected_transition_id varchar(200)
        CHECK (
            expected_transition_id IS NULL
            OR expected_transition_id ~ '^[a-z][a-z0-9_-]{2,199}$'
        ),
    target_registry_version bigint NOT NULL CHECK (target_registry_version >= 1),
    target_registry_fingerprint char(64) NOT NULL
        CHECK (target_registry_fingerprint ~ '^[0-9a-f]{64}$'),
    target_registry_urn varchar(500) NOT NULL
        CHECK (length(trim(target_registry_urn)) BETWEEN 1 AND 500),
    target_publication_approval_id varchar(200) NOT NULL
        CHECK (length(trim(target_publication_approval_id)) BETWEEN 1 AND 200),
    rollback_transition_id varchar(200),
    proposal_fingerprint char(64) NOT NULL
        CHECK (proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    approval_id varchar(200) NOT NULL UNIQUE
        CHECK (approval_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    actor varchar(120) NOT NULL
        CHECK (length(trim(actor)) BETWEEN 1 AND 120),
    approved_at timestamptz NOT NULL,
    decision_ids_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(decision_ids_json) = 'array'
            AND jsonb_array_length(decision_ids_json) BETWEEN 1 AND 2000
        ),
    committed_at timestamptz NOT NULL CHECK (committed_at >= approved_at),
    payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    CONSTRAINT registry_transition_generation_unique
        UNIQUE (workspace_id, catalog_scope, registry_id, generation),
    CONSTRAINT registry_transition_scope_identity_unique
        UNIQUE (
            workspace_id,
            catalog_scope,
            registry_id,
            generation,
            transition_id
        ),
    CONSTRAINT registry_transition_workspace_identity_unique
        UNIQUE (workspace_id, transition_id),
    CONSTRAINT registry_transition_expected_pointer_shape
        CHECK (
            (
                expected_generation = 0
                AND expected_registry_version IS NULL
                AND expected_registry_fingerprint IS NULL
                AND expected_transition_id IS NULL
                AND generation = 1
            )
            OR
            (
                expected_generation >= 1
                AND expected_registry_version IS NOT NULL
                AND expected_registry_fingerprint IS NOT NULL
                AND expected_transition_id IS NOT NULL
                AND generation = expected_generation + 1
            )
        ),
    CONSTRAINT registry_transition_rollback_shape
        CHECK (
            (action = 'activate' AND rollback_transition_id IS NULL)
            OR
            (
                action = 'rollback'
                AND rollback_transition_id IS NOT NULL
                AND rollback_transition_id ~ '^[a-z][a-z0-9_-]{2,199}$'
            )
        ),
    CONSTRAINT registry_transition_expected_pointer_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            expected_generation,
            expected_transition_id
        )
        REFERENCES schemabridge_control.registry_activation_transitions (
            workspace_id,
            catalog_scope,
            registry_id,
            generation,
            transition_id
        ),
    CONSTRAINT registry_transition_rollback_fk
        FOREIGN KEY (
            workspace_id,
            rollback_transition_id
        )
        REFERENCES schemabridge_control.registry_activation_transitions (
            workspace_id,
            transition_id
        )
);

CREATE INDEX registry_activation_transitions_scope_idx
    ON schemabridge_control.registry_activation_transitions (
        workspace_id,
        catalog_scope,
        registry_id,
        generation DESC
    );

CREATE TABLE schemabridge_control.registry_active_pointers (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    catalog_scope varchar(120) NOT NULL
        CHECK (length(trim(catalog_scope)) BETWEEN 1 AND 120),
    registry_id varchar(80) NOT NULL
        CHECK (length(trim(registry_id)) BETWEEN 1 AND 80),
    generation bigint NOT NULL CHECK (generation >= 1),
    registry_version bigint NOT NULL CHECK (registry_version >= 1),
    registry_fingerprint char(64) NOT NULL
        CHECK (registry_fingerprint ~ '^[0-9a-f]{64}$'),
    registry_target varchar(500) NOT NULL
        CHECK (length(trim(registry_target)) BETWEEN 1 AND 500),
    transition_id varchar(200) NOT NULL UNIQUE
        CHECK (transition_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    activated_by varchar(120) NOT NULL
        CHECK (length(trim(activated_by)) BETWEEN 1 AND 120),
    activated_at timestamptz NOT NULL,
    decision_ids_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(decision_ids_json) = 'array'
            AND jsonb_array_length(decision_ids_json) BETWEEN 1 AND 2000
        ),
    PRIMARY KEY (workspace_id, catalog_scope, registry_id),
    CONSTRAINT registry_active_pointer_transition_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            generation,
            transition_id
        )
        REFERENCES schemabridge_control.registry_activation_transitions (
            workspace_id,
            catalog_scope,
            registry_id,
            generation,
            transition_id
        )
);

CREATE TABLE schemabridge_control.registry_reconciliation_outbox (
    outbox_id varchar(200) PRIMARY KEY
        CHECK (outbox_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    transition_id varchar(200) NOT NULL UNIQUE
        CHECK (transition_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    catalog_scope varchar(120) NOT NULL
        CHECK (length(trim(catalog_scope)) BETWEEN 1 AND 120),
    registry_id varchar(80) NOT NULL
        CHECK (length(trim(registry_id)) BETWEEN 1 AND 80),
    generation bigint NOT NULL CHECK (generation >= 1),
    desired_projection_fingerprint char(64) NOT NULL
        CHECK (desired_projection_fingerprint ~ '^[0-9a-f]{64}$'),
    desired_projection_json jsonb NOT NULL
        CHECK (jsonb_typeof(desired_projection_json) = 'object'),
    status varchar(16) NOT NULL
        CHECK (status IN ('pending', 'delivered', 'superseded', 'blocked')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_reason_code varchar(64)
        CHECK (
            last_reason_code IS NULL
            OR last_reason_code ~ '^[a-z][a-z0-9_]{1,63}$'
        ),
    observed_projection_json jsonb
        CHECK (
            observed_projection_json IS NULL
            OR jsonb_typeof(observed_projection_json) = 'object'
        ),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    delivered_at timestamptz,
    CONSTRAINT registry_outbox_transition_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            generation,
            transition_id
        )
        REFERENCES schemabridge_control.registry_activation_transitions (
            workspace_id,
            catalog_scope,
            registry_id,
            generation,
            transition_id
        ),
    CONSTRAINT registry_outbox_reason_shape
        CHECK (
            (status = 'blocked' AND last_reason_code IS NOT NULL)
            OR (status <> 'blocked' AND last_reason_code IS NULL)
        ),
    CONSTRAINT registry_outbox_delivery_shape
        CHECK (
            (status = 'delivered' AND delivered_at IS NOT NULL)
            OR (status <> 'delivered' AND delivered_at IS NULL)
        ),
    CONSTRAINT registry_outbox_time_order
        CHECK (
            updated_at >= created_at
            AND (delivered_at IS NULL OR delivered_at >= created_at)
        )
);

CREATE INDEX registry_reconciliation_outbox_pending_idx
    ON schemabridge_control.registry_reconciliation_outbox (
        workspace_id,
        catalog_scope,
        registry_id,
        generation DESC
    )
    WHERE status = 'pending';

CREATE TABLE schemabridge_control.control_audit_events (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id varchar(200) NOT NULL UNIQUE
        CHECK (event_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    operation varchar(64) NOT NULL
        CHECK (operation ~ '^[a-z][a-z0-9_]{1,63}$'),
    transition_id varchar(200),
    previous_hash char(64)
        CHECK (previous_hash IS NULL OR previous_hash ~ '^[0-9a-f]{64}$'),
    event_hash char(64) NOT NULL UNIQUE
        CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    payload_fingerprint char(64) NOT NULL
        CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
    key_version varchar(80) NOT NULL
        CHECK (length(trim(key_version)) BETWEEN 1 AND 80),
    event_json jsonb NOT NULL CHECK (jsonb_typeof(event_json) = 'object'),
    occurred_at timestamptz NOT NULL,
    CONSTRAINT control_audit_transition_fk
        FOREIGN KEY (workspace_id, transition_id)
        REFERENCES schemabridge_control.registry_activation_transitions (
            workspace_id,
            transition_id
        )
);

CREATE INDEX control_audit_events_workspace_sequence_idx
    ON schemabridge_control.control_audit_events (workspace_id, sequence);

CREATE TABLE schemabridge_control.agent_workflow_drafts (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    id varchar(64) NOT NULL
        CHECK (id ~ '^[a-z0-9][a-z0-9_-]{2,63}$'),
    revision integer NOT NULL CHECK (revision >= 1),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    execution_row_count integer CHECK (execution_row_count >= 0),
    execution_preview_fingerprint char(64)
        CHECK (
            execution_preview_fingerprint IS NULL
            OR execution_preview_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, id),
    CONSTRAINT workflow_execution_summary_shape
        CHECK (
            (execution_row_count IS NULL) =
            (execution_preview_fingerprint IS NULL)
        )
);

CREATE TABLE schemabridge_control.workflow_access_grants (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    workflow_id varchar(64) NOT NULL
        CHECK (workflow_id ~ '^[a-z0-9][a-z0-9_-]{2,63}$'),
    owner_actor_id varchar(200) NOT NULL
        CHECK (length(trim(owner_actor_id)) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, workflow_id),
    CONSTRAINT workflow_access_draft_fk
        FOREIGN KEY (workspace_id, workflow_id)
        REFERENCES schemabridge_control.agent_workflow_drafts (workspace_id, id)
);

CREATE INDEX workflow_access_grants_owner_idx
    ON schemabridge_control.workflow_access_grants (
        workspace_id,
        owner_actor_id,
        created_at DESC,
        workflow_id
    );

CREATE TABLE schemabridge_control.analytical_request_drafts (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    id varchar(200) NOT NULL
        CHECK (id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'),
    revision integer NOT NULL CHECK (revision >= 1),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, id)
);

CREATE TABLE schemabridge_control.review_drafts (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    id varchar(200) NOT NULL
        CHECK (id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'),
    revision integer NOT NULL CHECK (revision >= 1),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, id)
);

CREATE TABLE schemabridge_control.review_decisions (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    id varchar(200) NOT NULL
        CHECK (length(trim(id)) BETWEEN 1 AND 200),
    draft_id varchar(200) NOT NULL,
    resulting_version integer NOT NULL CHECK (resulting_version >= 1),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    decided_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, id),
    UNIQUE (workspace_id, draft_id, resulting_version),
    CONSTRAINT review_decision_draft_fk
        FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.review_drafts (workspace_id, id)
);

CREATE TABLE schemabridge_control.review_publications (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    draft_id varchar(200) NOT NULL,
    approval_id varchar(200) NOT NULL
        CHECK (length(trim(approval_id)) BETWEEN 1 AND 200),
    fingerprint char(64) NOT NULL
        CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    published_at timestamptz NOT NULL,
    UNIQUE (workspace_id, approval_id),
    CONSTRAINT review_publication_draft_fk
        FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.review_drafts (workspace_id, id)
);

CREATE INDEX review_publications_draft_idx
    ON schemabridge_control.review_publications (
        workspace_id,
        draft_id,
        sequence
    );

CREATE TABLE schemabridge_control.join_review_drafts (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    id varchar(200) NOT NULL
        CHECK (id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'),
    revision integer NOT NULL CHECK (revision >= 1),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, id)
);

CREATE TABLE schemabridge_control.join_review_decisions (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    id varchar(200) NOT NULL
        CHECK (length(trim(id)) BETWEEN 1 AND 200),
    draft_id varchar(200) NOT NULL,
    resulting_version integer NOT NULL CHECK (resulting_version >= 1),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    decided_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, id),
    UNIQUE (workspace_id, draft_id, resulting_version),
    CONSTRAINT join_review_decision_draft_fk
        FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.join_review_drafts (workspace_id, id)
);

CREATE TABLE schemabridge_control.join_publications (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    draft_id varchar(200) NOT NULL,
    approval_id varchar(200) NOT NULL
        CHECK (length(trim(approval_id)) BETWEEN 1 AND 200),
    fingerprint char(64) NOT NULL
        CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    published_at timestamptz NOT NULL,
    UNIQUE (workspace_id, approval_id),
    CONSTRAINT join_publication_draft_fk
        FOREIGN KEY (workspace_id, draft_id)
        REFERENCES schemabridge_control.join_review_drafts (workspace_id, id)
);

CREATE INDEX join_publications_draft_idx
    ON schemabridge_control.join_publications (
        workspace_id,
        draft_id,
        sequence
    );

CREATE TABLE schemabridge_control.publication_approval_identity (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    approval_id varchar(200) NOT NULL
        CHECK (length(trim(approval_id)) BETWEEN 1 AND 200),
    family varchar(24) NOT NULL
        CHECK (family IN ('canonical', 'join', 'registry', 'workflow', 'recipe')),
    actor varchar(120) NOT NULL
        CHECK (length(trim(actor)) BETWEEN 1 AND 120),
    approved_at timestamptz NOT NULL,
    new_fingerprint char(64) NOT NULL
        CHECK (new_fingerprint ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (workspace_id, approval_id)
);

CREATE TABLE schemabridge_control.publication_target_audit (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    approval_id varchar(200) NOT NULL,
    family varchar(24) NOT NULL
        CHECK (family IN ('canonical', 'join', 'registry', 'workflow', 'recipe')),
    operation varchar(64) NOT NULL
        CHECK (operation ~ '^[a-z][a-z0-9_]{1,63}$'),
    target varchar(500) NOT NULL
        CHECK (length(trim(target)) BETWEEN 1 AND 500),
    record_json jsonb NOT NULL CHECK (jsonb_typeof(record_json) = 'object'),
    appended_at timestamptz NOT NULL,
    CONSTRAINT publication_target_approval_fk
        FOREIGN KEY (workspace_id, approval_id)
        REFERENCES schemabridge_control.publication_approval_identity (
            workspace_id,
            approval_id
        )
);

CREATE INDEX publication_target_audit_approval_idx
    ON schemabridge_control.publication_target_audit (
        workspace_id,
        approval_id,
        sequence
    );

CREATE TABLE schemabridge_control.identity_bindings (
    binding_id varchar(200) PRIMARY KEY
        CHECK (binding_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    binding_kind varchar(16) NOT NULL
        CHECK (binding_kind IN ('workspace', 'actor')),
    stable_reference_digest char(64) NOT NULL
        CHECK (stable_reference_digest ~ '^[0-9a-f]{64}$'),
    opaque_id varchar(200) NOT NULL
        CHECK (length(trim(opaque_id)) BETWEEN 1 AND 200),
    key_version varchar(80) NOT NULL
        CHECK (length(trim(key_version)) BETWEEN 1 AND 80),
    provenance_version integer NOT NULL CHECK (provenance_version >= 1),
    policy_version integer NOT NULL CHECK (policy_version >= 1),
    provenance_fingerprint char(64) NOT NULL
        CHECK (provenance_fingerprint ~ '^[0-9a-f]{64}$'),
    status varchar(16) NOT NULL
        CHECK (status IN ('active', 'previous', 'quarantined')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
    UNIQUE (
        workspace_id,
        binding_kind,
        opaque_id,
        key_version
    ),
    UNIQUE (
        workspace_id,
        binding_kind,
        stable_reference_digest,
        key_version
    )
);

CREATE INDEX identity_bindings_workspace_status_idx
    ON schemabridge_control.identity_bindings (
        workspace_id,
        status,
        binding_kind
    );

CREATE TABLE schemabridge_control.identity_rotation_plans (
    plan_id varchar(200) PRIMARY KEY
        CHECK (plan_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    from_key_version varchar(80) NOT NULL,
    to_key_version varchar(80) NOT NULL,
    provenance_version integer NOT NULL CHECK (provenance_version >= 1),
    policy_version integer NOT NULL CHECK (policy_version >= 1),
    plan_fingerprint char(64) NOT NULL
        CHECK (plan_fingerprint ~ '^[0-9a-f]{64}$'),
    approval_id varchar(200) NOT NULL UNIQUE
        CHECK (length(trim(approval_id)) BETWEEN 1 AND 200),
    actor varchar(120) NOT NULL
        CHECK (length(trim(actor)) BETWEEN 1 AND 120),
    approved_at timestamptz NOT NULL,
    status varchar(16) NOT NULL
        CHECK (status IN ('approved', 'applying', 'completed', 'blocked')),
    expected_binding_count integer NOT NULL CHECK (expected_binding_count >= 1),
    verified_binding_count integer NOT NULL DEFAULT 0
        CHECK (verified_binding_count >= 0),
    payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    CONSTRAINT identity_rotation_keys_differ
        CHECK (
            length(trim(from_key_version)) BETWEEN 1 AND 80
            AND length(trim(to_key_version)) BETWEEN 1 AND 80
            AND from_key_version <> to_key_version
        ),
    CONSTRAINT identity_rotation_counts_bounded
        CHECK (verified_binding_count <= expected_binding_count),
    CONSTRAINT identity_rotation_completion_shape
        CHECK (
            (
                status = 'completed'
                AND completed_at IS NOT NULL
                AND verified_binding_count = expected_binding_count
            )
            OR (status <> 'completed' AND completed_at IS NULL)
        ),
    CONSTRAINT identity_rotation_time_order
        CHECK (
            updated_at >= created_at
            AND (completed_at IS NULL OR completed_at >= created_at)
        )
);

CREATE TABLE schemabridge_control.identity_rotation_bindings (
    plan_id varchar(200) NOT NULL,
    binding_kind varchar(16) NOT NULL
        CHECK (binding_kind IN ('workspace', 'actor')),
    stable_reference_digest char(64) NOT NULL
        CHECK (stable_reference_digest ~ '^[0-9a-f]{64}$'),
    old_opaque_id varchar(200) NOT NULL
        CHECK (length(trim(old_opaque_id)) BETWEEN 1 AND 200),
    new_opaque_id varchar(200) NOT NULL
        CHECK (length(trim(new_opaque_id)) BETWEEN 1 AND 200),
    binding_fingerprint char(64) NOT NULL
        CHECK (binding_fingerprint ~ '^[0-9a-f]{64}$'),
    status varchar(16) NOT NULL
        CHECK (status IN ('planned', 'verified', 'blocked')),
    verified_at timestamptz,
    PRIMARY KEY (plan_id, binding_kind, stable_reference_digest),
    UNIQUE (plan_id, binding_kind, old_opaque_id),
    UNIQUE (plan_id, binding_kind, new_opaque_id),
    CONSTRAINT identity_rotation_binding_plan_fk
        FOREIGN KEY (plan_id)
        REFERENCES schemabridge_control.identity_rotation_plans (plan_id),
    CONSTRAINT identity_rotation_binding_ids_differ
        CHECK (old_opaque_id <> new_opaque_id),
    CONSTRAINT identity_rotation_binding_verified_shape
        CHECK (
            (status = 'verified' AND verified_at IS NOT NULL)
            OR (status <> 'verified' AND verified_at IS NULL)
        )
);

CREATE TABLE schemabridge_control.legacy_control_imports (
    import_id varchar(200) PRIMARY KEY
        CHECK (import_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    source_kind varchar(24) NOT NULL CHECK (source_kind = 'sqlite'),
    source_fingerprint char(64) NOT NULL UNIQUE
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
    source_schema_fingerprint char(64) NOT NULL
        CHECK (source_schema_fingerprint ~ '^[0-9a-f]{64}$'),
    plan_fingerprint char(64) NOT NULL
        CHECK (plan_fingerprint ~ '^[0-9a-f]{64}$'),
    approval_id varchar(200)
        CHECK (
            approval_id IS NULL
            OR length(trim(approval_id)) BETWEEN 1 AND 200
        ),
    actor varchar(120)
        CHECK (actor IS NULL OR length(trim(actor)) BETWEEN 1 AND 120),
    approved_at timestamptz,
    status varchar(24) NOT NULL
        CHECK (
            status IN (
                'dry_run',
                'approved',
                'importing',
                'completed',
                'blocked'
            )
        ),
    counts_json jsonb NOT NULL CHECK (jsonb_typeof(counts_json) = 'object'),
    created_at timestamptz NOT NULL,
    completed_at timestamptz,
    CONSTRAINT legacy_import_approval_shape
        CHECK (
            (
                status = 'dry_run'
                AND approval_id IS NULL
                AND actor IS NULL
                AND approved_at IS NULL
            )
            OR
            (
                status <> 'dry_run'
                AND approval_id IS NOT NULL
                AND actor IS NOT NULL
                AND approved_at IS NOT NULL
            )
        ),
    CONSTRAINT legacy_import_completion_shape
        CHECK (
            (status = 'completed' AND completed_at IS NOT NULL)
            OR (status <> 'completed' AND completed_at IS NULL)
        )
);

CREATE TABLE schemabridge_control.legacy_control_import_items (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_id varchar(200) NOT NULL,
    source_table varchar(120) NOT NULL
        CHECK (source_table ~ '^[a-z][a-z0-9_]{0,119}$'),
    source_id_digest char(64) NOT NULL
        CHECK (source_id_digest ~ '^[0-9a-f]{64}$'),
    outcome varchar(16) NOT NULL
        CHECK (outcome IN ('imported', 'quarantined', 'skipped')),
    target_type varchar(80),
    target_id varchar(200),
    reason_code varchar(64)
        CHECK (
            reason_code IS NULL
            OR reason_code ~ '^[a-z][a-z0-9_]{1,63}$'
        ),
    payload_fingerprint char(64) NOT NULL
        CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    UNIQUE (import_id, source_table, source_id_digest),
    CONSTRAINT legacy_import_item_import_fk
        FOREIGN KEY (import_id)
        REFERENCES schemabridge_control.legacy_control_imports (import_id),
    CONSTRAINT legacy_import_item_target_shape
        CHECK (
            (
                outcome = 'imported'
                AND target_type IS NOT NULL
                AND target_id IS NOT NULL
                AND reason_code IS NULL
            )
            OR
            (
                outcome IN ('quarantined', 'skipped')
                AND reason_code IS NOT NULL
            )
        )
);

CREATE TABLE schemabridge_control.control_quarantine_items (
    quarantine_id varchar(200) PRIMARY KEY
        CHECK (quarantine_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    import_id varchar(200),
    workspace_id varchar(200)
        CHECK (
            workspace_id IS NULL
            OR length(trim(workspace_id)) BETWEEN 1 AND 200
        ),
    resource_type varchar(80) NOT NULL
        CHECK (resource_type ~ '^[a-z][a-z0-9_]{0,79}$'),
    resource_id_digest char(64) NOT NULL
        CHECK (resource_id_digest ~ '^[0-9a-f]{64}$'),
    reason_code varchar(64) NOT NULL
        CHECK (reason_code ~ '^[a-z][a-z0-9_]{1,63}$'),
    payload_fingerprint char(64) NOT NULL
        CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
    details_json jsonb NOT NULL CHECK (jsonb_typeof(details_json) = 'object'),
    detected_at timestamptz NOT NULL,
    resolved_at timestamptz,
    resolution_event_id varchar(200),
    UNIQUE (import_id, resource_type, resource_id_digest),
    CONSTRAINT control_quarantine_import_fk
        FOREIGN KEY (import_id)
        REFERENCES schemabridge_control.legacy_control_imports (import_id),
    CONSTRAINT control_quarantine_resolution_shape
        CHECK (
            (resolved_at IS NULL AND resolution_event_id IS NULL)
            OR (resolved_at IS NOT NULL AND resolution_event_id IS NOT NULL)
        )
);

CREATE FUNCTION schemabridge_control.reject_immutable_control_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'immutable control record cannot be changed'
        USING ERRCODE = '55000';
END;
$$;

REVOKE ALL ON FUNCTION
    schemabridge_control.reject_immutable_control_mutation()
    FROM PUBLIC;

CREATE FUNCTION schemabridge_control.enforce_legacy_control_import_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'legacy import records cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF (
            NEW.status <> 'dry_run'
            OR NEW.approval_id IS NOT NULL
            OR NEW.actor IS NOT NULL
            OR NEW.approved_at IS NOT NULL
            OR NEW.completed_at IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'legacy import reservation must start as a dry run'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status <> 'dry_run' OR NEW.status <> 'completed' THEN
        RAISE EXCEPTION 'legacy import lifecycle transition is invalid'
            USING ERRCODE = '55000';
    END IF;

    IF (
        NEW.import_id IS DISTINCT FROM OLD.import_id
        OR NEW.source_kind IS DISTINCT FROM OLD.source_kind
        OR NEW.source_fingerprint IS DISTINCT FROM OLD.source_fingerprint
        OR NEW.source_schema_fingerprint IS DISTINCT FROM OLD.source_schema_fingerprint
        OR NEW.plan_fingerprint IS DISTINCT FROM OLD.plan_fingerprint
        OR NEW.counts_json IS DISTINCT FROM OLD.counts_json
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    ) THEN
        RAISE EXCEPTION 'legacy import reservation identity cannot be changed'
            USING ERRCODE = '55000';
    END IF;

    IF (
        OLD.approval_id IS NOT NULL
        OR OLD.actor IS NOT NULL
        OR OLD.approved_at IS NOT NULL
        OR OLD.completed_at IS NOT NULL
        OR NEW.approval_id IS NULL
        OR NEW.actor IS NULL
        OR NEW.approved_at IS NULL
        OR NEW.completed_at IS NULL
    ) THEN
        RAISE EXCEPTION 'legacy import completion evidence is invalid'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.approved_at < OLD.created_at OR NEW.completed_at < NEW.approved_at THEN
        RAISE EXCEPTION 'legacy import completion time is invalid'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION
    schemabridge_control.enforce_legacy_control_import_lifecycle()
    FROM PUBLIC;

CREATE FUNCTION schemabridge_control.enforce_control_quarantine_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.resolved_at IS NOT NULL OR NEW.resolution_event_id IS NOT NULL THEN
            RAISE EXCEPTION 'new quarantine records must be unresolved'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'quarantine records cannot be changed'
        USING ERRCODE = '55000';
END;
$$;

REVOKE ALL ON FUNCTION
    schemabridge_control.enforce_control_quarantine_lifecycle()
    FROM PUBLIC;

CREATE TRIGGER schema_migrations_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.schema_migrations
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER registry_activation_transitions_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.registry_activation_transitions
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER control_audit_events_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.control_audit_events
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER workflow_access_grants_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.workflow_access_grants
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER review_decisions_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.review_decisions
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER review_publications_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.review_publications
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER join_review_decisions_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.join_review_decisions
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER join_publications_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.join_publications
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER publication_approval_identity_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.publication_approval_identity
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER publication_target_audit_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.publication_target_audit
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER legacy_control_import_items_immutable
    BEFORE UPDATE OR DELETE
    ON schemabridge_control.legacy_control_import_items
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.reject_immutable_control_mutation();

CREATE TRIGGER legacy_control_imports_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE
    ON schemabridge_control.legacy_control_imports
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.enforce_legacy_control_import_lifecycle();

CREATE TRIGGER control_quarantine_items_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE
    ON schemabridge_control.control_quarantine_items
    FOR EACH ROW
    EXECUTE FUNCTION schemabridge_control.enforce_control_quarantine_lifecycle();

REVOKE ALL ON ALL TABLES IN SCHEMA schemabridge_control FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA schemabridge_control FROM PUBLIC;

GRANT SELECT ON schemabridge_control.schema_migrations
    TO schemabridge_runtime, schemabridge_reconciler;

GRANT SELECT, INSERT ON schemabridge_control.registry_activation_transitions
    TO schemabridge_runtime;
GRANT SELECT, INSERT, UPDATE ON schemabridge_control.registry_active_pointers
    TO schemabridge_runtime;
GRANT SELECT, INSERT ON schemabridge_control.registry_reconciliation_outbox
    TO schemabridge_runtime;
GRANT SELECT, INSERT ON schemabridge_control.control_audit_events
    TO schemabridge_runtime;

GRANT SELECT ON
    schemabridge_control.registry_activation_transitions,
    schemabridge_control.registry_active_pointers,
    schemabridge_control.registry_reconciliation_outbox,
    schemabridge_control.control_audit_events
    TO schemabridge_reconciler;
GRANT UPDATE (
    status,
    attempts,
    last_reason_code,
    observed_projection_json,
    updated_at,
    delivered_at
) ON schemabridge_control.registry_reconciliation_outbox
    TO schemabridge_reconciler;
GRANT INSERT ON schemabridge_control.control_audit_events
    TO schemabridge_reconciler;

GRANT SELECT, INSERT, UPDATE ON
    schemabridge_control.agent_workflow_drafts,
    schemabridge_control.analytical_request_drafts,
    schemabridge_control.review_drafts,
    schemabridge_control.join_review_drafts,
    schemabridge_control.identity_bindings,
    schemabridge_control.identity_rotation_plans,
    schemabridge_control.identity_rotation_bindings
    TO schemabridge_runtime;

GRANT SELECT, INSERT ON
    schemabridge_control.legacy_control_imports,
    schemabridge_control.control_quarantine_items
    TO schemabridge_runtime;

GRANT UPDATE (
    approval_id,
    actor,
    approved_at,
    status,
    completed_at
) ON schemabridge_control.legacy_control_imports
    TO schemabridge_runtime;

GRANT SELECT, INSERT ON
    schemabridge_control.workflow_access_grants,
    schemabridge_control.review_decisions,
    schemabridge_control.review_publications,
    schemabridge_control.join_review_decisions,
    schemabridge_control.join_publications,
    schemabridge_control.publication_approval_identity,
    schemabridge_control.publication_target_audit,
    schemabridge_control.legacy_control_import_items
    TO schemabridge_runtime;

GRANT USAGE, SELECT ON SEQUENCE
    schemabridge_control.control_audit_events_sequence_seq,
    schemabridge_control.review_publications_sequence_seq,
    schemabridge_control.join_publications_sequence_seq,
    schemabridge_control.publication_target_audit_sequence_seq,
    schemabridge_control.legacy_control_import_items_sequence_seq
    TO schemabridge_runtime;

GRANT USAGE, SELECT ON SEQUENCE
    schemabridge_control.control_audit_events_sequence_seq
    TO schemabridge_reconciler;
