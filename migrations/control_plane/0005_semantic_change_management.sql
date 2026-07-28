CREATE UNIQUE INDEX control_audit_events_workspace_event_idx
    ON schemabridge_control.control_audit_events (workspace_id, event_id);

CREATE UNIQUE INDEX registry_activation_transition_target_idx
    ON schemabridge_control.registry_activation_transitions (
        workspace_id,
        catalog_scope,
        registry_id,
        generation,
        transition_id,
        target_registry_version,
        target_registry_fingerprint
    );

CREATE UNIQUE INDEX catalog_connections_scope_identity_idx
    ON schemabridge_control.catalog_connections (
        workspace_id,
        catalog_scope,
        connection_id
    );

CREATE FUNCTION schemabridge_control.semantic_definition_fingerprint(varchar)
RETURNS char(64)
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
    SELECT encode(
        sha256(
            convert_to(
                jsonb_build_object('description', $1)::text,
                'UTF8'
            )
        ),
        'hex'
    )::char(64);
$$;

CREATE FUNCTION schemabridge_control.semantic_terms_fingerprint(
    varchar[],
    varchar[]
)
RETURNS char(64)
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
    SELECT encode(
        sha256(
            convert_to(
                jsonb_build_object(
                    'tags',
                    coalesce(to_jsonb($1), '[]'::jsonb),
                    'glossary_terms',
                    coalesce(to_jsonb($2), '[]'::jsonb)
                )::text,
                'UTF8'
            )
        ),
        'hex'
    )::char(64);
$$;

CREATE FUNCTION schemabridge_control.semantic_json_has_protected_keys(jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
    WITH RECURSIVE values_to_inspect(value) AS (
        SELECT coalesce($1, 'null'::jsonb)
        UNION ALL
        SELECT nested.value
        FROM values_to_inspect AS current_value
        CROSS JOIN LATERAL (
            SELECT object_value AS value
            FROM jsonb_each(
                CASE
                    WHEN jsonb_typeof(current_value.value) = 'object'
                    THEN current_value.value
                    ELSE '{}'::jsonb
                END
            ) AS object_member(object_key, object_value)
            UNION ALL
            SELECT array_value AS value
            FROM jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(current_value.value) = 'array'
                    THEN current_value.value
                    ELSE '[]'::jsonb
                END
            ) AS array_member(array_value)
        ) AS nested
    )
    SELECT EXISTS (
        SELECT 1
        FROM values_to_inspect AS current_value
        CROSS JOIN LATERAL jsonb_object_keys(
            CASE
                WHEN jsonb_typeof(current_value.value) = 'object'
                THEN current_value.value
                ELSE '{}'::jsonb
            END
        ) AS member(key_name)
        WHERE lower(member.key_name) ~
            '^(raw_value|source_row|sample_value|sql|parameters|prompt|token|dsn|password|secret|api_key|.*_api_key|access_token|authorization|credential|connection_string|client_secret)$'
    );
$$;

CREATE TABLE schemabridge_control.catalog_generation_changes (
    change_id varchar(80) PRIMARY KEY
        CHECK (change_id ~ '^chg_[0-9a-f]{64}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (
            length(trim(workspace_id)) BETWEEN 1 AND 200
            AND octet_length(workspace_id) <= 200
        ),
    connection_id varchar(200) NOT NULL
        CHECK (connection_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    base_generation bigint CHECK (base_generation > 0),
    observed_generation bigint NOT NULL CHECK (observed_generation > 0),
    resource_kind varchar(16) NOT NULL
        CHECK (resource_kind IN ('asset', 'field')),
    change_kind varchar(24) NOT NULL
        CHECK (change_kind IN ('added', 'removed', 'metadata_changed')),
    asset_key char(64) NOT NULL CHECK (asset_key ~ '^[0-9a-f]{64}$'),
    asset_id varchar(500) NOT NULL
        CHECK (
            length(asset_id) BETWEEN 1 AND 500
            AND octet_length(asset_id) <= 2000
        ),
    resource_key char(64) NOT NULL
        CHECK (resource_key ~ '^[0-9a-f]{64}$'),
    field_key char(64)
        CHECK (field_key IS NULL OR field_key ~ '^[0-9a-f]{64}$'),
    field_path varchar(200)[]
        CHECK (
            field_path IS NULL
            OR (
                cardinality(field_path) BETWEEN 1 AND 64
                AND array_position(field_path, NULL) IS NULL
                AND octet_length(array_to_string(field_path, '.')) <= 12800
            )
        ),
    previous_metadata_fingerprint char(64)
        CHECK (
            previous_metadata_fingerprint IS NULL
            OR previous_metadata_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    current_metadata_fingerprint char(64)
        CHECK (
            current_metadata_fingerprint IS NULL
            OR current_metadata_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    change_fingerprint char(64) NOT NULL UNIQUE
        CHECK (change_fingerprint ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    UNIQUE (
        workspace_id,
        connection_id,
        observed_generation,
        resource_kind,
        asset_key,
        resource_key
    ),
    CONSTRAINT catalog_generation_change_connection_fk
        FOREIGN KEY (workspace_id, connection_id)
        REFERENCES schemabridge_control.catalog_connections (
            workspace_id,
            connection_id
        ),
    CONSTRAINT catalog_generation_change_generation_shape
        CHECK (
            base_generation IS NULL
            OR observed_generation > base_generation
        ),
    CONSTRAINT catalog_generation_change_resource_shape
        CHECK (
            (
                resource_kind = 'asset'
                AND field_key IS NULL
                AND field_path IS NULL
                AND resource_key = asset_key
            )
            OR
            (
                resource_kind = 'field'
                AND field_key IS NOT NULL
                AND field_path IS NOT NULL
                AND resource_key = field_key
            )
        ),
    CONSTRAINT catalog_generation_change_fingerprint_shape
        CHECK (
            (
                change_kind = 'added'
                AND previous_metadata_fingerprint IS NULL
                AND current_metadata_fingerprint IS NOT NULL
            )
            OR
            (
                change_kind = 'removed'
                AND previous_metadata_fingerprint IS NOT NULL
                AND current_metadata_fingerprint IS NULL
            )
            OR
            (
                change_kind = 'metadata_changed'
                AND previous_metadata_fingerprint IS NOT NULL
                AND current_metadata_fingerprint IS NOT NULL
                AND previous_metadata_fingerprint
                    <> current_metadata_fingerprint
            )
        )
);

CREATE INDEX catalog_generation_changes_generation_idx
    ON schemabridge_control.catalog_generation_changes (
        workspace_id,
        connection_id,
        observed_generation,
        resource_kind,
        asset_key,
        resource_key
    );

CREATE INDEX catalog_generation_changes_resource_idx
    ON schemabridge_control.catalog_generation_changes (
        workspace_id,
        connection_id,
        asset_id,
        field_key,
        observed_generation DESC,
        change_id
    );

CREATE TABLE schemabridge_control.semantic_resource_bindings (
    binding_id varchar(200) NOT NULL
        CHECK (binding_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    catalog_scope varchar(120) NOT NULL
        CHECK (length(trim(catalog_scope)) BETWEEN 1 AND 120),
    registry_id varchar(80) NOT NULL
        CHECK (length(trim(registry_id)) BETWEEN 1 AND 80),
    binding_revision bigint NOT NULL CHECK (binding_revision >= 1),
    registry_generation bigint NOT NULL CHECK (registry_generation >= 1),
    registry_version bigint NOT NULL CHECK (registry_version >= 1),
    registry_fingerprint char(64) NOT NULL
        CHECK (registry_fingerprint ~ '^[0-9a-f]{64}$'),
    pointer_transition_id varchar(200) NOT NULL
        CHECK (pointer_transition_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    mapping_decision_id varchar(200) NOT NULL
        CHECK (length(trim(mapping_decision_id)) BETWEEN 1 AND 200),
    mapping_version bigint NOT NULL CHECK (mapping_version >= 1),
    logical_field varchar(200) NOT NULL
        CHECK (
            length(trim(logical_field)) BETWEEN 1 AND 200
            AND octet_length(logical_field) <= 400
        ),
    connection_id varchar(200) NOT NULL
        CHECK (connection_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    catalog_generation bigint NOT NULL CHECK (catalog_generation > 0),
    catalog_generation_fingerprint char(64) NOT NULL
        CHECK (catalog_generation_fingerprint ~ '^[0-9a-f]{64}$'),
    asset_key char(64) NOT NULL CHECK (asset_key ~ '^[0-9a-f]{64}$'),
    asset_id varchar(500) NOT NULL
        CHECK (
            length(asset_id) BETWEEN 1 AND 500
            AND octet_length(asset_id) <= 2000
        ),
    field_key char(64) NOT NULL CHECK (field_key ~ '^[0-9a-f]{64}$'),
    field_path varchar(200)[] NOT NULL
        CHECK (
            cardinality(field_path) BETWEEN 1 AND 64
            AND array_position(field_path, NULL) IS NULL
            AND octet_length(array_to_string(field_path, '.')) <= 12800
        ),
    normalized_type varchar(32)
        CHECK (
            normalized_type IS NULL
            OR normalized_type IN (
                'string',
                'integer',
                'decimal',
                'float',
                'boolean',
                'date',
                'timestamp',
                'binary',
                'struct',
                'array',
                'unknown'
            )
        ),
    nullable boolean,
    is_part_of_key boolean,
    field_metadata_fingerprint char(64) NOT NULL
        CHECK (field_metadata_fingerprint ~ '^[0-9a-f]{64}$'),
    field_definition_fingerprint char(64) NOT NULL
        CHECK (field_definition_fingerprint ~ '^[0-9a-f]{64}$'),
    field_terms_fingerprint char(64) NOT NULL
        CHECK (field_terms_fingerprint ~ '^[0-9a-f]{64}$'),
    asset_metadata_fingerprint char(64) NOT NULL
        CHECK (asset_metadata_fingerprint ~ '^[0-9a-f]{64}$'),
    evidence_fingerprint char(64) NOT NULL
        CHECK (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
    binding_fingerprint char(64) NOT NULL UNIQUE
        CHECK (binding_fingerprint ~ '^[0-9a-f]{64}$'),
    binding_state varchar(24) NOT NULL
        CHECK (
            binding_state IN (
                'proposed',
                'approved',
                'revalidated',
                'rejected',
                'superseded'
            )
        ),
    approved_baseline_revision bigint
        CHECK (approved_baseline_revision >= 1),
    approval_id varchar(200)
        CHECK (
            approval_id IS NULL
            OR approval_id ~ '^[a-z][a-z0-9_-]{2,199}$'
        ),
    actor_id varchar(200)
        CHECK (
            actor_id IS NULL
            OR length(trim(actor_id)) BETWEEN 1 AND 200
        ),
    decided_at timestamptz,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, binding_id),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        binding_id
    ),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        registry_generation,
        registry_fingerprint,
        binding_id
    ),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        registry_generation,
        registry_fingerprint,
        binding_id,
        connection_id,
        asset_key,
        field_key
    ),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        mapping_decision_id,
        binding_revision
    ),
    CONSTRAINT semantic_binding_registry_transition_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            registry_generation,
            pointer_transition_id,
            registry_version,
            registry_fingerprint
        )
        REFERENCES schemabridge_control.registry_activation_transitions (
            workspace_id,
            catalog_scope,
            registry_id,
            generation,
            transition_id,
            target_registry_version,
            target_registry_fingerprint
        ),
    CONSTRAINT semantic_binding_catalog_scope_fk
        FOREIGN KEY (workspace_id, catalog_scope, connection_id)
        REFERENCES schemabridge_control.catalog_connections (
            workspace_id,
            catalog_scope,
            connection_id
        ),
    CONSTRAINT semantic_binding_decision_shape
        CHECK (
            (
                binding_state = 'proposed'
                AND approved_baseline_revision IS NULL
                AND approval_id IS NULL
                AND actor_id IS NULL
                AND decided_at IS NULL
            )
            OR
            (
                binding_state <> 'proposed'
                AND approval_id IS NOT NULL
                AND actor_id IS NOT NULL
                AND decided_at IS NOT NULL
                AND (
                    (
                        binding_state IN ('approved', 'revalidated', 'superseded')
                        AND approved_baseline_revision IS NOT NULL
                    )
                    OR
                    (
                        binding_state = 'rejected'
                        AND approved_baseline_revision IS NULL
                    )
                )
            )
        )
);

CREATE INDEX semantic_resource_bindings_scope_idx
    ON schemabridge_control.semantic_resource_bindings (
        workspace_id,
        catalog_scope,
        registry_id,
        registry_generation,
        mapping_decision_id,
        binding_revision DESC
    );

CREATE INDEX semantic_resource_bindings_resource_idx
    ON schemabridge_control.semantic_resource_bindings (
        workspace_id,
        connection_id,
        asset_key,
        field_key,
        catalog_generation DESC,
        binding_id
    );

CREATE INDEX semantic_resource_bindings_gate_idx
    ON schemabridge_control.semantic_resource_bindings (
        workspace_id,
        catalog_scope,
        registry_id,
        registry_generation,
        approved_baseline_revision,
        mapping_decision_id
    )
    WHERE binding_state IN ('approved', 'revalidated');

CREATE TABLE schemabridge_control.semantic_join_profiles (
    profile_id varchar(200) NOT NULL
        CHECK (profile_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    catalog_scope varchar(120) NOT NULL
        CHECK (length(trim(catalog_scope)) BETWEEN 1 AND 120),
    registry_id varchar(80) NOT NULL
        CHECK (length(trim(registry_id)) BETWEEN 1 AND 80),
    registry_generation bigint NOT NULL CHECK (registry_generation >= 1),
    registry_version bigint NOT NULL CHECK (registry_version >= 1),
    registry_fingerprint char(64) NOT NULL
        CHECK (registry_fingerprint ~ '^[0-9a-f]{64}$'),
    contract_id varchar(200) NOT NULL
        CHECK (length(trim(contract_id)) BETWEEN 1 AND 200),
    contract_version bigint NOT NULL CHECK (contract_version >= 1),
    left_binding_id varchar(200) NOT NULL,
    right_binding_id varchar(200) NOT NULL,
    normalization_fingerprint char(64) NOT NULL
        CHECK (normalization_fingerprint ~ '^[0-9a-f]{64}$'),
    policy_fingerprint char(64) NOT NULL
        CHECK (policy_fingerprint ~ '^[0-9a-f]{64}$'),
    left_row_count bigint NOT NULL CHECK (left_row_count >= 0),
    right_row_count bigint NOT NULL CHECK (right_row_count >= 0),
    left_null_count bigint NOT NULL CHECK (left_null_count >= 0),
    right_null_count bigint NOT NULL CHECK (right_null_count >= 0),
    left_invalid_count bigint NOT NULL CHECK (left_invalid_count >= 0),
    right_invalid_count bigint NOT NULL CHECK (right_invalid_count >= 0),
    left_distinct_count bigint NOT NULL CHECK (left_distinct_count >= 0),
    right_distinct_count bigint NOT NULL CHECK (right_distinct_count >= 0),
    matching_distinct_count bigint NOT NULL CHECK (matching_distinct_count >= 0),
    max_left_multiplicity bigint NOT NULL CHECK (max_left_multiplicity >= 0),
    max_right_multiplicity bigint NOT NULL CHECK (max_right_multiplicity >= 0),
    declared_relationship varchar(40) NOT NULL
        CHECK (
            declared_relationship IN (
                'none',
                'left_foreign_key_to_right',
                'right_foreign_key_to_left'
            )
        ),
    foreign_key_evidence boolean NOT NULL,
    observed_cardinality varchar(24) NOT NULL
        CHECK (
            observed_cardinality IN (
                'one_to_one',
                'one_to_many',
                'many_to_one',
                'many_to_many',
                'unknown'
            )
        ),
    profile_state varchar(24) NOT NULL
        CHECK (
            profile_state IN (
                'candidate',
                'approved',
                'revalidated',
                'rejected',
                'superseded'
            )
        ),
    approved_baseline_revision bigint
        CHECK (approved_baseline_revision >= 1),
    approval_id varchar(200)
        CHECK (
            approval_id IS NULL
            OR approval_id ~ '^[a-z][a-z0-9_-]{2,199}$'
        ),
    actor_id varchar(200)
        CHECK (
            actor_id IS NULL
            OR length(trim(actor_id)) BETWEEN 1 AND 200
        ),
    decided_at timestamptz,
    profile_fingerprint char(64) NOT NULL
        CHECK (profile_fingerprint ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, profile_id),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        profile_id
    ),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        registry_generation,
        contract_id,
        contract_version,
        approved_baseline_revision,
        left_binding_id,
        right_binding_id,
        profile_fingerprint
    ),
    CONSTRAINT semantic_join_profile_left_binding_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            registry_generation,
            registry_fingerprint,
            left_binding_id
        )
        REFERENCES schemabridge_control.semantic_resource_bindings (
            workspace_id,
            catalog_scope,
            registry_id,
            registry_generation,
            registry_fingerprint,
            binding_id
        ),
    CONSTRAINT semantic_join_profile_right_binding_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            registry_generation,
            registry_fingerprint,
            right_binding_id
        )
        REFERENCES schemabridge_control.semantic_resource_bindings (
            workspace_id,
            catalog_scope,
            registry_id,
            registry_generation,
            registry_fingerprint,
            binding_id
        ),
    CONSTRAINT semantic_join_profile_count_shape
        CHECK (
            left_null_count <= left_row_count
            AND right_null_count <= right_row_count
            AND left_invalid_count <= left_row_count
            AND right_invalid_count <= right_row_count
            AND left_distinct_count <= left_row_count
            AND right_distinct_count <= right_row_count
            AND matching_distinct_count <= left_distinct_count
            AND matching_distinct_count <= right_distinct_count
            AND foreign_key_evidence
                = (declared_relationship <> 'none')
        ),
    CONSTRAINT semantic_join_profile_state_shape
        CHECK (
            (
                profile_state = 'candidate'
                AND approved_baseline_revision IS NULL
                AND approval_id IS NULL
                AND actor_id IS NULL
                AND decided_at IS NULL
            )
            OR
            (
                profile_state IN ('approved', 'revalidated', 'superseded')
                AND approved_baseline_revision IS NOT NULL
                AND approval_id IS NOT NULL
                AND actor_id IS NOT NULL
                AND decided_at IS NOT NULL
            )
            OR
            (
                profile_state = 'rejected'
                AND approved_baseline_revision IS NULL
                AND approval_id IS NOT NULL
                AND actor_id IS NOT NULL
                AND decided_at IS NOT NULL
            )
        )
);

CREATE INDEX semantic_join_profiles_scope_idx
    ON schemabridge_control.semantic_join_profiles (
        workspace_id,
        catalog_scope,
        registry_id,
        registry_generation,
        contract_id,
        contract_version DESC,
        observed_at DESC
    );

CREATE INDEX semantic_join_profiles_gate_idx
    ON schemabridge_control.semantic_join_profiles (
        workspace_id,
        catalog_scope,
        registry_id,
        registry_generation,
        approved_baseline_revision,
        contract_id
    )
    WHERE profile_state IN ('approved', 'revalidated');

CREATE TABLE schemabridge_control.semantic_change_reports (
    report_id varchar(200) NOT NULL
        CHECK (report_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    catalog_scope varchar(120) NOT NULL
        CHECK (length(trim(catalog_scope)) BETWEEN 1 AND 120),
    registry_id varchar(80) NOT NULL
        CHECK (length(trim(registry_id)) BETWEEN 1 AND 80),
    registry_generation bigint NOT NULL CHECK (registry_generation >= 1),
    registry_version bigint NOT NULL CHECK (registry_version >= 1),
    registry_fingerprint char(64) NOT NULL
        CHECK (registry_fingerprint ~ '^[0-9a-f]{64}$'),
    pointer_transition_id varchar(200) NOT NULL
        CHECK (pointer_transition_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    pointer_fingerprint char(64) NOT NULL
        CHECK (pointer_fingerprint ~ '^[0-9a-f]{64}$'),
    context_fingerprint char(64) NOT NULL
        CHECK (context_fingerprint ~ '^[0-9a-f]{64}$'),
    observation_fingerprint char(64) NOT NULL
        CHECK (observation_fingerprint ~ '^[0-9a-f]{64}$'),
    expected_head_revision bigint NOT NULL CHECK (expected_head_revision >= 0),
    baseline_revision bigint NOT NULL CHECK (baseline_revision >= 0),
    baseline_fingerprint char(64)
        CHECK (
            baseline_fingerprint IS NULL
            OR baseline_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    report_kind varchar(24) NOT NULL
        CHECK (report_kind IN ('baseline_review', 'drift_review')),
    outcome varchar(24) NOT NULL
        CHECK (
            outcome IN (
                'current',
                'review_required',
                'blocked'
            )
        ),
    catalog_generation_vector_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(catalog_generation_vector_json) = 'object'
            AND jsonb_typeof(
                catalog_generation_vector_json -> 'observations'
            ) = 'array'
            AND jsonb_array_length(
                catalog_generation_vector_json -> 'observations'
            )
                BETWEEN 1 AND 2000
            AND octet_length(catalog_generation_vector_json::text) <= 2000000
        ),
    catalog_generation_vector_fingerprint char(64) NOT NULL
        CHECK (catalog_generation_vector_fingerprint ~ '^[0-9a-f]{64}$'),
    dependency_index_watermark bigint NOT NULL
        CHECK (dependency_index_watermark >= 0),
    dependency_index_fingerprint char(64) NOT NULL
        CHECK (dependency_index_fingerprint ~ '^[0-9a-f]{64}$'),
    dependency_index_complete boolean NOT NULL,
    governed_mapping_count integer NOT NULL
        CHECK (governed_mapping_count BETWEEN 0 AND 2000),
    governed_join_count integer NOT NULL
        CHECK (governed_join_count BETWEEN 0 AND 500),
    finding_count integer NOT NULL CHECK (finding_count BETWEEN 0 AND 2500),
    review_finding_count integer NOT NULL
        CHECK (review_finding_count BETWEEN 0 AND 2500),
    blocking_finding_count integer NOT NULL
        CHECK (blocking_finding_count BETWEEN 0 AND 2500),
    informational_finding_count integer NOT NULL
        CHECK (informational_finding_count BETWEEN 0 AND 2500),
    impact_count bigint NOT NULL CHECK (impact_count BETWEEN 0 AND 10000),
    finding_set_fingerprint char(64) NOT NULL
        CHECK (finding_set_fingerprint ~ '^[0-9a-f]{64}$'),
    impact_set_fingerprint char(64) NOT NULL
        CHECK (impact_set_fingerprint ~ '^[0-9a-f]{64}$'),
    report_fingerprint char(64) NOT NULL UNIQUE
        CHECK (report_fingerprint ~ '^[0-9a-f]{64}$'),
    context_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(context_json) = 'object'
            AND octet_length(context_json::text) <= 4000000
            AND context_json ?& ARRAY[
                'scope',
                'pointer_generation',
                'pointer_fingerprint',
                'pointer_transition_id',
                'registry_version',
                'registry_fingerprint',
                'mappings',
                'joins',
                'dependency_index',
                'fingerprint'
            ]
            AND NOT schemabridge_control.semantic_json_has_protected_keys(
                context_json
            )
        ),
    observation_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(observation_json) = 'object'
            AND octet_length(observation_json::text) <= 16000000
            AND observation_json ?& ARRAY[
                'context',
                'catalog_generations',
                'fields',
                'joins',
                'observed_at',
                'complete',
                'fingerprint'
            ]
            AND NOT schemabridge_control.semantic_json_has_protected_keys(
                observation_json
            )
        ),
    report_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(report_json) = 'object'
            AND octet_length(report_json::text) <= 16000000
            AND report_json ?& ARRAY[
                'id',
                'context',
                'observation_fingerprint',
                'catalog_generations',
                'baseline_revision',
                'baseline_fingerprint',
                'findings',
                'impacts',
                'status',
                'inspected_at',
                'fingerprint'
            ]
            AND NOT schemabridge_control.semantic_json_has_protected_keys(
                report_json
            )
        ),
    inspected_at timestamptz NOT NULL,
    retain_until timestamptz NOT NULL CHECK (retain_until >= inspected_at),
    PRIMARY KEY (workspace_id, report_id),
    UNIQUE (
        workspace_id,
        report_id,
        report_fingerprint
    ),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        report_id
    ),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        report_id,
        report_fingerprint
    ),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        report_id,
        report_fingerprint,
        impact_count,
        impact_set_fingerprint,
        dependency_index_watermark,
        dependency_index_fingerprint
    ),
    CONSTRAINT semantic_change_report_registry_transition_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            registry_generation,
            pointer_transition_id,
            registry_version,
            registry_fingerprint
        )
        REFERENCES schemabridge_control.registry_activation_transitions (
            workspace_id,
            catalog_scope,
            registry_id,
            generation,
            transition_id,
            target_registry_version,
            target_registry_fingerprint
        ),
    CONSTRAINT semantic_change_report_count_shape
        CHECK (
            finding_count = informational_finding_count
                + review_finding_count
                + blocking_finding_count
        ),
    CONSTRAINT semantic_change_report_baseline_shape
        CHECK (
            (
                baseline_revision = 0
                AND baseline_fingerprint IS NULL
            )
            OR
            (
                baseline_revision >= 1
                AND baseline_fingerprint IS NOT NULL
            )
        ),
    CONSTRAINT semantic_change_report_payload_identity_shape
        CHECK (
            context_json ->> 'fingerprint' = context_fingerprint
            AND observation_json ->> 'fingerprint' = observation_fingerprint
            AND report_json ->> 'id' = report_id
            AND report_json ->> 'fingerprint' = report_fingerprint
            AND report_json ->> 'observation_fingerprint'
                = observation_fingerprint
            AND report_json ->> 'status' = outcome
            AND catalog_generation_vector_json ->> 'fingerprint'
                = catalog_generation_vector_fingerprint
            AND context_json #>> '{scope,workspace_id}' = workspace_id
            AND context_json #>> '{scope,catalog_scope}' = catalog_scope
            AND context_json #>> '{scope,registry_id}' = registry_id
            AND (context_json ->> 'pointer_generation')::bigint
                = registry_generation
            AND (context_json ->> 'registry_version')::bigint
                = registry_version
            AND context_json ->> 'registry_fingerprint'
                = registry_fingerprint
            AND context_json ->> 'pointer_transition_id'
                = pointer_transition_id
            AND context_json ->> 'pointer_fingerprint'
                = pointer_fingerprint
            AND observation_json -> 'context' = context_json
            AND observation_json -> 'catalog_generations'
                = catalog_generation_vector_json
            AND report_json -> 'context' = context_json
            AND report_json -> 'catalog_generations'
                = catalog_generation_vector_json
            AND jsonb_typeof(report_json -> 'impacts') = 'object'
            AND report_json -> 'impacts' ?& ARRAY[
                'mapping_count',
                'join_count',
                'workflow_count',
                'recipe_count',
                'complete',
                'watermark',
                'dependency_index_fingerprint',
                'impact_set_fingerprint'
            ]
            AND (
                (report_json #>> '{impacts,mapping_count}')::integer
                + (report_json #>> '{impacts,join_count}')::integer
                + (report_json #>> '{impacts,workflow_count}')::integer
                + (report_json #>> '{impacts,recipe_count}')::integer
            ) = impact_count
            AND (report_json #>> '{impacts,complete}')::boolean
                = dependency_index_complete
            AND (report_json #>> '{impacts,watermark}')::bigint
                = dependency_index_watermark
            AND report_json #>> '{impacts,dependency_index_fingerprint}'
                = dependency_index_fingerprint
            AND report_json #>> '{impacts,impact_set_fingerprint}'
                = impact_set_fingerprint
            AND (
                (
                    baseline_revision = 0
                    AND report_json -> 'baseline_revision' = 'null'::jsonb
                    AND report_json -> 'baseline_fingerprint' = 'null'::jsonb
                )
                OR
                (
                    baseline_revision >= 1
                    AND (report_json ->> 'baseline_revision')::bigint
                        = baseline_revision
                    AND report_json ->> 'baseline_fingerprint'
                        = baseline_fingerprint
                )
            )
        ),
    CONSTRAINT semantic_change_report_outcome_shape
        CHECK (
            (
                outcome = 'current'
                AND review_finding_count = 0
                AND blocking_finding_count = 0
            )
            OR (
                outcome = 'review_required'
                AND review_finding_count > 0
                AND blocking_finding_count = 0
            )
            OR (
                outcome = 'blocked'
                AND blocking_finding_count > 0
            )
        ),
    CONSTRAINT semantic_change_report_completeness_shape
        CHECK (
            dependency_index_complete
            OR outcome = 'blocked'
        )
);

CREATE INDEX semantic_change_reports_scope_page_idx
    ON schemabridge_control.semantic_change_reports (
        workspace_id,
        catalog_scope,
        registry_id,
        inspected_at DESC,
        report_id
    );

CREATE INDEX semantic_change_reports_status_page_idx
    ON schemabridge_control.semantic_change_reports (
        workspace_id,
        outcome,
        inspected_at DESC,
        report_id
    );

CREATE INDEX semantic_change_reports_retention_idx
    ON schemabridge_control.semantic_change_reports (
        retain_until,
        workspace_id,
        report_id
    );

CREATE TABLE schemabridge_control.semantic_change_findings (
    finding_id varchar(200) NOT NULL
        CHECK (finding_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL,
    catalog_scope varchar(120) NOT NULL,
    registry_id varchar(80) NOT NULL,
    report_id varchar(200) NOT NULL,
    finding_sort_key varchar(256) NOT NULL
        CHECK (
            length(finding_sort_key) BETWEEN 1 AND 256
            AND octet_length(finding_sort_key) <= 256
        ),
    change_kind varchar(64) NOT NULL
        CHECK (
            change_kind IN (
                'baseline_required',
                'binding_missing',
                'binding_ambiguous',
                'asset_removed',
                'field_removed',
                'physical_type_changed',
                'nullability_changed',
                'key_status_changed',
                'field_definition_changed',
                'field_terms_changed',
                'asset_metadata_changed',
                'registry_changed',
                'join_cardinality_changed',
                'join_foreign_key_changed',
                'join_overlap_changed',
                'join_nulls_changed',
                'join_invalids_changed',
                'join_multiplicity_changed',
                'evidence_unavailable'
            )
        ),
    severity varchar(24) NOT NULL
        CHECK (severity IN ('informational', 'review_required', 'blocking')),
    binding_id varchar(200),
    mapping_decision_id varchar(200)
        CHECK (
            mapping_decision_id IS NULL
            OR length(trim(mapping_decision_id)) BETWEEN 1 AND 200
        ),
    affected_join_contracts_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(affected_join_contracts_json) = 'array'
            AND jsonb_array_length(affected_join_contracts_json)
                BETWEEN 0 AND 500
            AND octet_length(affected_join_contracts_json::text) <= 100000
        ),
    previous_evidence_fingerprint char(64)
        CHECK (
            previous_evidence_fingerprint IS NULL
            OR previous_evidence_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    current_evidence_fingerprint char(64)
        CHECK (
            current_evidence_fingerprint IS NULL
            OR current_evidence_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    risk_codes varchar(200)[] NOT NULL
        CHECK (
            cardinality(risk_codes) BETWEEN 1 AND 20
            AND array_position(risk_codes, NULL) IS NULL
            AND octet_length(array_to_string(risk_codes, ',')) <= 4000
        ),
    finding_fingerprint char(64) NOT NULL
        CHECK (finding_fingerprint ~ '^[0-9a-f]{64}$'),
    finding_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(finding_json) = 'object'
            AND octet_length(finding_json::text) <= 100000
            AND finding_json ?& ARRAY[
                'id',
                'kind',
                'severity',
                'mapping',
                'join',
                'previous_fingerprint',
                'current_fingerprint',
                'risks',
                'fingerprint'
            ]
            AND NOT schemabridge_control.semantic_json_has_protected_keys(
                finding_json
            )
        ),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, report_id, finding_id),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        report_id,
        finding_fingerprint
    ),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        report_id,
        finding_id
    ),
    CONSTRAINT semantic_change_finding_report_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            report_id
        )
        REFERENCES schemabridge_control.semantic_change_reports (
            workspace_id,
            catalog_scope,
            registry_id,
            report_id
        ),
    CONSTRAINT semantic_change_finding_binding_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            binding_id
        )
        REFERENCES schemabridge_control.semantic_resource_bindings (
            workspace_id,
            catalog_scope,
            registry_id,
            binding_id
        ),
    CONSTRAINT semantic_change_finding_evidence_shape
        CHECK (
            previous_evidence_fingerprint IS NOT NULL
            OR current_evidence_fingerprint IS NOT NULL
            OR change_kind IN (
                'baseline_required',
                'binding_missing',
                'binding_ambiguous',
                'evidence_unavailable'
            )
        ),
    CONSTRAINT semantic_change_finding_payload_identity_shape
        CHECK (
            finding_json ->> 'id' = finding_id
            AND finding_json ->> 'kind' = change_kind
            AND finding_json ->> 'severity' = severity
            AND finding_json ->> 'fingerprint' = finding_fingerprint
        ),
    CONSTRAINT semantic_change_finding_severity_shape
        CHECK (
            severity = 'informational'
            OR
            (
                change_kind IN (
                    'baseline_required',
                    'field_definition_changed',
                    'field_terms_changed',
                    'asset_metadata_changed'
                )
                AND severity = 'review_required'
            )
            OR
            (
                change_kind NOT IN (
                    'baseline_required',
                    'field_definition_changed',
                    'field_terms_changed',
                    'asset_metadata_changed'
                )
                AND severity = 'blocking'
            )
        )
);

CREATE INDEX semantic_change_findings_page_idx
    ON schemabridge_control.semantic_change_findings (
        workspace_id,
        report_id,
        severity,
        finding_sort_key,
        finding_id
    );

CREATE INDEX semantic_change_findings_binding_idx
    ON schemabridge_control.semantic_change_findings (
        workspace_id,
        catalog_scope,
        registry_id,
        binding_id,
        created_at DESC,
        finding_id
    );

CREATE TABLE schemabridge_control.semantic_change_impacts (
    impact_id varchar(200) NOT NULL
        CHECK (impact_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL,
    catalog_scope varchar(120) NOT NULL,
    registry_id varchar(80) NOT NULL,
    report_id varchar(200) NOT NULL,
    impact_sort_key varchar(256) NOT NULL
        CHECK (
            length(impact_sort_key) BETWEEN 1 AND 256
            AND octet_length(impact_sort_key) <= 256
        ),
    artifact_kind varchar(24) NOT NULL
        CHECK (
            artifact_kind IN (
                'mapping',
                'join',
                'workflow',
                'recipe'
            )
        ),
    artifact_id varchar(500) NOT NULL
        CHECK (
            length(artifact_id) BETWEEN 1 AND 500
            AND octet_length(artifact_id) <= 2000
        ),
    artifact_version bigint CHECK (artifact_version >= 1),
    artifact_fingerprint char(64)
        CHECK (
            artifact_fingerprint IS NULL
            OR artifact_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    finding_ids_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(finding_ids_json) = 'array'
            AND jsonb_array_length(finding_ids_json) BETWEEN 1 AND 2000
            AND octet_length(finding_ids_json::text) <= 500000
        ),
    impact_state varchar(24) NOT NULL
        CHECK (
            impact_state IN ('informational', 'review_required', 'blocked')
        ),
    impact_fingerprint char(64) NOT NULL
        CHECK (impact_fingerprint ~ '^[0-9a-f]{64}$'),
    impact_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(impact_json) = 'object'
            AND octet_length(impact_json::text) <= 600000
            AND impact_json ?& ARRAY[
                'kind',
                'artifact_id',
                'artifact_version',
                'finding_ids',
                'fingerprint'
            ]
            AND NOT schemabridge_control.semantic_json_has_protected_keys(
                impact_json
            )
        ),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, impact_id),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        report_id,
        impact_fingerprint
    ),
    UNIQUE NULLS NOT DISTINCT (
        workspace_id,
        report_id,
        artifact_kind,
        artifact_id,
        artifact_version
    ),
    CONSTRAINT semantic_change_impact_report_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            report_id
        )
        REFERENCES schemabridge_control.semantic_change_reports (
            workspace_id,
            catalog_scope,
            registry_id,
            report_id
        ),
    CONSTRAINT semantic_change_impact_payload_identity_shape
        CHECK (
            impact_json ->> 'kind' = artifact_kind
            AND impact_json ->> 'artifact_id' = artifact_id
            AND impact_json ->> 'fingerprint' = impact_fingerprint
            AND impact_json -> 'finding_ids' = finding_ids_json
            AND (
                (
                    artifact_version IS NULL
                    AND impact_json -> 'artifact_version' = 'null'::jsonb
                )
                OR
                (
                    artifact_version IS NOT NULL
                    AND (impact_json ->> 'artifact_version')::bigint
                        = artifact_version
                )
            )
        )
);

CREATE INDEX semantic_change_impacts_page_idx
    ON schemabridge_control.semantic_change_impacts (
        workspace_id,
        report_id,
        artifact_kind,
        impact_sort_key,
        impact_id
    );

CREATE INDEX semantic_change_impacts_artifact_idx
    ON schemabridge_control.semantic_change_impacts (
        workspace_id,
        artifact_kind,
        artifact_id,
        artifact_version DESC,
        report_id
    );

CREATE TABLE schemabridge_control.semantic_change_resolutions (
    resolution_id varchar(200) NOT NULL
        CHECK (resolution_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL,
    catalog_scope varchar(120) NOT NULL,
    registry_id varchar(80) NOT NULL,
    report_id varchar(200) NOT NULL,
    report_fingerprint char(64) NOT NULL
        CHECK (report_fingerprint ~ '^[0-9a-f]{64}$'),
    expected_head_revision bigint NOT NULL CHECK (expected_head_revision >= 0),
    expected_baseline_revision bigint NOT NULL
        CHECK (expected_baseline_revision >= 0),
    resulting_head_revision bigint NOT NULL CHECK (resulting_head_revision >= 1),
    resulting_baseline_revision bigint
        CHECK (resulting_baseline_revision >= 1),
    decision_action varchar(40) NOT NULL
        CHECK (
            decision_action IN (
                'establish_baseline',
                'revalidate_compatible_change',
                'reject_change'
            )
        ),
    resulting_state varchar(24) NOT NULL
        CHECK (resulting_state IN ('revalidated', 'rejected')),
    proposal_fingerprint char(64) NOT NULL
        CHECK (proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    approval_fingerprint char(64) NOT NULL
        CHECK (approval_fingerprint ~ '^[0-9a-f]{64}$'),
    approval_id varchar(200) NOT NULL UNIQUE
        CHECK (approval_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    confirmation varchar(64) NOT NULL
        CHECK (
            confirmation IN (
                'ESTABLISH SEMANTIC EVIDENCE BASELINE',
                'REVALIDATE COMPATIBLE SEMANTIC CHANGE',
                'REJECT SEMANTIC CHANGE'
            )
        ),
    actor_id varchar(200) NOT NULL
        CHECK (length(trim(actor_id)) BETWEEN 1 AND 200),
    approved_at timestamptz NOT NULL,
    impact_count bigint NOT NULL CHECK (impact_count BETWEEN 0 AND 10000),
    impact_set_fingerprint char(64) NOT NULL
        CHECK (impact_set_fingerprint ~ '^[0-9a-f]{64}$'),
    dependency_index_watermark bigint NOT NULL
        CHECK (dependency_index_watermark >= 0),
    dependency_index_fingerprint char(64) NOT NULL
        CHECK (dependency_index_fingerprint ~ '^[0-9a-f]{64}$'),
    approved_binding_set_fingerprint char(64)
        CHECK (
            approved_binding_set_fingerprint IS NULL
            OR approved_binding_set_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    control_audit_event_id varchar(200) NOT NULL
        CHECK (control_audit_event_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    resolution_fingerprint char(64) NOT NULL UNIQUE
        CHECK (resolution_fingerprint ~ '^[0-9a-f]{64}$'),
    committed_at timestamptz NOT NULL CHECK (committed_at >= approved_at),
    PRIMARY KEY (workspace_id, resolution_id),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        report_id,
        resolution_id
    ),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        resolution_id
    ),
    CONSTRAINT semantic_change_resolution_report_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            report_id,
            report_fingerprint,
            impact_count,
            impact_set_fingerprint,
            dependency_index_watermark,
            dependency_index_fingerprint
        )
        REFERENCES schemabridge_control.semantic_change_reports (
            workspace_id,
            catalog_scope,
            registry_id,
            report_id,
            report_fingerprint,
            impact_count,
            impact_set_fingerprint,
            dependency_index_watermark,
            dependency_index_fingerprint
        ),
    CONSTRAINT semantic_change_resolution_audit_fk
        FOREIGN KEY (workspace_id, control_audit_event_id)
        REFERENCES schemabridge_control.control_audit_events (
            workspace_id,
            event_id
        ),
    CONSTRAINT semantic_change_resolution_cas_shape
        CHECK (resulting_head_revision = expected_head_revision + 1),
    CONSTRAINT semantic_change_resolution_confirmation_shape
        CHECK (
            (
                decision_action = 'establish_baseline'
                AND confirmation = 'ESTABLISH SEMANTIC EVIDENCE BASELINE'
            )
            OR
            (
                decision_action = 'revalidate_compatible_change'
                AND confirmation = 'REVALIDATE COMPATIBLE SEMANTIC CHANGE'
            )
            OR
            (
                decision_action = 'reject_change'
                AND confirmation = 'REJECT SEMANTIC CHANGE'
            )
        ),
    CONSTRAINT semantic_change_resolution_result_shape
        CHECK (
            (
                decision_action = 'establish_baseline'
                AND resulting_state = 'revalidated'
                AND resulting_baseline_revision
                    = resulting_head_revision
                AND approved_binding_set_fingerprint IS NOT NULL
            )
            OR
            (
                decision_action = 'revalidate_compatible_change'
                AND resulting_state = 'revalidated'
                AND resulting_baseline_revision
                    = resulting_head_revision
                AND approved_binding_set_fingerprint IS NOT NULL
            )
            OR
            (
                decision_action = 'reject_change'
                AND resulting_state = 'rejected'
                AND resulting_baseline_revision IS NULL
                AND approved_binding_set_fingerprint IS NULL
            )
        )
);

CREATE INDEX semantic_change_resolutions_scope_idx
    ON schemabridge_control.semantic_change_resolutions (
        workspace_id,
        catalog_scope,
        registry_id,
        committed_at DESC,
        resolution_id
    );

CREATE INDEX semantic_change_resolutions_report_state_idx
    ON schemabridge_control.semantic_change_resolutions (
        workspace_id,
        catalog_scope,
        registry_id,
        report_id,
        resulting_head_revision DESC,
        resolution_id DESC
    );

CREATE TABLE schemabridge_control.semantic_artifact_dependencies (
    dependency_id varchar(200) NOT NULL
        CHECK (dependency_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    workspace_id varchar(200) NOT NULL,
    catalog_scope varchar(120) NOT NULL,
    registry_id varchar(80) NOT NULL,
    registry_generation bigint NOT NULL CHECK (registry_generation >= 1),
    registry_fingerprint char(64) NOT NULL
        CHECK (registry_fingerprint ~ '^[0-9a-f]{64}$'),
    artifact_kind varchar(24) NOT NULL
        CHECK (artifact_kind IN ('workflow', 'query_recipe')),
    artifact_id varchar(500) NOT NULL
        CHECK (
            length(artifact_id) BETWEEN 1 AND 500
            AND octet_length(artifact_id) <= 2000
        ),
    artifact_version bigint NOT NULL CHECK (artifact_version >= 1),
    artifact_fingerprint char(64) NOT NULL
        CHECK (artifact_fingerprint ~ '^[0-9a-f]{64}$'),
    dependency_kind varchar(24) NOT NULL
        CHECK (dependency_kind IN ('mapping', 'join_contract')),
    mapping_decision_id varchar(200),
    join_contract_id varchar(200),
    join_contract_version bigint CHECK (join_contract_version >= 1),
    binding_id varchar(200),
    connection_id varchar(200)
        CHECK (connection_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    asset_key char(64) CHECK (asset_key ~ '^[0-9a-f]{64}$'),
    field_key char(64) CHECK (field_key ~ '^[0-9a-f]{64}$'),
    dependency_index_watermark bigint NOT NULL
        CHECK (dependency_index_watermark >= 1),
    dependency_fingerprint char(64) NOT NULL
        CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    indexed_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, dependency_id),
    UNIQUE (
        workspace_id,
        catalog_scope,
        registry_id,
        dependency_fingerprint
    ),
    CONSTRAINT semantic_artifact_dependency_binding_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            registry_generation,
            registry_fingerprint,
            binding_id,
            connection_id,
            asset_key,
            field_key
        )
        REFERENCES schemabridge_control.semantic_resource_bindings (
            workspace_id,
            catalog_scope,
            registry_id,
            registry_generation,
            registry_fingerprint,
            binding_id,
            connection_id,
            asset_key,
            field_key
        ),
    CONSTRAINT semantic_artifact_dependency_shape
        CHECK (
            (
                binding_id IS NULL
                AND connection_id IS NULL
                AND asset_key IS NULL
                AND field_key IS NULL
            )
            OR
            (
                binding_id IS NOT NULL
                AND connection_id IS NOT NULL
                AND asset_key IS NOT NULL
                AND field_key IS NOT NULL
            )
        ),
    CONSTRAINT semantic_artifact_dependency_target_shape
        CHECK (
            (
                dependency_kind = 'mapping'
                AND mapping_decision_id IS NOT NULL
                AND join_contract_id IS NULL
                AND join_contract_version IS NULL
            )
            OR
            (
                dependency_kind = 'join_contract'
                AND mapping_decision_id IS NULL
                AND join_contract_id IS NOT NULL
                AND join_contract_version IS NOT NULL
            )
        )
);

CREATE TABLE schemabridge_control.semantic_dependency_index_states (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    catalog_scope varchar(120) NOT NULL
        CHECK (length(trim(catalog_scope)) BETWEEN 1 AND 120),
    registry_id varchar(80) NOT NULL
        CHECK (length(trim(registry_id)) BETWEEN 1 AND 80),
    registry_generation bigint NOT NULL CHECK (registry_generation >= 1),
    registry_version bigint NOT NULL CHECK (registry_version >= 1),
    registry_fingerprint char(64) NOT NULL
        CHECK (registry_fingerprint ~ '^[0-9a-f]{64}$'),
    pointer_transition_id varchar(200) NOT NULL
        CHECK (pointer_transition_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    watermark bigint NOT NULL CHECK (watermark >= 0),
    index_fingerprint char(64) NOT NULL
        CHECK (index_fingerprint ~ '^[0-9a-f]{64}$'),
    complete boolean NOT NULL,
    indexed_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, catalog_scope, registry_id),
    CONSTRAINT semantic_dependency_index_registry_transition_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            registry_generation,
            pointer_transition_id,
            registry_version,
            registry_fingerprint
        )
        REFERENCES schemabridge_control.registry_activation_transitions (
            workspace_id,
            catalog_scope,
            registry_id,
            generation,
            transition_id,
            target_registry_version,
            target_registry_fingerprint
        )
);

CREATE INDEX semantic_dependency_index_states_watermark_idx
    ON schemabridge_control.semantic_dependency_index_states (
        workspace_id,
        watermark DESC,
        catalog_scope,
        registry_id
    );

CREATE INDEX semantic_artifact_dependencies_mapping_idx
    ON schemabridge_control.semantic_artifact_dependencies (
        workspace_id,
        catalog_scope,
        registry_id,
        registry_generation,
        mapping_decision_id,
        dependency_index_watermark,
        dependency_id
    )
    WHERE dependency_kind = 'mapping';

CREATE INDEX semantic_artifact_dependencies_join_idx
    ON schemabridge_control.semantic_artifact_dependencies (
        workspace_id,
        catalog_scope,
        registry_id,
        registry_generation,
        join_contract_id,
        join_contract_version,
        dependency_index_watermark,
        dependency_id
    )
    WHERE dependency_kind = 'join_contract';

CREATE INDEX semantic_artifact_dependencies_artifact_idx
    ON schemabridge_control.semantic_artifact_dependencies (
        workspace_id,
        artifact_kind,
        artifact_id,
        artifact_version DESC,
        dependency_id
    );

CREATE UNIQUE INDEX semantic_artifact_dependencies_identity_idx
    ON schemabridge_control.semantic_artifact_dependencies (
        workspace_id,
        catalog_scope,
        registry_id,
        registry_generation,
        artifact_kind,
        artifact_id,
        artifact_version,
        dependency_kind,
        coalesce(mapping_decision_id, ''),
        coalesce(join_contract_id, ''),
        coalesce(join_contract_version, 0),
        dependency_index_watermark
    );

CREATE TABLE schemabridge_control.semantic_change_heads (
    workspace_id varchar(200) NOT NULL
        CHECK (length(trim(workspace_id)) BETWEEN 1 AND 200),
    catalog_scope varchar(120) NOT NULL
        CHECK (length(trim(catalog_scope)) BETWEEN 1 AND 120),
    registry_id varchar(80) NOT NULL
        CHECK (length(trim(registry_id)) BETWEEN 1 AND 80),
    head_revision bigint NOT NULL CHECK (head_revision >= 1),
    registry_generation bigint NOT NULL CHECK (registry_generation >= 1),
    registry_version bigint NOT NULL CHECK (registry_version >= 1),
    registry_fingerprint char(64) NOT NULL
        CHECK (registry_fingerprint ~ '^[0-9a-f]{64}$'),
    pointer_transition_id varchar(200) NOT NULL
        CHECK (pointer_transition_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    baseline_revision bigint NOT NULL CHECK (baseline_revision >= 0),
    baseline_report_id varchar(200),
    baseline_fingerprint char(64)
        CHECK (
            baseline_fingerprint IS NULL
            OR baseline_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    current_report_id varchar(200) NOT NULL,
    current_report_fingerprint char(64) NOT NULL
        CHECK (current_report_fingerprint ~ '^[0-9a-f]{64}$'),
    state varchar(24) NOT NULL
        CHECK (
            state IN (
                'current',
                'review_required',
                'blocked',
                'revalidated',
                'rejected',
                'superseded',
                'unavailable'
            )
        ),
    catalog_generation_vector_fingerprint char(64) NOT NULL
        CHECK (catalog_generation_vector_fingerprint ~ '^[0-9a-f]{64}$'),
    dependency_index_watermark bigint NOT NULL
        CHECK (dependency_index_watermark >= 0),
    dependency_index_fingerprint char(64) NOT NULL
        CHECK (dependency_index_fingerprint ~ '^[0-9a-f]{64}$'),
    dependency_index_complete boolean NOT NULL,
    last_resolution_id varchar(200),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, catalog_scope, registry_id),
    CONSTRAINT semantic_change_head_registry_transition_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            registry_generation,
            pointer_transition_id,
            registry_version,
            registry_fingerprint
        )
        REFERENCES schemabridge_control.registry_activation_transitions (
            workspace_id,
            catalog_scope,
            registry_id,
            generation,
            transition_id,
            target_registry_version,
            target_registry_fingerprint
        ),
    CONSTRAINT semantic_change_head_current_report_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            current_report_id,
            current_report_fingerprint
        )
        REFERENCES schemabridge_control.semantic_change_reports (
            workspace_id,
            catalog_scope,
            registry_id,
            report_id,
            report_fingerprint
        ),
    CONSTRAINT semantic_change_head_baseline_report_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            baseline_report_id
        )
        REFERENCES schemabridge_control.semantic_change_reports (
            workspace_id,
            catalog_scope,
            registry_id,
            report_id
        ),
    CONSTRAINT semantic_change_head_resolution_fk
        FOREIGN KEY (
            workspace_id,
            catalog_scope,
            registry_id,
            last_resolution_id
        )
        REFERENCES schemabridge_control.semantic_change_resolutions (
            workspace_id,
            catalog_scope,
            registry_id,
            resolution_id
        ),
    CONSTRAINT semantic_change_head_baseline_shape
        CHECK (
            (
                baseline_revision = 0
                AND baseline_report_id IS NULL
                AND baseline_fingerprint IS NULL
            )
            OR
            (
                baseline_revision >= 1
                AND baseline_report_id IS NOT NULL
                AND baseline_fingerprint IS NOT NULL
            )
        ),
    CONSTRAINT semantic_change_head_resolution_shape
        CHECK (
            state NOT IN ('current', 'revalidated', 'rejected')
            OR last_resolution_id IS NOT NULL
        ),
    CONSTRAINT semantic_change_head_completeness_shape
        CHECK (
            dependency_index_complete
            OR state IN ('review_required', 'blocked', 'unavailable')
        )
);

CREATE INDEX semantic_change_heads_state_idx
    ON schemabridge_control.semantic_change_heads (
        workspace_id,
        state,
        updated_at DESC,
        catalog_scope,
        registry_id
    );

CREATE TABLE schemabridge_control.semantic_change_scan_requests (
    scan_id varchar(80) PRIMARY KEY
        CHECK (scan_id ~ '^scan_[0-9a-f]{64}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (
            length(trim(workspace_id)) BETWEEN 1 AND 200
            AND octet_length(workspace_id) <= 200
        ),
    source_kind varchar(32) NOT NULL
        CHECK (source_kind IN ('catalog_generation', 'registry_pointer')),
    source_event_key varchar(500) NOT NULL
        CHECK (
            length(source_event_key) BETWEEN 1 AND 500
            AND octet_length(source_event_key) <= 2000
        ),
    source_fingerprint char(64) NOT NULL
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
    catalog_scope varchar(120),
    registry_id varchar(80),
    registry_generation bigint CHECK (registry_generation >= 1),
    connection_id varchar(200)
        CHECK (
            connection_id IS NULL
            OR connection_id ~ '^[a-z][a-z0-9_-]{2,199}$'
        ),
    base_catalog_generation bigint CHECK (base_catalog_generation > 0),
    observed_catalog_generation bigint
        CHECK (observed_catalog_generation > 0),
    status varchar(16) NOT NULL
        CHECK (
            status IN (
                'requested',
                'leased',
                'retry_wait',
                'completed',
                'failed',
                'superseded'
            )
        ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 100),
    max_attempts integer NOT NULL DEFAULT 5
        CHECK (max_attempts BETWEEN 1 AND 100),
    available_at timestamptz NOT NULL,
    lease_owner_id varchar(200)
        CHECK (
            lease_owner_id IS NULL
            OR length(trim(lease_owner_id)) BETWEEN 1 AND 200
        ),
    lease_capability_digest char(64)
        CHECK (
            lease_capability_digest IS NULL
            OR lease_capability_digest ~ '^[0-9a-f]{64}$'
        ),
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_acquired_at timestamptz,
    lease_heartbeat_at timestamptz,
    lease_expires_at timestamptz,
    last_reason_code varchar(64)
        CHECK (
            last_reason_code IS NULL
            OR last_reason_code ~ '^[a-z][a-z0-9_]{1,63}$'
        ),
    requested_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    completed_report_id varchar(200),
    completed_report_fingerprint char(64)
        CHECK (
            completed_report_fingerprint IS NULL
            OR completed_report_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    superseded_by_scan_id varchar(80)
        CHECK (
            superseded_by_scan_id IS NULL
            OR superseded_by_scan_id ~ '^scan_[0-9a-f]{64}$'
        ),
    superseded_by_scan_fingerprint char(64)
        CHECK (
            superseded_by_scan_fingerprint IS NULL
            OR superseded_by_scan_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    retain_until timestamptz,
    UNIQUE (workspace_id, scan_id),
    UNIQUE (workspace_id, source_kind, source_event_key),
    UNIQUE (workspace_id, scan_id, source_fingerprint),
    CHECK (updated_at >= requested_at),
    CHECK (attempts <= max_attempts),
    CHECK (
        superseded_by_scan_id IS NULL
        OR superseded_by_scan_id <> scan_id
    ),
    CONSTRAINT semantic_change_scan_completed_report_fk
        FOREIGN KEY (
            workspace_id,
            completed_report_id,
            completed_report_fingerprint
        )
        REFERENCES schemabridge_control.semantic_change_reports (
            workspace_id,
            report_id,
            report_fingerprint
        ),
    CONSTRAINT semantic_change_scan_superseded_by_fk
        FOREIGN KEY (
            workspace_id,
            superseded_by_scan_id,
            superseded_by_scan_fingerprint
        )
        REFERENCES schemabridge_control.semantic_change_scan_requests (
            workspace_id,
            scan_id,
            source_fingerprint
        ),
    CONSTRAINT semantic_change_scan_source_shape
        CHECK (
            (
                source_kind = 'catalog_generation'
                AND catalog_scope IS NOT NULL
                AND length(trim(catalog_scope)) BETWEEN 1 AND 120
                AND registry_id IS NOT NULL
                AND length(trim(registry_id)) BETWEEN 1 AND 80
                AND registry_generation IS NULL
                AND connection_id IS NOT NULL
                AND observed_catalog_generation IS NOT NULL
                AND (
                    base_catalog_generation IS NULL
                    OR observed_catalog_generation > base_catalog_generation
                )
            )
            OR
            (
                source_kind = 'registry_pointer'
                AND catalog_scope IS NOT NULL
                AND length(trim(catalog_scope)) BETWEEN 1 AND 120
                AND registry_id IS NOT NULL
                AND length(trim(registry_id)) BETWEEN 1 AND 80
                AND registry_generation IS NOT NULL
                AND connection_id IS NULL
                AND base_catalog_generation IS NULL
                AND observed_catalog_generation IS NULL
            )
        ),
    CONSTRAINT semantic_change_scan_lifecycle_shape
        CHECK (
            (
                status IN ('requested', 'retry_wait')
                AND lease_owner_id IS NULL
                AND lease_capability_digest IS NULL
                AND lease_acquired_at IS NULL
                AND lease_heartbeat_at IS NULL
                AND lease_expires_at IS NULL
                AND completed_at IS NULL
                AND completed_report_id IS NULL
                AND completed_report_fingerprint IS NULL
                AND superseded_by_scan_id IS NULL
                AND superseded_by_scan_fingerprint IS NULL
                AND retain_until IS NULL
            )
            OR
            (
                status = 'leased'
                AND lease_owner_id IS NOT NULL
                AND lease_capability_digest IS NOT NULL
                AND fencing_token > 0
                AND lease_acquired_at IS NOT NULL
                AND lease_heartbeat_at IS NOT NULL
                AND lease_heartbeat_at >= lease_acquired_at
                AND lease_expires_at IS NOT NULL
                AND lease_expires_at > lease_heartbeat_at
                AND completed_at IS NULL
                AND completed_report_id IS NULL
                AND completed_report_fingerprint IS NULL
                AND superseded_by_scan_id IS NULL
                AND superseded_by_scan_fingerprint IS NULL
                AND retain_until IS NULL
            )
            OR
            (
                status IN ('completed', 'failed', 'superseded')
                AND lease_owner_id IS NULL
                AND lease_capability_digest IS NULL
                AND lease_acquired_at IS NULL
                AND lease_heartbeat_at IS NULL
                AND lease_expires_at IS NULL
                AND completed_at IS NOT NULL
                AND retain_until IS NOT NULL
                AND retain_until >= completed_at
                AND (
                    (
                        status = 'completed'
                        AND completed_report_id IS NOT NULL
                        AND completed_report_fingerprint IS NOT NULL
                        AND superseded_by_scan_id IS NULL
                        AND superseded_by_scan_fingerprint IS NULL
                    )
                    OR
                    (
                        status = 'failed'
                        AND completed_report_id IS NULL
                        AND completed_report_fingerprint IS NULL
                        AND superseded_by_scan_id IS NULL
                        AND superseded_by_scan_fingerprint IS NULL
                    )
                    OR
                    (
                        status = 'superseded'
                        AND completed_report_id IS NULL
                        AND completed_report_fingerprint IS NULL
                        AND superseded_by_scan_id IS NOT NULL
                        AND superseded_by_scan_fingerprint IS NOT NULL
                    )
                )
            )
        )
);

CREATE INDEX semantic_change_scan_claim_idx
    ON schemabridge_control.semantic_change_scan_requests (
        available_at,
        requested_at,
        workspace_id,
        scan_id
    )
    WHERE status IN ('requested', 'retry_wait');

CREATE INDEX semantic_change_scan_expired_lease_idx
    ON schemabridge_control.semantic_change_scan_requests (
        lease_expires_at,
        workspace_id,
        scan_id
    )
    WHERE status = 'leased';

CREATE INDEX semantic_change_scan_scope_idx
    ON schemabridge_control.semantic_change_scan_requests (
        workspace_id,
        catalog_scope,
        registry_id,
        requested_at DESC,
        scan_id
    );

CREATE TABLE schemabridge_control.semantic_join_profile_jobs (
    job_id varchar(80) PRIMARY KEY
        CHECK (job_id ~ '^profile_job_[0-9a-f]{64}$'),
    workspace_id varchar(200) NOT NULL
        CHECK (
            length(trim(workspace_id)) BETWEEN 1 AND 200
            AND octet_length(workspace_id) <= 200
        ),
    scan_id varchar(80) NOT NULL
        CHECK (scan_id ~ '^scan_[0-9a-f]{64}$'),
    connection_id varchar(200) NOT NULL
        CHECK (connection_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    proposal_fingerprint char(64) NOT NULL
        CHECK (proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    proposal_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(proposal_json) = 'object'
            AND octet_length(proposal_json::text) <= 65536
            AND proposal_json ?& ARRAY[
                'id',
                'left_key',
                'right_key',
                'default_join_type'
            ]
            AND (
                proposal_json - ARRAY[
                    'id',
                    'left_key',
                    'right_key',
                    'default_join_type'
                ]
            ) = '{}'::jsonb
            AND proposal_json ->> 'id'
                ~ '^[A-Za-z_][A-Za-z0-9_]*$'
            AND NOT jsonb_path_exists(
                proposal_json,
                '$.**.operation ? (@ == "map_values")'
            )
            AND NOT schemabridge_control.semantic_json_has_protected_keys(
                proposal_json
            )
        ),
    status varchar(16) NOT NULL
        CHECK (
            status IN (
                'requested',
                'leased',
                'retry_wait',
                'completed',
                'failed'
            )
        ),
    attempt_count integer NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND 100),
    max_attempts integer NOT NULL DEFAULT 5
        CHECK (max_attempts BETWEEN 1 AND 100),
    available_at timestamptz NOT NULL,
    lease_owner_id varchar(200)
        CHECK (
            lease_owner_id IS NULL
            OR lease_owner_id ~ '^[a-z][a-z0-9_-]{2,199}$'
        ),
    lease_capability_digest char(64)
        CHECK (
            lease_capability_digest IS NULL
            OR lease_capability_digest ~ '^[0-9a-f]{64}$'
        ),
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_acquired_at timestamptz,
    lease_heartbeat_at timestamptz,
    lease_expires_at timestamptz,
    failure_code varchar(64)
        CHECK (
            failure_code IS NULL
            OR failure_code IN (
                'source_unavailable',
                'source_timeout',
                'lease_expired',
                'shutdown_requested',
                'source_connection_mismatch',
                'proposal_invalid',
                'evidence_invalid',
                'unexpected_worker_failure'
            )
        ),
    result_profile_json jsonb
        CHECK (
            result_profile_json IS NULL
            OR (
                jsonb_typeof(result_profile_json) = 'object'
                AND octet_length(result_profile_json::text) <= 16384
                AND result_profile_json ?& ARRAY[
                    'left_row_count',
                    'right_row_count',
                    'left_null_count',
                    'right_null_count',
                    'left_invalid_count',
                    'right_invalid_count',
                    'left_distinct_valid',
                    'right_distinct_valid',
                    'matching_distinct_keys',
                    'left_max_multiplicity',
                    'right_max_multiplicity',
                    'declared_relationship',
                    'reader_user',
                    'transaction_read_only',
                    'statement_timeout_ms'
                ]
                AND (
                    result_profile_json - ARRAY[
                        'left_row_count',
                        'right_row_count',
                        'left_null_count',
                        'right_null_count',
                        'left_invalid_count',
                        'right_invalid_count',
                        'left_distinct_valid',
                        'right_distinct_valid',
                        'matching_distinct_keys',
                        'left_max_multiplicity',
                        'right_max_multiplicity',
                        'declared_relationship',
                        'reader_user',
                        'transaction_read_only',
                        'statement_timeout_ms'
                    ]
                ) = '{}'::jsonb
                AND NOT schemabridge_control.semantic_json_has_protected_keys(
                    result_profile_json
                )
            )
        ),
    result_profile_fingerprint char(64)
        CHECK (
            result_profile_fingerprint IS NULL
            OR result_profile_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    requested_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    retain_until timestamptz,
    UNIQUE (workspace_id, scan_id, proposal_fingerprint),
    CONSTRAINT semantic_join_profile_job_scan_fk
        FOREIGN KEY (workspace_id, scan_id)
        REFERENCES schemabridge_control.semantic_change_scan_requests (
            workspace_id,
            scan_id
        ),
    CONSTRAINT semantic_join_profile_job_identity_shape
        CHECK (
            job_id = (
                'profile_job_'
                || encode(
                    sha256(
                        convert_to(
                            concat_ws(
                                '|',
                                'semantic_join_profile_job_v1',
                                workspace_id,
                                scan_id,
                                proposal_fingerprint
                            ),
                            'UTF8'
                        )
                    ),
                    'hex'
                )
            )
        ),
    CONSTRAINT semantic_join_profile_job_time_shape
        CHECK (
            updated_at >= requested_at
            AND available_at >= requested_at
            AND attempt_count <= max_attempts
            AND fencing_token = attempt_count
        ),
    CONSTRAINT semantic_join_profile_job_lifecycle_shape
        CHECK (
            (
                status = 'requested'
                AND attempt_count = 0
                AND lease_owner_id IS NULL
                AND lease_capability_digest IS NULL
                AND lease_acquired_at IS NULL
                AND lease_heartbeat_at IS NULL
                AND lease_expires_at IS NULL
                AND failure_code IS NULL
                AND result_profile_json IS NULL
                AND result_profile_fingerprint IS NULL
                AND completed_at IS NULL
                AND retain_until IS NULL
            )
            OR
            (
                status = 'leased'
                AND attempt_count >= 1
                AND lease_owner_id IS NOT NULL
                AND lease_capability_digest IS NOT NULL
                AND lease_acquired_at IS NOT NULL
                AND lease_heartbeat_at IS NOT NULL
                AND lease_heartbeat_at >= lease_acquired_at
                AND lease_expires_at IS NOT NULL
                AND lease_expires_at > lease_heartbeat_at
                AND lease_expires_at - lease_heartbeat_at
                    <= interval '5 minutes'
                AND failure_code IS NULL
                AND result_profile_json IS NULL
                AND result_profile_fingerprint IS NULL
                AND completed_at IS NULL
                AND retain_until IS NULL
            )
            OR
            (
                status = 'retry_wait'
                AND attempt_count >= 1
                AND lease_owner_id IS NULL
                AND lease_capability_digest IS NULL
                AND lease_acquired_at IS NULL
                AND lease_heartbeat_at IS NULL
                AND lease_expires_at IS NULL
                AND failure_code IN (
                    'source_unavailable',
                    'source_timeout',
                    'lease_expired',
                    'shutdown_requested'
                )
                AND result_profile_json IS NULL
                AND result_profile_fingerprint IS NULL
                AND completed_at IS NULL
                AND retain_until IS NULL
                AND available_at > updated_at
            )
            OR
            (
                status = 'completed'
                AND attempt_count >= 1
                AND lease_owner_id IS NULL
                AND lease_capability_digest IS NULL
                AND lease_acquired_at IS NULL
                AND lease_heartbeat_at IS NULL
                AND lease_expires_at IS NULL
                AND failure_code IS NULL
                AND result_profile_json IS NOT NULL
                AND result_profile_fingerprint IS NOT NULL
                AND completed_at IS NOT NULL
                AND updated_at = completed_at
                AND retain_until IS NOT NULL
                AND retain_until >= completed_at
                AND retain_until - completed_at <= interval '366 days'
            )
            OR
            (
                status = 'failed'
                AND attempt_count >= 1
                AND lease_owner_id IS NULL
                AND lease_capability_digest IS NULL
                AND lease_acquired_at IS NULL
                AND lease_heartbeat_at IS NULL
                AND lease_expires_at IS NULL
                AND failure_code IS NOT NULL
                AND result_profile_json IS NULL
                AND result_profile_fingerprint IS NULL
                AND completed_at IS NOT NULL
                AND updated_at = completed_at
                AND retain_until IS NOT NULL
                AND retain_until >= completed_at
                AND retain_until - completed_at <= interval '366 days'
            )
        )
);

CREATE INDEX semantic_join_profile_job_claim_idx
    ON schemabridge_control.semantic_join_profile_jobs (
        workspace_id,
        connection_id,
        available_at,
        requested_at,
        job_id
    )
    WHERE status IN ('requested', 'retry_wait');

CREATE INDEX semantic_join_profile_job_expired_lease_idx
    ON schemabridge_control.semantic_join_profile_jobs (
        workspace_id,
        connection_id,
        lease_expires_at,
        job_id
    )
    WHERE status = 'leased';

CREATE INDEX semantic_join_profile_job_scan_idx
    ON schemabridge_control.semantic_join_profile_jobs (
        workspace_id,
        scan_id,
        proposal_fingerprint,
        status
    );

CREATE FUNCTION schemabridge_control.reject_semantic_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    RAISE EXCEPTION 'immutable semantic change record cannot be changed'
        USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION schemabridge_control.guard_semantic_change_head()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'semantic change head cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.head_revision <> 1 THEN
            RAISE EXCEPTION 'semantic change head must start at revision one'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.catalog_scope IS DISTINCT FROM OLD.catalog_scope
        OR NEW.registry_id IS DISTINCT FROM OLD.registry_id
        OR NEW.head_revision <> OLD.head_revision + 1
        OR NEW.updated_at <= OLD.updated_at
    ) THEN
        RAISE EXCEPTION 'semantic change head compare-and-swap is invalid'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_semantic_change_scan()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'semantic change scan cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF (
            NEW.status <> 'requested'
            OR NEW.attempts <> 0
            OR NEW.fencing_token <> 0
        ) THEN
            RAISE EXCEPTION 'new semantic change scan is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF (
        NEW.scan_id IS DISTINCT FROM OLD.scan_id
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.source_kind IS DISTINCT FROM OLD.source_kind
        OR NEW.source_event_key IS DISTINCT FROM OLD.source_event_key
        OR NEW.source_fingerprint IS DISTINCT FROM OLD.source_fingerprint
        OR NEW.catalog_scope IS DISTINCT FROM OLD.catalog_scope
        OR NEW.registry_id IS DISTINCT FROM OLD.registry_id
        OR NEW.registry_generation IS DISTINCT FROM OLD.registry_generation
        OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
        OR NEW.base_catalog_generation
            IS DISTINCT FROM OLD.base_catalog_generation
        OR NEW.observed_catalog_generation
            IS DISTINCT FROM OLD.observed_catalog_generation
        OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
        OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
        OR NEW.attempts < OLD.attempts
        OR NEW.fencing_token < OLD.fencing_token
        OR NEW.updated_at <= OLD.updated_at
    ) THEN
        RAISE EXCEPTION 'semantic change scan identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF (
        (OLD.status IN ('requested', 'retry_wait')
            AND NEW.status NOT IN ('leased', 'superseded'))
        OR (OLD.status = 'leased'
            AND NEW.status NOT IN (
                'leased',
                'retry_wait',
                'completed',
                'failed',
                'superseded'
            ))
        OR OLD.status IN ('completed', 'failed', 'superseded')
    ) THEN
        RAISE EXCEPTION 'semantic change scan transition is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF (
        NEW.status = 'leased'
        AND OLD.status IN ('requested', 'retry_wait')
        AND (
            NEW.fencing_token <= OLD.fencing_token
            OR NEW.attempts <> OLD.attempts + 1
            OR NEW.lease_acquired_at IS NULL
            OR NEW.lease_heartbeat_at
                IS DISTINCT FROM NEW.lease_acquired_at
        )
    ) THEN
        RAISE EXCEPTION 'semantic change scan lease fence is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF (
        OLD.status = 'leased'
        AND NEW.status = 'leased'
        AND (
            NEW.fencing_token <> OLD.fencing_token
            OR NEW.attempts <> OLD.attempts
            OR NEW.lease_owner_id IS DISTINCT FROM OLD.lease_owner_id
            OR NEW.lease_capability_digest
                IS DISTINCT FROM OLD.lease_capability_digest
            OR NEW.lease_acquired_at
                IS DISTINCT FROM OLD.lease_acquired_at
            OR NEW.lease_heartbeat_at <= OLD.lease_heartbeat_at
            OR NEW.lease_expires_at <= OLD.lease_expires_at
        )
    ) THEN
        RAISE EXCEPTION 'semantic change scan heartbeat is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_semantic_join_profile_job()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'semantic join profile job cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF (
            NEW.status <> 'requested'
            OR NEW.attempt_count <> 0
            OR NEW.fencing_token <> 0
        ) THEN
            RAISE EXCEPTION 'new semantic join profile job is invalid'
                USING ERRCODE = '55000';
        END IF;
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                concat_ws(
                    '|',
                    'semantic_join_profile_capacity_v1',
                    NEW.workspace_id,
                    NEW.scan_id
                ),
                0
            )
        );
        IF NOT EXISTS (
            SELECT 1
            FROM schemabridge_control.semantic_join_profile_jobs AS replay
            WHERE replay.workspace_id = NEW.workspace_id
              AND replay.scan_id = NEW.scan_id
              AND replay.proposal_fingerprint = NEW.proposal_fingerprint
        ) AND (
            SELECT count(*)
            FROM schemabridge_control.semantic_join_profile_jobs AS job
            WHERE job.workspace_id = NEW.workspace_id
              AND job.scan_id = NEW.scan_id
        ) >= 500 THEN
            RAISE EXCEPTION 'semantic join profile queue capacity is exhausted'
                USING ERRCODE = '53300';
        END IF;
        RETURN NEW;
    END IF;
    IF (
        NEW.job_id IS DISTINCT FROM OLD.job_id
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.scan_id IS DISTINCT FROM OLD.scan_id
        OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
        OR NEW.proposal_fingerprint IS DISTINCT FROM OLD.proposal_fingerprint
        OR NEW.proposal_json IS DISTINCT FROM OLD.proposal_json
        OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
        OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
        OR NEW.attempt_count < OLD.attempt_count
        OR NEW.fencing_token < OLD.fencing_token
        OR NEW.updated_at <= OLD.updated_at
    ) THEN
        RAISE EXCEPTION 'semantic join profile job identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status IN ('completed', 'failed') THEN
        RAISE EXCEPTION 'terminal semantic join profile job is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF (
        (
            OLD.status IN ('requested', 'retry_wait')
            AND NEW.status <> 'leased'
        )
        OR (
            OLD.status = 'leased'
            AND NEW.status NOT IN (
                'leased',
                'retry_wait',
                'completed',
                'failed'
            )
        )
    ) THEN
        RAISE EXCEPTION 'semantic join profile job transition is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF (
        NEW.status = 'leased'
        AND OLD.status IN ('requested', 'retry_wait')
        AND (
            NEW.attempt_count <> OLD.attempt_count + 1
            OR NEW.fencing_token <> OLD.fencing_token + 1
            OR NEW.lease_acquired_at IS NULL
            OR NEW.lease_heartbeat_at
                IS DISTINCT FROM NEW.lease_acquired_at
        )
    ) THEN
        RAISE EXCEPTION 'semantic join profile lease fence is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF (
        OLD.status = 'leased'
        AND NEW.status = 'leased'
        AND (
            NEW.attempt_count <> OLD.attempt_count
            OR NEW.fencing_token <> OLD.fencing_token
            OR NEW.lease_owner_id IS DISTINCT FROM OLD.lease_owner_id
            OR NEW.lease_capability_digest
                IS DISTINCT FROM OLD.lease_capability_digest
            OR NEW.lease_acquired_at
                IS DISTINCT FROM OLD.lease_acquired_at
            OR NEW.lease_heartbeat_at <= OLD.lease_heartbeat_at
            OR NEW.lease_expires_at <= OLD.lease_expires_at
        )
    ) THEN
        RAISE EXCEPTION 'semantic join profile heartbeat is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF (
        OLD.status = 'leased'
        AND NEW.status IN ('retry_wait', 'completed', 'failed')
        AND (
            NEW.attempt_count <> OLD.attempt_count
            OR NEW.fencing_token <> OLD.fencing_token
        )
    ) THEN
        RAISE EXCEPTION 'semantic join profile terminal fence is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.capture_catalog_generation_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    event_time timestamptz := coalesce(
        NEW.active_generation_completed_at,
        clock_timestamp()
    );
    event_key text;
    event_digest char(64);
    target_scope record;
BEGIN
    IF (
        NEW.active_generation IS NULL
        OR NEW.active_generation IS NOT DISTINCT FROM OLD.active_generation
    ) THEN
        RETURN NEW;
    END IF;

    IF OLD.active_generation IS NOT NULL THEN
        INSERT INTO schemabridge_control.catalog_generation_changes (
        change_id,
        workspace_id,
        connection_id,
        base_generation,
        observed_generation,
        resource_kind,
        change_kind,
        asset_key,
        asset_id,
        resource_key,
        field_key,
        field_path,
        previous_metadata_fingerprint,
        current_metadata_fingerprint,
        change_fingerprint,
        observed_at
    )
    WITH previous_asset AS (
        SELECT asset_key, asset_id, metadata_fingerprint
        FROM schemabridge_control.catalog_assets
        WHERE workspace_id = NEW.workspace_id
          AND connection_id = NEW.connection_id
          AND generation = OLD.active_generation
    ),
    current_asset AS (
        SELECT asset_key, asset_id, metadata_fingerprint
        FROM schemabridge_control.catalog_assets
        WHERE workspace_id = NEW.workspace_id
          AND connection_id = NEW.connection_id
          AND generation = NEW.active_generation
    ),
    changed AS (
        SELECT
            coalesce(current_asset.asset_key, previous_asset.asset_key) AS asset_key,
            coalesce(current_asset.asset_id, previous_asset.asset_id) AS asset_id,
            previous_asset.metadata_fingerprint AS previous_fingerprint,
            current_asset.metadata_fingerprint AS current_fingerprint,
            CASE
                WHEN previous_asset.asset_key IS NULL THEN 'added'
                WHEN current_asset.asset_key IS NULL THEN 'removed'
                ELSE 'metadata_changed'
            END AS change_kind
        FROM previous_asset
        FULL OUTER JOIN current_asset
          ON current_asset.asset_key = previous_asset.asset_key
        WHERE previous_asset.asset_key IS NULL
           OR current_asset.asset_key IS NULL
           OR previous_asset.metadata_fingerprint
                IS DISTINCT FROM current_asset.metadata_fingerprint
    ),
    fingerprinted AS (
        SELECT
            changed.*,
            encode(
                sha256(
                    convert_to(
                        concat_ws(
                            '|',
                            'catalog_asset_change_v1',
                            NEW.workspace_id,
                            NEW.connection_id,
                            coalesce(OLD.active_generation::text, 'none'),
                            NEW.active_generation::text,
                            changed.asset_key,
                            changed.change_kind,
                            coalesce(changed.previous_fingerprint, 'none'),
                            coalesce(changed.current_fingerprint, 'none')
                        ),
                        'UTF8'
                    )
                ),
                'hex'
            ) AS fingerprint
        FROM changed
    )
    SELECT
        'chg_' || fingerprint,
        NEW.workspace_id,
        NEW.connection_id,
        OLD.active_generation,
        NEW.active_generation,
        'asset',
        change_kind,
        asset_key,
        asset_id,
        asset_key,
        NULL,
        NULL,
        previous_fingerprint,
        current_fingerprint,
        fingerprint,
        event_time
    FROM fingerprinted
        ON CONFLICT DO NOTHING;

        INSERT INTO schemabridge_control.catalog_generation_changes (
        change_id,
        workspace_id,
        connection_id,
        base_generation,
        observed_generation,
        resource_kind,
        change_kind,
        asset_key,
        asset_id,
        resource_key,
        field_key,
        field_path,
        previous_metadata_fingerprint,
        current_metadata_fingerprint,
        change_fingerprint,
        observed_at
    )
    WITH previous_field AS (
        SELECT
            field.asset_key,
            asset.asset_id,
            field.field_key,
            field.field_path,
            field.metadata_fingerprint
        FROM schemabridge_control.catalog_fields AS field
        JOIN schemabridge_control.catalog_assets AS asset
          ON asset.workspace_id = field.workspace_id
         AND asset.connection_id = field.connection_id
         AND asset.generation = field.generation
         AND asset.asset_key = field.asset_key
        WHERE field.workspace_id = NEW.workspace_id
          AND field.connection_id = NEW.connection_id
          AND field.generation = OLD.active_generation
    ),
    current_field AS (
        SELECT
            field.asset_key,
            asset.asset_id,
            field.field_key,
            field.field_path,
            field.metadata_fingerprint
        FROM schemabridge_control.catalog_fields AS field
        JOIN schemabridge_control.catalog_assets AS asset
          ON asset.workspace_id = field.workspace_id
         AND asset.connection_id = field.connection_id
         AND asset.generation = field.generation
         AND asset.asset_key = field.asset_key
        WHERE field.workspace_id = NEW.workspace_id
          AND field.connection_id = NEW.connection_id
          AND field.generation = NEW.active_generation
    ),
    changed AS (
        SELECT
            coalesce(current_field.asset_key, previous_field.asset_key) AS asset_key,
            coalesce(current_field.asset_id, previous_field.asset_id) AS asset_id,
            coalesce(current_field.field_key, previous_field.field_key) AS field_key,
            coalesce(current_field.field_path, previous_field.field_path) AS field_path,
            previous_field.metadata_fingerprint AS previous_fingerprint,
            current_field.metadata_fingerprint AS current_fingerprint,
            CASE
                WHEN previous_field.field_key IS NULL THEN 'added'
                WHEN current_field.field_key IS NULL THEN 'removed'
                ELSE 'metadata_changed'
            END AS change_kind
        FROM previous_field
        FULL OUTER JOIN current_field
          ON current_field.asset_key = previous_field.asset_key
         AND current_field.field_key = previous_field.field_key
        WHERE previous_field.field_key IS NULL
           OR current_field.field_key IS NULL
           OR previous_field.metadata_fingerprint
                IS DISTINCT FROM current_field.metadata_fingerprint
    ),
    fingerprinted AS (
        SELECT
            changed.*,
            encode(
                sha256(
                    convert_to(
                        concat_ws(
                            '|',
                            'catalog_field_change_v1',
                            NEW.workspace_id,
                            NEW.connection_id,
                            coalesce(OLD.active_generation::text, 'none'),
                            NEW.active_generation::text,
                            changed.asset_key,
                            changed.field_key,
                            changed.change_kind,
                            coalesce(changed.previous_fingerprint, 'none'),
                            coalesce(changed.current_fingerprint, 'none')
                        ),
                        'UTF8'
                    )
                ),
                'hex'
            ) AS fingerprint
        FROM changed
    )
    SELECT
        'chg_' || fingerprint,
        NEW.workspace_id,
        NEW.connection_id,
        OLD.active_generation,
        NEW.active_generation,
        'field',
        change_kind,
        asset_key,
        asset_id,
        field_key,
        field_key,
        field_path,
        previous_fingerprint,
        current_fingerprint,
        fingerprint,
        event_time
    FROM fingerprinted
        ON CONFLICT DO NOTHING;
    END IF;

    FOR target_scope IN
        SELECT
            pointer.catalog_scope,
            pointer.registry_id,
            pointer.generation,
            pointer.registry_fingerprint
        FROM schemabridge_control.registry_active_pointers AS pointer
        WHERE pointer.workspace_id = NEW.workspace_id
          AND pointer.catalog_scope = NEW.catalog_scope
        ORDER BY pointer.catalog_scope, pointer.registry_id
    LOOP
        event_key := concat_ws(
            ':',
            NEW.connection_id,
            NEW.active_generation::text,
            target_scope.catalog_scope,
            target_scope.registry_id
        );
        event_digest := encode(
            sha256(
                convert_to(
                    concat_ws(
                        '|',
                        'catalog_generation_scan_v2',
                        NEW.workspace_id,
                        event_key,
                        NEW.active_generation_fingerprint,
                        target_scope.generation::text,
                        target_scope.registry_fingerprint
                    ),
                    'UTF8'
                )
            ),
            'hex'
        );
        INSERT INTO schemabridge_control.semantic_change_scan_requests (
            scan_id,
            workspace_id,
            source_kind,
            source_event_key,
            source_fingerprint,
            catalog_scope,
            registry_id,
            registry_generation,
            connection_id,
            base_catalog_generation,
            observed_catalog_generation,
            status,
            attempts,
            available_at,
            fencing_token,
            requested_at,
            updated_at
        ) VALUES (
            'scan_' || event_digest,
            NEW.workspace_id,
            'catalog_generation',
            event_key,
            event_digest,
            target_scope.catalog_scope,
            target_scope.registry_id,
            NULL,
            NEW.connection_id,
            OLD.active_generation,
            NEW.active_generation,
            'requested',
            0,
            event_time,
            0,
            event_time,
            event_time
        )
        ON CONFLICT (workspace_id, source_kind, source_event_key) DO NOTHING;
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.enqueue_registry_semantic_scan()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    event_time timestamptz := clock_timestamp();
    event_digest char(64);
BEGIN
    IF (
        TG_OP = 'UPDATE'
        AND NEW.generation IS NOT DISTINCT FROM OLD.generation
    ) THEN
        RETURN NEW;
    END IF;
    event_digest := encode(
        sha256(
            convert_to(
                concat_ws(
                    '|',
                    'registry_pointer_scan_v1',
                    NEW.workspace_id,
                    NEW.catalog_scope,
                    NEW.registry_id,
                    NEW.generation::text,
                    NEW.registry_fingerprint,
                    NEW.transition_id
                ),
                'UTF8'
            )
        ),
        'hex'
    );
    INSERT INTO schemabridge_control.semantic_change_scan_requests (
        scan_id,
        workspace_id,
        source_kind,
        source_event_key,
        source_fingerprint,
        catalog_scope,
        registry_id,
        registry_generation,
        connection_id,
        base_catalog_generation,
        observed_catalog_generation,
        status,
        attempts,
        available_at,
        fencing_token,
        requested_at,
        updated_at
    ) VALUES (
        'scan_' || event_digest,
        NEW.workspace_id,
        'registry_pointer',
        NEW.transition_id,
        event_digest,
        NEW.catalog_scope,
        NEW.registry_id,
        NEW.generation,
        NULL,
        NULL,
        NULL,
        'requested',
        0,
        event_time,
        0,
        event_time,
        event_time
    )
    ON CONFLICT (workspace_id, source_kind, source_event_key) DO NOTHING;
    RETURN NEW;
END;
$$;

CREATE TRIGGER catalog_generation_changes_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.catalog_generation_changes
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_immutable_mutation();

CREATE TRIGGER semantic_resource_bindings_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.semantic_resource_bindings
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_immutable_mutation();

CREATE TRIGGER semantic_join_profiles_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.semantic_join_profiles
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_immutable_mutation();

CREATE TRIGGER semantic_change_reports_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.semantic_change_reports
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_immutable_mutation();

CREATE TRIGGER semantic_change_findings_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.semantic_change_findings
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_immutable_mutation();

CREATE TRIGGER semantic_change_impacts_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.semantic_change_impacts
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_immutable_mutation();

CREATE TRIGGER semantic_change_resolutions_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.semantic_change_resolutions
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_immutable_mutation();

CREATE TRIGGER semantic_artifact_dependencies_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.semantic_artifact_dependencies
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_semantic_immutable_mutation();

CREATE TRIGGER semantic_change_heads_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_change_heads
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_semantic_change_head();

CREATE TRIGGER semantic_change_scan_requests_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_change_scan_requests
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_semantic_change_scan();

CREATE TRIGGER semantic_join_profile_jobs_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.semantic_join_profile_jobs
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_semantic_join_profile_job();

CREATE TRIGGER catalog_generation_semantic_change_capture
AFTER UPDATE OF active_generation
ON schemabridge_control.catalog_connections
FOR EACH ROW
WHEN (
    NEW.active_generation IS NOT NULL
    AND NEW.active_generation IS DISTINCT FROM OLD.active_generation
)
EXECUTE FUNCTION schemabridge_control.capture_catalog_generation_change();

CREATE TRIGGER registry_pointer_semantic_scan_enqueue
AFTER INSERT OR UPDATE OF generation
ON schemabridge_control.registry_active_pointers
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.enqueue_registry_semantic_scan();

CREATE FUNCTION schemabridge_control.semantic_catalog_asset_locator_key(
    workspace_id varchar,
    connection_id varchar,
    generation bigint,
    qualified_name varchar
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
    SELECT
        octet_length(workspace_id)::text || ':' || workspace_id
        || octet_length(connection_id)::text || ':' || connection_id
        || octet_length(generation::text)::text || ':' || generation::text
        || octet_length(qualified_name)::text || ':' || qualified_name;
$$;

CREATE FUNCTION schemabridge_control.semantic_catalog_field_locator_key(
    workspace_id varchar,
    connection_id varchar,
    generation bigint,
    asset_key char(64),
    field_key char(64)
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
    SELECT
        octet_length(workspace_id)::text || ':' || workspace_id
        || octet_length(connection_id)::text || ':' || connection_id
        || octet_length(generation::text)::text || ':' || generation::text
        || octet_length(asset_key::text)::text || ':' || asset_key::text
        || octet_length(field_key::text)::text || ':' || field_key::text;
$$;

CREATE INDEX catalog_assets_semantic_lookup_idx
    ON schemabridge_control.catalog_assets (
        schemabridge_control.semantic_catalog_asset_locator_key(
            workspace_id,
            connection_id,
            generation,
            qualified_name
        )
    );

CREATE UNIQUE INDEX catalog_fields_semantic_lookup_idx
    ON schemabridge_control.catalog_fields (
        schemabridge_control.semantic_catalog_field_locator_key(
            workspace_id,
            connection_id,
            generation,
            asset_key,
            field_key
        )
    );

CREATE VIEW schemabridge_control.semantic_catalog_evidence_projection
WITH (security_barrier = true)
AS
SELECT
    connection.workspace_id,
    connection.catalog_scope,
    connection.connection_id,
    connection.active_generation AS catalog_generation,
    connection.active_generation_fingerprint AS catalog_generation_fingerprint,
    asset.asset_key,
    asset.asset_id,
    asset.qualified_name,
    asset.metadata_fingerprint AS asset_metadata_fingerprint,
    field.field_key,
    field.field_path,
    field.field_name,
    field.native_type,
    field.normalized_type,
    field.nullable,
    field.is_part_of_key,
    field.metadata_fingerprint AS field_metadata_fingerprint,
    schemabridge_control.semantic_definition_fingerprint(
        field.description
    ) AS field_definition_fingerprint,
    schemabridge_control.semantic_terms_fingerprint(
        field.tags,
        field.glossary_terms
    ) AS field_terms_fingerprint
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
WHERE connection.status = 'enabled';

CREATE VIEW schemabridge_control.semantic_catalog_connection_evidence_projection
WITH (security_barrier = true)
AS
SELECT
    workspace_id,
    catalog_scope,
    connection_id,
    active_generation AS catalog_generation,
    active_generation_fingerprint AS catalog_generation_fingerprint
FROM schemabridge_control.catalog_connections
WHERE status = 'enabled'
  AND active_generation IS NOT NULL
  AND active_generation_fingerprint IS NOT NULL;

CREATE FUNCTION schemabridge_control.load_semantic_initial_catalog_candidates(
    requested_workspace_id varchar(200),
    requested_catalog_scope varchar(120),
    requested_fields jsonb
)
RETURNS TABLE (
    ordinal integer,
    connection_id varchar(200),
    catalog_generation bigint,
    catalog_generation_fingerprint char(64),
    asset_key char(64),
    asset_id varchar(500),
    qualified_name varchar(500),
    asset_metadata_fingerprint char(64),
    field_key char(64),
    field_path varchar(200)[],
    normalized_type varchar(32),
    nullable boolean,
    is_part_of_key boolean,
    field_metadata_fingerprint char(64),
    field_definition_fingerprint char(64),
    field_terms_fingerprint char(64),
    asset_present boolean,
    field_present boolean,
    candidate_count bigint
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
    WITH checked_input AS MATERIALIZED (
        SELECT
            CASE
                WHEN jsonb_typeof(requested_fields) = 'array' THEN
                    CASE
                        WHEN jsonb_array_length(requested_fields)
                                BETWEEN 1 AND 2000
                         AND octet_length(requested_fields::text) <= 2000000
                         AND length(trim(requested_workspace_id))
                                BETWEEN 1 AND 200
                         AND length(trim(requested_catalog_scope))
                                BETWEEN 1 AND 120
                        THEN requested_fields
                        ELSE '[]'::jsonb
                    END
                ELSE '[]'::jsonb
            END AS payload
    ),
    raw_requested AS MATERIALIZED (
        SELECT value
        FROM checked_input
        CROSS JOIN LATERAL jsonb_array_elements(payload)
    ),
    parsed_requested AS MATERIALIZED (
        SELECT
            item.ordinal,
            item.dataset_ref,
            item.field_path,
            item.selected_connection_id,
            item.selected_asset_id,
            item.selected_field_path
        FROM raw_requested AS raw
        CROSS JOIN LATERAL jsonb_to_record(
            CASE
                WHEN jsonb_typeof(raw.value) = 'object' THEN raw.value
                ELSE '{}'::jsonb
            END
        ) AS item(
            ordinal integer,
            dataset_ref varchar(500),
            field_path varchar(200)[],
            selected_connection_id varchar(200),
            selected_asset_id varchar(500),
            selected_field_path varchar(200)[]
        )
        WHERE jsonb_typeof(raw.value) = 'object'
          AND raw.value ?& ARRAY[
              'ordinal',
              'dataset_ref',
              'field_path',
              'selected_connection_id',
              'selected_asset_id',
              'selected_field_path'
          ]
          AND (
              raw.value - ARRAY[
                  'ordinal',
                  'dataset_ref',
                  'field_path',
                  'selected_connection_id',
                  'selected_asset_id',
                  'selected_field_path'
              ]
          ) = '{}'::jsonb
          AND item.ordinal BETWEEN 0 AND 1999
          AND item.dataset_ref
                ~ '^[A-Za-z_][A-Za-z0-9_]*[.][A-Za-z_][A-Za-z0-9_]*$'
          AND cardinality(item.field_path) BETWEEN 1 AND 64
          AND array_position(item.field_path, NULL) IS NULL
          AND octet_length(array_to_string(item.field_path, '.')) <= 12800
          AND NOT EXISTS (
              SELECT 1
              FROM unnest(item.field_path) AS segment(value)
              WHERE length(segment.value) NOT BETWEEN 1 AND 200
                 OR segment.value !~ '^[A-Za-z_][A-Za-z0-9_]*$'
          )
          AND (
              (
                  item.selected_connection_id IS NULL
                  AND item.selected_asset_id IS NULL
                  AND item.selected_field_path IS NULL
              )
              OR
              (
                  item.selected_connection_id
                        ~ '^[a-z][a-z0-9_-]{2,199}$'
                  AND length(item.selected_asset_id) BETWEEN 1 AND 500
                  AND octet_length(item.selected_asset_id) <= 2000
                  AND cardinality(item.selected_field_path) BETWEEN 1 AND 64
                  AND array_position(item.selected_field_path, NULL) IS NULL
                  AND item.selected_field_path = item.field_path
                  AND octet_length(
                      array_to_string(item.selected_field_path, '.')
                  ) <= 12800
                  AND NOT EXISTS (
                      SELECT 1
                      FROM unnest(item.selected_field_path) AS segment(value)
                      WHERE length(segment.value) NOT BETWEEN 1 AND 200
                         OR segment.value !~ '^[A-Za-z_][A-Za-z0-9_]*$'
                  )
              )
          )
    ),
    requested AS MATERIALIZED (
        SELECT
            parsed.*,
            encode(
                sha256(
                    convert_to(
                        replace(to_jsonb(parsed.field_path)::text, ', ', ','),
                        'UTF8'
                    )
                ),
                'hex'
            )::char(64) AS field_key
        FROM parsed_requested AS parsed
        WHERE (
            SELECT count(*)
            FROM parsed_requested
        ) = (
            SELECT jsonb_array_length(payload)
            FROM checked_input
        )
          AND (
              SELECT count(DISTINCT ordinal)
              FROM parsed_requested
          ) = (
              SELECT count(*)
              FROM parsed_requested
          )
          AND (
              SELECT coalesce(min(ordinal), 0) = 0
                 AND coalesce(max(ordinal), -1) = count(*) - 1
              FROM parsed_requested
          )
    )
    SELECT
        requested.ordinal,
        candidate.connection_id,
        candidate.catalog_generation,
        candidate.catalog_generation_fingerprint,
        candidate.asset_key,
        candidate.asset_id,
        candidate.qualified_name,
        candidate.asset_metadata_fingerprint,
        candidate.field_key,
        candidate.field_path,
        candidate.normalized_type,
        candidate.nullable,
        candidate.is_part_of_key,
        candidate.field_metadata_fingerprint,
        candidate.field_definition_fingerprint,
        candidate.field_terms_fingerprint,
        candidate.asset_key IS NOT NULL,
        candidate.field_key IS NOT NULL,
        coalesce(candidate.candidate_count, 0)
    FROM requested
    LEFT JOIN LATERAL (
        SELECT
            source_connection.connection_id,
            source_connection.active_generation AS catalog_generation,
            source_connection.active_generation_fingerprint
                AS catalog_generation_fingerprint,
            asset.asset_key,
            asset.asset_id,
            asset.qualified_name,
            asset.metadata_fingerprint AS asset_metadata_fingerprint,
            field.field_key,
            field.field_path,
            field.normalized_type,
            field.nullable,
            field.is_part_of_key,
            field.metadata_fingerprint AS field_metadata_fingerprint,
            schemabridge_control.semantic_definition_fingerprint(
                field.description
            ) AS field_definition_fingerprint,
            schemabridge_control.semantic_terms_fingerprint(
                field.tags,
                field.glossary_terms
            ) AS field_terms_fingerprint,
            count(*) OVER () AS candidate_count,
            row_number() OVER (
                ORDER BY
                    CASE
                        WHEN source_connection.connection_id
                                = requested.selected_connection_id
                         AND asset.asset_id = requested.selected_asset_id
                         AND field.field_path = requested.selected_field_path
                        THEN 0
                        ELSE 1
                    END,
                    source_connection.connection_id,
                    asset.asset_id
            ) AS candidate_rank
        FROM schemabridge_control.catalog_connections AS source_connection
        JOIN LATERAL (
            SELECT inner_asset.*
            FROM schemabridge_control.catalog_assets AS inner_asset
            WHERE schemabridge_control.semantic_catalog_asset_locator_key(
                      inner_asset.workspace_id,
                      inner_asset.connection_id,
                      inner_asset.generation,
                      inner_asset.qualified_name
                  )
                  = schemabridge_control.semantic_catalog_asset_locator_key(
                      requested_workspace_id,
                      source_connection.connection_id,
                      source_connection.active_generation,
                      requested.dataset_ref
                  )
            OFFSET 0
        ) AS asset
          ON asset.workspace_id = requested_workspace_id
         AND asset.workspace_id = source_connection.workspace_id
         AND asset.connection_id = source_connection.connection_id
         AND asset.generation = source_connection.active_generation
         AND asset.qualified_name = requested.dataset_ref
        JOIN LATERAL (
            SELECT inner_field.*
            FROM schemabridge_control.catalog_fields AS inner_field
            WHERE schemabridge_control.semantic_catalog_field_locator_key(
                      inner_field.workspace_id,
                      inner_field.connection_id,
                      inner_field.generation,
                      inner_field.asset_key,
                      inner_field.field_key
                  )
                  = schemabridge_control.semantic_catalog_field_locator_key(
                      asset.workspace_id,
                      asset.connection_id,
                      asset.generation,
                      asset.asset_key,
                      requested.field_key
                  )
            OFFSET 0
        ) AS field
          ON field.workspace_id = asset.workspace_id
         AND field.connection_id = asset.connection_id
         AND field.generation = asset.generation
         AND field.asset_key = asset.asset_key
         AND field.field_key = requested.field_key
         AND field.field_path = requested.field_path
        WHERE source_connection.workspace_id = requested_workspace_id
          AND source_connection.catalog_scope = requested_catalog_scope
          AND source_connection.status = 'enabled'
          AND source_connection.active_generation IS NOT NULL
          AND source_connection.active_generation_fingerprint IS NOT NULL
        ORDER BY candidate_rank
        LIMIT 2
    ) AS candidate ON true
    ORDER BY requested.ordinal, candidate.candidate_rank;
$$;

CREATE FUNCTION schemabridge_control.load_semantic_bound_catalog_evidence(
    requested_workspace_id varchar(200),
    requested_catalog_scope varchar(120),
    requested_bindings jsonb
)
RETURNS TABLE (
    ordinal integer,
    connection_id varchar(200),
    catalog_generation bigint,
    catalog_generation_fingerprint char(64),
    asset_key char(64),
    asset_id varchar(500),
    qualified_name varchar(500),
    asset_metadata_fingerprint char(64),
    field_key char(64),
    field_path varchar(200)[],
    normalized_type varchar(32),
    nullable boolean,
    is_part_of_key boolean,
    field_metadata_fingerprint char(64),
    field_definition_fingerprint char(64),
    field_terms_fingerprint char(64),
    asset_present boolean,
    field_present boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
    WITH checked_input AS MATERIALIZED (
        SELECT
            CASE
                WHEN jsonb_typeof(requested_bindings) = 'array' THEN
                    CASE
                        WHEN jsonb_array_length(requested_bindings)
                                BETWEEN 1 AND 2000
                         AND octet_length(requested_bindings::text) <= 2000000
                         AND length(trim(requested_workspace_id))
                                BETWEEN 1 AND 200
                         AND length(trim(requested_catalog_scope))
                                BETWEEN 1 AND 120
                        THEN requested_bindings
                        ELSE '[]'::jsonb
                    END
                ELSE '[]'::jsonb
            END AS payload
    ),
    raw_requested AS MATERIALIZED (
        SELECT value
        FROM checked_input
        CROSS JOIN LATERAL jsonb_array_elements(payload)
    ),
    requested AS MATERIALIZED (
        SELECT
            item.ordinal,
            item.connection_id,
            item.asset_id,
            item.field_path
        FROM raw_requested AS raw
        CROSS JOIN LATERAL jsonb_to_record(
            CASE
                WHEN jsonb_typeof(raw.value) = 'object' THEN raw.value
                ELSE '{}'::jsonb
            END
        ) AS item(
            ordinal integer,
            connection_id varchar(200),
            asset_id varchar(500),
            field_path varchar(200)[]
        )
        WHERE jsonb_typeof(raw.value) = 'object'
          AND raw.value ?& ARRAY[
              'ordinal',
              'connection_id',
              'asset_id',
              'field_path'
          ]
          AND (
              raw.value - ARRAY[
                  'ordinal',
                  'connection_id',
                  'asset_id',
                  'field_path'
              ]
          ) = '{}'::jsonb
          AND item.ordinal BETWEEN 0 AND 1999
          AND item.connection_id ~ '^[a-z][a-z0-9_-]{2,199}$'
          AND length(item.asset_id) BETWEEN 1 AND 500
          AND octet_length(item.asset_id) <= 2000
          AND cardinality(item.field_path) BETWEEN 1 AND 64
          AND array_position(item.field_path, NULL) IS NULL
          AND octet_length(array_to_string(item.field_path, '.')) <= 12800
    )
    SELECT
        requested.ordinal,
        source_connection.connection_id,
        source_connection.active_generation,
        source_connection.active_generation_fingerprint,
        asset.asset_key,
        requested.asset_id,
        asset.qualified_name,
        asset.metadata_fingerprint,
        field.field_key,
        requested.field_path,
        field.normalized_type,
        field.nullable,
        field.is_part_of_key,
        field.metadata_fingerprint,
        schemabridge_control.semantic_definition_fingerprint(
            field.description
        ),
        schemabridge_control.semantic_terms_fingerprint(
            field.tags,
            field.glossary_terms
        ),
        asset.asset_key IS NOT NULL,
        field.field_key IS NOT NULL
    FROM requested
    LEFT JOIN schemabridge_control.catalog_connections AS source_connection
      ON source_connection.workspace_id = requested_workspace_id
     AND source_connection.catalog_scope = requested_catalog_scope
     AND source_connection.connection_id = requested.connection_id
     AND source_connection.status = 'enabled'
     AND source_connection.active_generation IS NOT NULL
     AND source_connection.active_generation_fingerprint IS NOT NULL
    LEFT JOIN schemabridge_control.catalog_assets AS asset
      ON asset.workspace_id = source_connection.workspace_id
     AND asset.connection_id = source_connection.connection_id
     AND asset.generation = source_connection.active_generation
     AND asset.asset_id = requested.asset_id
    LEFT JOIN schemabridge_control.catalog_fields AS field
      ON field.workspace_id = asset.workspace_id
     AND field.connection_id = asset.connection_id
     AND field.generation = asset.generation
     AND field.asset_key = asset.asset_key
     AND field.field_path = requested.field_path
    ORDER BY requested.ordinal;
$$;

CREATE VIEW schemabridge_control.semantic_context_gate_projection
WITH (security_barrier = true)
AS
WITH latest_context_report AS (
    SELECT
        head.workspace_id,
        head.catalog_scope,
        head.registry_id,
        report.report_id,
        report.dependency_index_watermark,
        report.dependency_index_fingerprint,
        report.dependency_index_complete,
        report.observation_json
    FROM schemabridge_control.semantic_change_heads AS head
    CROSS JOIN LATERAL (
        SELECT
            candidate.report_id,
            candidate.dependency_index_watermark,
            candidate.dependency_index_fingerprint,
            candidate.dependency_index_complete,
            candidate.observation_json
        FROM schemabridge_control.semantic_change_reports AS candidate
        WHERE candidate.workspace_id = head.workspace_id
          AND candidate.catalog_scope = head.catalog_scope
          AND candidate.registry_id = head.registry_id
          AND candidate.registry_generation = head.registry_generation
          AND candidate.registry_version = head.registry_version
          AND candidate.registry_fingerprint = head.registry_fingerprint
          AND candidate.baseline_revision = head.baseline_revision
        ORDER BY candidate.inspected_at DESC, candidate.report_id DESC
        LIMIT 1
    ) AS report
),
live_dependency_status AS (
    SELECT
        head.workspace_id,
        head.catalog_scope,
        head.registry_id,
        coalesce(
            live.complete
            AND live.registry_generation = head.registry_generation
            AND live.registry_version = head.registry_version
            AND live.registry_fingerprint = head.registry_fingerprint
            AND live.pointer_transition_id = head.pointer_transition_id
            AND (
                (
                    report.report_id IS NULL
                    AND head.dependency_index_complete
                    AND live.watermark = head.dependency_index_watermark
                    AND live.index_fingerprint
                        = head.dependency_index_fingerprint
                )
                OR
                (
                    report.report_id IS NOT NULL
                    AND report.dependency_index_complete
                    AND live.watermark = report.dependency_index_watermark
                    AND live.index_fingerprint
                        = report.dependency_index_fingerprint
                )
            ),
            false
        ) AS dependency_index_current
    FROM schemabridge_control.semantic_change_heads AS head
    LEFT JOIN schemabridge_control.semantic_dependency_index_states AS live
      ON live.workspace_id = head.workspace_id
     AND live.catalog_scope = head.catalog_scope
     AND live.registry_id = head.registry_id
    LEFT JOIN latest_context_report AS report
      ON report.workspace_id = head.workspace_id
     AND report.catalog_scope = head.catalog_scope
     AND report.registry_id = head.registry_id
),
latest_dependency_impacts AS (
    SELECT
        report.workspace_id,
        report.catalog_scope,
        report.registry_id,
        report.report_id,
        impact.artifact_kind,
        impact.artifact_id,
        impact.artifact_version,
        impact.impact_state
    FROM latest_context_report AS report
    JOIN schemabridge_control.semantic_change_impacts AS impact
      ON impact.workspace_id = report.workspace_id
     AND impact.catalog_scope = report.catalog_scope
     AND impact.registry_id = report.registry_id
     AND impact.report_id = report.report_id
),
latest_join_profiles AS (
    SELECT
        report.workspace_id,
        report.catalog_scope,
        report.registry_id,
        report.report_id,
        join_profile.value #>> '{join,contract_id}' AS contract_id,
        (join_profile.value #>> '{join,version}')::bigint AS contract_version,
        join_profile.value ->> 'safety_fingerprint' AS safety_fingerprint,
        join_profile.value ->> 'fingerprint' AS profile_fingerprint
    FROM latest_context_report AS report
    CROSS JOIN LATERAL jsonb_array_elements(
        report.observation_json -> 'joins'
    ) AS join_profile(value)
),
mapping_evidence AS (
    SELECT
        head.workspace_id,
        head.catalog_scope,
        head.registry_id,
        head.head_revision,
        head.registry_generation,
        head.registry_version,
        head.registry_fingerprint,
        head.baseline_revision,
        head.state AS context_state,
        head.catalog_generation_vector_fingerprint,
        head.dependency_index_watermark,
        head.dependency_index_fingerprint,
        head.dependency_index_complete,
        dependency_status.dependency_index_current,
        binding.mapping_decision_id,
        binding.mapping_version,
        binding.logical_field,
        binding.binding_id,
        binding.evidence_fingerprint,
        binding.connection_id,
        binding.asset_key,
        binding.asset_id,
        binding.field_key,
        binding.field_path,
        binding.catalog_generation AS baseline_catalog_generation,
        binding.catalog_generation_fingerprint
            AS baseline_catalog_generation_fingerprint,
        binding.asset_metadata_fingerprint
            AS baseline_asset_metadata_fingerprint,
        binding.field_metadata_fingerprint
            AS baseline_field_metadata_fingerprint,
        binding.field_definition_fingerprint
            AS baseline_field_definition_fingerprint,
        binding.field_terms_fingerprint
            AS baseline_field_terms_fingerprint,
        binding.normalized_type AS baseline_normalized_type,
        binding.nullable AS baseline_nullable,
        binding.is_part_of_key AS baseline_is_part_of_key,
        current_connection.active_generation AS current_catalog_generation,
        current_connection.active_generation_fingerprint
            AS current_catalog_generation_fingerprint,
        current_asset.asset_id AS current_asset_id,
        current_asset.metadata_fingerprint
            AS current_asset_metadata_fingerprint,
        current_field.field_path AS current_field_path,
        current_field.metadata_fingerprint
            AS current_field_metadata_fingerprint,
        schemabridge_control.semantic_definition_fingerprint(
            current_field.description
        ) AS current_field_definition_fingerprint,
        schemabridge_control.semantic_terms_fingerprint(
            current_field.tags,
            current_field.glossary_terms
        ) AS current_field_terms_fingerprint,
        current_field.normalized_type AS current_normalized_type,
        current_field.nullable AS current_nullable,
        current_field.is_part_of_key AS current_is_part_of_key,
        (
            current_connection.status = 'enabled'
            AND current_connection.active_generation IS NOT NULL
            AND current_asset.asset_key IS NOT NULL
            AND current_field.field_key IS NOT NULL
        ) AS catalog_evidence_available
    FROM schemabridge_control.semantic_change_heads AS head
    JOIN live_dependency_status AS dependency_status
      ON dependency_status.workspace_id = head.workspace_id
     AND dependency_status.catalog_scope = head.catalog_scope
     AND dependency_status.registry_id = head.registry_id
    JOIN schemabridge_control.semantic_resource_bindings AS binding
      ON binding.workspace_id = head.workspace_id
     AND binding.catalog_scope = head.catalog_scope
     AND binding.registry_id = head.registry_id
     AND binding.registry_generation = head.registry_generation
     AND binding.registry_fingerprint = head.registry_fingerprint
     AND binding.approved_baseline_revision = head.baseline_revision
     AND binding.binding_state IN ('approved', 'revalidated')
    LEFT JOIN schemabridge_control.catalog_connections AS current_connection
      ON current_connection.workspace_id = binding.workspace_id
     AND current_connection.catalog_scope = binding.catalog_scope
     AND current_connection.connection_id = binding.connection_id
    LEFT JOIN schemabridge_control.catalog_assets AS current_asset
      ON current_asset.workspace_id = current_connection.workspace_id
     AND current_asset.connection_id = current_connection.connection_id
     AND current_asset.generation = current_connection.active_generation
     AND current_asset.asset_key = binding.asset_key
    LEFT JOIN schemabridge_control.catalog_fields AS current_field
      ON current_field.workspace_id = current_asset.workspace_id
     AND current_field.connection_id = current_asset.connection_id
     AND current_field.generation = current_asset.generation
     AND current_field.asset_key = current_asset.asset_key
     AND current_field.field_key = binding.field_key
),
mapping_gate AS (
    SELECT
        evidence.*,
        report.report_id AS latest_report_id,
        report.dependency_index_complete
            AS latest_report_dependency_index_complete,
        impact.impact_state AS latest_impact_state,
        (
            evidence.catalog_evidence_available
            AND evidence.current_asset_id = evidence.asset_id
            AND evidence.current_field_path = evidence.field_path
            AND evidence.current_asset_metadata_fingerprint
                = evidence.baseline_asset_metadata_fingerprint
            AND evidence.current_field_metadata_fingerprint
                = evidence.baseline_field_metadata_fingerprint
            AND evidence.current_field_definition_fingerprint
                = evidence.baseline_field_definition_fingerprint
            AND evidence.current_field_terms_fingerprint
                = evidence.baseline_field_terms_fingerprint
            AND evidence.current_normalized_type
                IS NOT DISTINCT FROM evidence.baseline_normalized_type
            AND evidence.current_nullable
                IS NOT DISTINCT FROM evidence.baseline_nullable
            AND evidence.current_is_part_of_key
                IS NOT DISTINCT FROM evidence.baseline_is_part_of_key
        ) AS catalog_evidence_matches
    FROM mapping_evidence AS evidence
    LEFT JOIN latest_context_report AS report
      ON report.workspace_id = evidence.workspace_id
     AND report.catalog_scope = evidence.catalog_scope
     AND report.registry_id = evidence.registry_id
    LEFT JOIN latest_dependency_impacts AS impact
      ON impact.workspace_id = report.workspace_id
     AND impact.catalog_scope = report.catalog_scope
     AND impact.registry_id = report.registry_id
     AND impact.report_id = report.report_id
     AND impact.artifact_kind = 'mapping'
     AND impact.artifact_id = evidence.logical_field
     AND impact.artifact_version = evidence.mapping_version
)
SELECT
    mapping.workspace_id,
    mapping.catalog_scope,
    mapping.registry_id,
    mapping.head_revision,
    mapping.registry_generation,
    mapping.registry_version,
    mapping.registry_fingerprint,
    mapping.baseline_revision,
    mapping.context_state,
    mapping.catalog_generation_vector_fingerprint,
    mapping.dependency_index_watermark,
    mapping.dependency_index_fingerprint,
    mapping.dependency_index_complete,
    'mapping'::varchar(24) AS dependency_kind,
    mapping.mapping_decision_id AS dependency_id,
    mapping.mapping_version AS dependency_version,
    mapping.binding_id AS evidence_id,
    mapping.evidence_fingerprint,
    mapping.connection_id,
    mapping.asset_key,
    mapping.field_key,
    mapping.baseline_catalog_generation,
    mapping.baseline_catalog_generation_fingerprint,
    mapping.current_catalog_generation,
    mapping.current_catalog_generation_fingerprint,
    mapping.baseline_asset_metadata_fingerprint,
    mapping.current_asset_metadata_fingerprint,
    mapping.baseline_field_metadata_fingerprint,
    mapping.current_field_metadata_fingerprint,
    mapping.baseline_field_definition_fingerprint,
    mapping.current_field_definition_fingerprint,
    mapping.baseline_field_terms_fingerprint,
    mapping.current_field_terms_fingerprint,
    mapping.baseline_normalized_type,
    mapping.current_normalized_type,
    mapping.baseline_nullable,
    mapping.current_nullable,
    mapping.baseline_is_part_of_key,
    mapping.current_is_part_of_key,
    mapping.catalog_evidence_available,
    mapping.catalog_evidence_matches,
    mapping.catalog_evidence_matches AS catalog_generation_covered,
    (
        mapping.context_state IN ('current', 'revalidated', 'rejected')
        AND mapping.dependency_index_complete
        AND mapping.dependency_index_current
        AND mapping.catalog_evidence_matches
        AND (
            mapping.latest_report_id IS NULL
            OR (
                mapping.latest_report_dependency_index_complete
                AND (
                    mapping.latest_impact_state IS NULL
                    OR mapping.latest_impact_state = 'informational'
                )
            )
        )
    ) AS gate_eligible
FROM mapping_gate AS mapping
UNION ALL
SELECT
    head.workspace_id,
    head.catalog_scope,
    head.registry_id,
    head.head_revision,
    head.registry_generation,
    head.registry_version,
    head.registry_fingerprint,
    head.baseline_revision,
    head.state AS context_state,
    head.catalog_generation_vector_fingerprint,
    head.dependency_index_watermark,
    head.dependency_index_fingerprint,
    head.dependency_index_complete,
    'join'::varchar(24) AS dependency_kind,
    profile.contract_id AS dependency_id,
    profile.contract_version AS dependency_version,
    profile.profile_id AS evidence_id,
    profile.profile_fingerprint AS evidence_fingerprint,
    (
        CASE
            WHEN left_binding.connection_id = right_binding.connection_id
            THEN left_binding.connection_id
            ELSE NULL
        END
    )::varchar(200) AS connection_id,
    NULL::char(64) AS asset_key,
    NULL::char(64) AS field_key,
    NULL::bigint AS baseline_catalog_generation,
    NULL::char(64) AS baseline_catalog_generation_fingerprint,
    NULL::bigint AS current_catalog_generation,
    NULL::char(64) AS current_catalog_generation_fingerprint,
    NULL::char(64) AS baseline_asset_metadata_fingerprint,
    NULL::char(64) AS current_asset_metadata_fingerprint,
    NULL::char(64) AS baseline_field_metadata_fingerprint,
    NULL::char(64) AS current_field_metadata_fingerprint,
    NULL::char(64) AS baseline_field_definition_fingerprint,
    NULL::char(64) AS current_field_definition_fingerprint,
    NULL::char(64) AS baseline_field_terms_fingerprint,
    NULL::char(64) AS current_field_terms_fingerprint,
    NULL::varchar(32) AS baseline_normalized_type,
    NULL::varchar(32) AS current_normalized_type,
    NULL::boolean AS baseline_nullable,
    NULL::boolean AS current_nullable,
    NULL::boolean AS baseline_is_part_of_key,
    NULL::boolean AS current_is_part_of_key,
    (
        left_binding.catalog_evidence_available
        AND right_binding.catalog_evidence_available
        AND (
            latest_report.report_id IS NULL
            OR latest_profile.profile_fingerprint IS NOT NULL
        )
    ) AS catalog_evidence_available,
    (
        left_binding.catalog_evidence_matches
        AND right_binding.catalog_evidence_matches
        AND (
            latest_report.report_id IS NULL
            OR latest_profile.safety_fingerprint = profile.policy_fingerprint
        )
    ) AS catalog_evidence_matches,
    (
        left_binding.catalog_evidence_matches
        AND right_binding.catalog_evidence_matches
        AND (
            latest_report.report_id IS NULL
            OR latest_profile.safety_fingerprint = profile.policy_fingerprint
        )
    ) AS catalog_generation_covered,
    (
        head.state IN ('current', 'revalidated', 'rejected')
        AND head.dependency_index_complete
        AND left_binding.dependency_index_current
        AND right_binding.dependency_index_current
        AND left_binding.connection_id = right_binding.connection_id
        AND left_binding.catalog_evidence_matches
        AND right_binding.catalog_evidence_matches
        AND (
            latest_report.report_id IS NULL
            OR (
                latest_report.dependency_index_complete
                AND latest_profile.profile_fingerprint IS NOT NULL
                AND latest_profile.safety_fingerprint = profile.policy_fingerprint
                AND (
                    latest_impact.impact_state IS NULL
                    OR latest_impact.impact_state = 'informational'
                )
            )
        )
    ) AS gate_eligible
FROM schemabridge_control.semantic_change_heads AS head
JOIN schemabridge_control.semantic_join_profiles AS profile
  ON profile.workspace_id = head.workspace_id
 AND profile.catalog_scope = head.catalog_scope
 AND profile.registry_id = head.registry_id
 AND profile.registry_generation = head.registry_generation
 AND profile.registry_fingerprint = head.registry_fingerprint
 AND profile.approved_baseline_revision = head.baseline_revision
 AND profile.profile_state IN ('approved', 'revalidated')
JOIN mapping_gate AS left_binding
  ON left_binding.workspace_id = profile.workspace_id
 AND left_binding.catalog_scope = profile.catalog_scope
 AND left_binding.registry_id = profile.registry_id
 AND left_binding.binding_id = profile.left_binding_id
JOIN mapping_gate AS right_binding
  ON right_binding.workspace_id = profile.workspace_id
 AND right_binding.catalog_scope = profile.catalog_scope
 AND right_binding.registry_id = profile.registry_id
 AND right_binding.binding_id = profile.right_binding_id
LEFT JOIN latest_context_report AS latest_report
  ON latest_report.workspace_id = head.workspace_id
 AND latest_report.catalog_scope = head.catalog_scope
 AND latest_report.registry_id = head.registry_id
LEFT JOIN latest_join_profiles AS latest_profile
  ON latest_profile.workspace_id = latest_report.workspace_id
 AND latest_profile.catalog_scope = latest_report.catalog_scope
 AND latest_profile.registry_id = latest_report.registry_id
 AND latest_profile.report_id = latest_report.report_id
 AND latest_profile.contract_id = profile.contract_id
 AND latest_profile.contract_version = profile.contract_version
LEFT JOIN latest_dependency_impacts AS latest_impact
  ON latest_impact.workspace_id = latest_report.workspace_id
 AND latest_impact.catalog_scope = latest_report.catalog_scope
 AND latest_impact.registry_id = latest_report.registry_id
 AND latest_impact.report_id = latest_report.report_id
 AND latest_impact.artifact_kind = 'join'
 AND latest_impact.artifact_id = profile.contract_id
 AND latest_impact.artifact_version = profile.contract_version;

CREATE VIEW schemabridge_control.semantic_change_report_public
WITH (security_barrier = true)
AS
SELECT
    report.workspace_id,
    report.report_id,
    CASE
        WHEN audited_resolution.resulting_state IS NOT NULL
            AND head.head_revision
                >= audited_resolution.resulting_head_revision
        THEN audited_resolution.resulting_state
        WHEN pointer.workspace_id IS NOT NULL
            AND (
                pointer.generation <> report.registry_generation
                OR pointer.registry_version <> report.registry_version
                OR pointer.registry_fingerprint
                    <> report.registry_fingerprint
                OR pointer.transition_id
                    <> report.pointer_transition_id
            )
        THEN 'superseded'
        ELSE report.outcome
    END AS status,
    report.registry_generation AS pointer_generation,
    report.pointer_fingerprint,
    report.registry_version,
    report.registry_fingerprint,
    jsonb_array_length(
        report.catalog_generation_vector_json -> 'observations'
    ) AS catalog_generation_count,
    report.observation_fingerprint,
    NULLIF(report.baseline_revision, 0) AS baseline_revision,
    report.baseline_fingerprint,
    report.finding_count,
    (report.report_json #>> '{impacts,mapping_count}')::integer
        AS mapping_impact_count,
    (report.report_json #>> '{impacts,join_count}')::integer
        AS join_impact_count,
    (report.report_json #>> '{impacts,workflow_count}')::integer
        AS workflow_impact_count,
    (report.report_json #>> '{impacts,recipe_count}')::integer
        AS recipe_impact_count,
    report.dependency_index_complete AS impacts_complete,
    report.dependency_index_watermark AS dependency_watermark,
    report.impact_set_fingerprint,
    report.inspected_at,
    report.report_fingerprint AS fingerprint
FROM schemabridge_control.semantic_change_reports AS report
LEFT JOIN schemabridge_control.semantic_change_heads AS head
  ON head.workspace_id = report.workspace_id
 AND head.catalog_scope = report.catalog_scope
 AND head.registry_id = report.registry_id
LEFT JOIN schemabridge_control.registry_active_pointers AS pointer
  ON pointer.workspace_id = report.workspace_id
 AND pointer.catalog_scope = report.catalog_scope
 AND pointer.registry_id = report.registry_id
LEFT JOIN LATERAL (
    SELECT
        resolution.resulting_state,
        resolution.resulting_head_revision
    FROM schemabridge_control.semantic_change_resolutions AS resolution
    JOIN schemabridge_control.control_audit_events AS audit
      ON audit.workspace_id = resolution.workspace_id
     AND audit.event_id = resolution.control_audit_event_id
     AND audit.operation = 'semantic_change_decision'
    WHERE resolution.workspace_id = report.workspace_id
      AND resolution.catalog_scope = report.catalog_scope
      AND resolution.registry_id = report.registry_id
      AND resolution.report_id = report.report_id
      AND resolution.report_fingerprint = report.report_fingerprint
    ORDER BY
        resolution.resulting_head_revision DESC,
        resolution.resolution_id DESC
    LIMIT 1
) AS audited_resolution ON true
WHERE report.retain_until > clock_timestamp();

CREATE VIEW schemabridge_control.semantic_change_finding_public
WITH (security_barrier = true)
AS
SELECT
    finding.workspace_id,
    finding.report_id,
    finding.finding_id,
    finding.change_kind AS kind,
    finding.severity,
    CASE
        WHEN finding.finding_json -> 'mapping' IS DISTINCT FROM 'null'::jsonb
        THEN 'mapping'
        ELSE 'join'
    END::varchar(16) AS target_kind,
    CASE
        WHEN finding.finding_json -> 'mapping' IS DISTINCT FROM 'null'::jsonb
        THEN finding.finding_json #>> '{mapping,logical_field}'
        ELSE finding.finding_json #>> '{join,contract_id}'
    END AS target_id,
    CASE
        WHEN finding.finding_json -> 'mapping' IS DISTINCT FROM 'null'::jsonb
        THEN (finding.finding_json #>> '{mapping,version}')::bigint
        ELSE (finding.finding_json #>> '{join,version}')::bigint
    END AS target_version,
    finding.previous_evidence_fingerprint AS previous_fingerprint,
    finding.current_evidence_fingerprint AS current_fingerprint,
    finding.risk_codes AS risks,
    finding.finding_fingerprint AS fingerprint
FROM schemabridge_control.semantic_change_findings AS finding
JOIN schemabridge_control.semantic_change_reports AS report
  ON report.workspace_id = finding.workspace_id
 AND report.report_id = finding.report_id
WHERE report.retain_until > clock_timestamp();

CREATE VIEW schemabridge_control.semantic_change_impact_public
WITH (security_barrier = true)
AS
SELECT
    impact.workspace_id,
    impact.report_id,
    impact.impact_sort_key,
    impact.artifact_kind AS kind,
    impact.artifact_id,
    impact.artifact_version,
    impact.finding_ids_json AS finding_ids,
    impact.impact_fingerprint AS fingerprint
FROM schemabridge_control.semantic_change_impacts AS impact
JOIN schemabridge_control.semantic_change_reports AS report
  ON report.workspace_id = impact.workspace_id
 AND report.report_id = impact.report_id
WHERE report.retain_until > clock_timestamp();

INSERT INTO schemabridge_control.semantic_change_scan_requests (
    scan_id,
    workspace_id,
    source_kind,
    source_event_key,
    source_fingerprint,
    catalog_scope,
    registry_id,
    registry_generation,
    connection_id,
    base_catalog_generation,
    observed_catalog_generation,
    status,
    attempts,
    available_at,
    fencing_token,
    requested_at,
    updated_at
)
SELECT
    'scan_' || seed.fingerprint,
    seed.workspace_id,
    'catalog_generation',
    seed.source_event_key,
    seed.fingerprint,
    seed.catalog_scope,
    seed.registry_id,
    NULL,
    seed.connection_id,
    NULL,
    seed.active_generation,
    'requested',
    0,
    seed.observed_at,
    0,
    seed.observed_at,
    seed.observed_at
FROM (
    SELECT
        connection.workspace_id,
        connection.connection_id,
        connection.active_generation,
        pointer.catalog_scope,
        pointer.registry_id,
        concat_ws(
            ':',
            connection.connection_id,
            connection.active_generation::text,
            pointer.catalog_scope,
            pointer.registry_id
        ) AS source_event_key,
        coalesce(
            connection.active_generation_completed_at,
            clock_timestamp()
        ) AS observed_at,
        encode(
            sha256(
                convert_to(
                    concat_ws(
                        '|',
                        'catalog_generation_scan_v2',
                        connection.workspace_id,
                        concat_ws(
                            ':',
                            connection.connection_id,
                            connection.active_generation::text,
                            pointer.catalog_scope,
                            pointer.registry_id
                        ),
                        connection.active_generation_fingerprint,
                        pointer.generation::text,
                        pointer.registry_fingerprint
                    ),
                    'UTF8'
                )
            ),
            'hex'
        ) AS fingerprint
    FROM schemabridge_control.catalog_connections AS connection
    JOIN schemabridge_control.registry_active_pointers AS pointer
      ON pointer.workspace_id = connection.workspace_id
     AND pointer.catalog_scope = connection.catalog_scope
    WHERE connection.active_generation IS NOT NULL
) AS seed
ON CONFLICT (workspace_id, source_kind, source_event_key) DO NOTHING;

INSERT INTO schemabridge_control.semantic_change_scan_requests (
    scan_id,
    workspace_id,
    source_kind,
    source_event_key,
    source_fingerprint,
    catalog_scope,
    registry_id,
    registry_generation,
    connection_id,
    base_catalog_generation,
    observed_catalog_generation,
    status,
    attempts,
    available_at,
    fencing_token,
    requested_at,
    updated_at
)
SELECT
    'scan_' || seed.fingerprint,
    seed.workspace_id,
    'registry_pointer',
    seed.transition_id,
    seed.fingerprint,
    seed.catalog_scope,
    seed.registry_id,
    seed.generation,
    NULL,
    NULL,
    NULL,
    'requested',
    0,
    seed.observed_at,
    0,
    seed.observed_at,
    seed.observed_at
FROM (
    SELECT
        pointer.workspace_id,
        pointer.catalog_scope,
        pointer.registry_id,
        pointer.generation,
        pointer.registry_fingerprint,
        pointer.transition_id,
        coalesce(pointer.activated_at, clock_timestamp()) AS observed_at,
        encode(
            sha256(
                convert_to(
                    concat_ws(
                        '|',
                        'registry_pointer_scan_v1',
                        pointer.workspace_id,
                        pointer.catalog_scope,
                        pointer.registry_id,
                        pointer.generation::text,
                        pointer.registry_fingerprint,
                        pointer.transition_id
                    ),
                    'UTF8'
                )
            ),
            'hex'
        ) AS fingerprint
    FROM schemabridge_control.registry_active_pointers AS pointer
) AS seed
ON CONFLICT (workspace_id, source_kind, source_event_key) DO NOTHING;

REVOKE ALL ON
    schemabridge_control.catalog_generation_changes,
    schemabridge_control.semantic_resource_bindings,
    schemabridge_control.semantic_join_profiles,
    schemabridge_control.semantic_change_reports,
    schemabridge_control.semantic_change_findings,
    schemabridge_control.semantic_change_impacts,
    schemabridge_control.semantic_change_resolutions,
    schemabridge_control.semantic_artifact_dependencies,
    schemabridge_control.semantic_dependency_index_states,
    schemabridge_control.semantic_change_heads,
    schemabridge_control.semantic_change_scan_requests,
    schemabridge_control.semantic_join_profile_jobs,
    schemabridge_control.semantic_catalog_evidence_projection,
    schemabridge_control.semantic_catalog_connection_evidence_projection,
    schemabridge_control.semantic_context_gate_projection,
    schemabridge_control.semantic_change_report_public,
    schemabridge_control.semantic_change_finding_public,
    schemabridge_control.semantic_change_impact_public
    FROM PUBLIC;

REVOKE ALL ON FUNCTION
    schemabridge_control.semantic_catalog_asset_locator_key(
        varchar,
        varchar,
        bigint,
        varchar
    ),
    schemabridge_control.semantic_catalog_field_locator_key(
        varchar,
        varchar,
        bigint,
        char,
        char
    ),
    schemabridge_control.semantic_definition_fingerprint(varchar),
    schemabridge_control.semantic_terms_fingerprint(varchar[], varchar[]),
    schemabridge_control.semantic_json_has_protected_keys(jsonb),
    schemabridge_control.reject_semantic_immutable_mutation(),
    schemabridge_control.guard_semantic_change_head(),
    schemabridge_control.guard_semantic_change_scan(),
    schemabridge_control.guard_semantic_join_profile_job(),
    schemabridge_control.load_semantic_initial_catalog_candidates(
        varchar,
        varchar,
        jsonb
    ),
    schemabridge_control.load_semantic_bound_catalog_evidence(
        varchar,
        varchar,
        jsonb
    ),
    schemabridge_control.capture_catalog_generation_change(),
    schemabridge_control.enqueue_registry_semantic_scan()
    FROM PUBLIC;

GRANT SELECT ON
    schemabridge_control.catalog_generation_changes,
    schemabridge_control.semantic_resource_bindings,
    schemabridge_control.semantic_join_profiles,
    schemabridge_control.semantic_change_reports,
    schemabridge_control.semantic_change_findings,
    schemabridge_control.semantic_change_impacts,
    schemabridge_control.semantic_change_resolutions,
    schemabridge_control.semantic_artifact_dependencies,
    schemabridge_control.semantic_dependency_index_states,
    schemabridge_control.semantic_change_heads,
    schemabridge_control.semantic_change_scan_requests,
    schemabridge_control.semantic_join_profile_jobs,
    schemabridge_control.semantic_catalog_evidence_projection,
    schemabridge_control.semantic_catalog_connection_evidence_projection
    TO schemabridge_reconciler;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_semantic_initial_catalog_candidates(
        varchar,
        varchar,
        jsonb
    ),
    schemabridge_control.load_semantic_bound_catalog_evidence(
        varchar,
        varchar,
        jsonb
    )
    TO schemabridge_reconciler;

GRANT SELECT (
    workspace_id,
    id,
    revision,
    payload,
    updated_at
) ON schemabridge_control.agent_workflow_drafts
    TO schemabridge_reconciler;

GRANT INSERT ON
    schemabridge_control.semantic_resource_bindings,
    schemabridge_control.semantic_join_profiles,
    schemabridge_control.semantic_change_reports,
    schemabridge_control.semantic_change_findings,
    schemabridge_control.semantic_change_impacts,
    schemabridge_control.semantic_change_resolutions,
    schemabridge_control.semantic_artifact_dependencies,
    schemabridge_control.semantic_dependency_index_states,
    schemabridge_control.semantic_change_heads,
    schemabridge_control.semantic_join_profile_jobs
    TO schemabridge_reconciler;

GRANT UPDATE ON
    schemabridge_control.semantic_change_heads,
    schemabridge_control.semantic_dependency_index_states
    TO schemabridge_reconciler;

GRANT UPDATE (
    status,
    attempts,
    available_at,
    lease_owner_id,
    lease_capability_digest,
    fencing_token,
    lease_acquired_at,
    lease_heartbeat_at,
    lease_expires_at,
    last_reason_code,
    updated_at,
    completed_at,
    completed_report_id,
    completed_report_fingerprint,
    superseded_by_scan_id,
    superseded_by_scan_fingerprint,
    retain_until
) ON schemabridge_control.semantic_change_scan_requests
    TO schemabridge_reconciler;

GRANT SELECT ON
    schemabridge_control.semantic_join_profile_jobs
    TO schemabridge_worker;

GRANT UPDATE (
    status,
    attempt_count,
    available_at,
    lease_owner_id,
    lease_capability_digest,
    fencing_token,
    lease_acquired_at,
    lease_heartbeat_at,
    lease_expires_at,
    failure_code,
    result_profile_json,
    result_profile_fingerprint,
    updated_at,
    completed_at,
    retain_until
) ON schemabridge_control.semantic_join_profile_jobs
    TO schemabridge_worker;

GRANT SELECT ON
    schemabridge_control.semantic_context_gate_projection
    TO schemabridge_runtime, schemabridge_worker;

GRANT SELECT ON
    schemabridge_control.semantic_change_report_public,
    schemabridge_control.semantic_change_finding_public,
    schemabridge_control.semantic_change_impact_public
    TO schemabridge_api;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.semantic_catalog_asset_locator_key(
        varchar,
        varchar,
        bigint,
        varchar
    ),
    schemabridge_control.semantic_catalog_field_locator_key(
        varchar,
        varchar,
        bigint,
        char,
        char
    )
    TO schemabridge_catalog;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.semantic_definition_fingerprint(varchar),
    schemabridge_control.semantic_terms_fingerprint(varchar[], varchar[]),
    schemabridge_control.semantic_json_has_protected_keys(jsonb),
    schemabridge_control.reject_semantic_immutable_mutation(),
    schemabridge_control.guard_semantic_change_head(),
    schemabridge_control.guard_semantic_change_scan(),
    schemabridge_control.guard_semantic_join_profile_job(),
    schemabridge_control.capture_catalog_generation_change(),
    schemabridge_control.enqueue_registry_semantic_scan()
    TO schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.semantic_definition_fingerprint(varchar),
    schemabridge_control.semantic_terms_fingerprint(varchar[], varchar[])
    TO schemabridge_runtime, schemabridge_worker;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.semantic_json_has_protected_keys(jsonb)
    TO schemabridge_worker;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.semantic_definition_fingerprint(varchar),
    schemabridge_control.semantic_terms_fingerprint(varchar[], varchar[]),
    schemabridge_control.semantic_json_has_protected_keys(jsonb)
    TO schemabridge_reconciler;
