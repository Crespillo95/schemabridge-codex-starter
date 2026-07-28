"""Approval-gated DataHub SDK/aspect write-back and independent read-back."""

from __future__ import annotations

import json
import logging
import re
import stat
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from schemabridge.adapters.datahub.canonical_urns import (
    CUSTOMER_KEY_TERM_URN,
    DECISION_PROPERTY_URN,
    LOGICAL_CUSTOMER_URN,
    REGISTRATION_DATE_TERM_URN,
)
from schemabridge.adapters.datahub.decision_property import ensure_decision_property
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.reviews import (
    CanonicalPublication,
    PublicationApproval,
    PublicationDecisionRef,
    PublicationItemKind,
    PublicationItemResult,
    PublicationItemStatus,
    PublicationResult,
    PublicationStatus,
    PublishedCanonicalContext,
)

logger = logging.getLogger(__name__)

_PUBLICATION_FINGERPRINT_PROPERTY = "schemabridge.publicationFingerprint"
_LOGICAL_MODEL_FINGERPRINT_PROPERTY = "schemabridge.logicalModelFingerprint"


@dataclass(frozen=True, slots=True)
class DataHubWriteConfig:
    server: str
    token: str
    actor_urn: str


@dataclass(frozen=True, slots=True)
class _TargetState:
    previous_fingerprint: str | None
    is_current: bool


class DataHubCatalogWriteAdapter:
    """Write only a validated publication, recording its fingerprint last."""

    def __init__(self, config: DataHubWriteConfig) -> None:
        self._config = config

    @classmethod
    def from_env_file(cls, path: Path) -> DataHubCatalogWriteAdapter:
        if not path.is_file():
            raise ReviewWorkflowError(
                ReviewErrorCode.CATALOG_UNAVAILABLE,
                "DataHub writer credential is absent; run make datahub-provision-writer",
            )
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ReviewWorkflowError(
                ReviewErrorCode.CATALOG_PERMISSION_DENIED,
                "DataHub writer credential must be accessible only to its owner",
            )
        values = _read_env_file(path)
        try:
            config = DataHubWriteConfig(
                server=values["DATAHUB_GMS_URL"].rstrip("/"),
                token=values["DATAHUB_GMS_TOKEN"],
                actor_urn=values["DATAHUB_WRITER_ACTOR_URN"],
            )
        except KeyError as error:
            raise ReviewWorkflowError(
                ReviewErrorCode.CATALOG_UNAVAILABLE,
                "DataHub writer credential is incomplete",
            ) from error
        if not config.server.startswith(("http://", "https://")) or not config.token:
            raise ReviewWorkflowError(
                ReviewErrorCode.CATALOG_UNAVAILABLE,
                "DataHub writer credential is invalid",
            )
        return cls(config)

    def publish(
        self,
        publication: CanonicalPublication,
        approval: PublicationApproval,
    ) -> PublicationResult:
        _validate_approval(publication, approval)
        try:
            self._verify_runtime_identity()
            client = self._client()
            actions = self._actions(client, publication)
            target_states = {
                (kind, target): self._target_state(
                    client,
                    publication,
                    kind,
                    target,
                )
                for kind, target, _action in actions
            }
        except ReviewWorkflowError:
            raise
        except Exception as error:
            code = _review_error_code(error)
            raise ReviewWorkflowError(code, "DataHub canonical target state read failed") from error

        document_state = next(
            state
            for (kind, _target), state in target_states.items()
            if kind is PublicationItemKind.DECISION_DOCUMENT
        )
        if (
            document_state.previous_fingerprint is not None
            and document_state.previous_fingerprint != publication.fingerprint
        ):
            raise ReviewWorkflowError(
                ReviewErrorCode.CONFLICT,
                "DataHub immutable canonical decision document identifies different content",
            )

        if all(
            state.is_current and state.previous_fingerprint == publication.fingerprint
            for state in target_states.values()
        ):
            return _all_current(publication, approval, target_states)

        refs = _decision_refs(publication)
        results: list[PublicationItemResult] = []
        failed = False
        for kind, target, action in actions:
            if failed:
                previous_fingerprint = target_states[(kind, target)].previous_fingerprint
                results.append(
                    _item(
                        kind,
                        target,
                        PublicationItemStatus.NOT_ATTEMPTED,
                        refs,
                        publication,
                        approval,
                        previous_fingerprint,
                        reason_code="prior_item_failed",
                    )
                )
                continue
            try:
                state = self._target_state(client, publication, kind, target)
            except Exception as error:
                failed = True
                previous_fingerprint = target_states[(kind, target)].previous_fingerprint
                reason = _reason_code(error)
                results.append(
                    _item(
                        kind,
                        target,
                        PublicationItemStatus.FAILED,
                        refs,
                        publication,
                        approval,
                        previous_fingerprint,
                        reason_code=reason,
                    )
                )
                continue
            previous_fingerprint = state.previous_fingerprint
            if state.is_current and previous_fingerprint == publication.fingerprint:
                results.append(
                    _item(
                        kind,
                        target,
                        PublicationItemStatus.ALREADY_CURRENT,
                        refs,
                        publication,
                        approval,
                        previous_fingerprint,
                    )
                )
                continue
            logger.info("datahub_publish operation=%s target=%s", kind.value, target)
            try:
                action()
                resulting_state = self._target_state(
                    client,
                    publication,
                    kind,
                    target,
                )
                if not resulting_state.is_current:
                    raise ValueError("DataHub canonical target did not match its approved payload")
            except Exception as error:  # external SDK and transport exceptions are normalized here
                failed = True
                reason = _reason_code(error)
                logger.warning(
                    "datahub_publish_failed operation=%s target=%s code=%s",
                    kind.value,
                    target,
                    reason,
                )
                results.append(
                    _item(
                        kind,
                        target,
                        PublicationItemStatus.FAILED,
                        refs,
                        publication,
                        approval,
                        previous_fingerprint,
                        reason_code=reason,
                    )
                )
            else:
                results.append(
                    _item(
                        kind,
                        target,
                        PublicationItemStatus.PUBLISHED,
                        refs,
                        publication,
                        approval,
                        previous_fingerprint,
                    )
                )
        return PublicationResult(
            approval_id=approval.id,
            draft_id=publication.draft_id,
            fingerprint=publication.fingerprint,
            status=(PublicationStatus.PARTIAL_FAILURE if failed else PublicationStatus.PUBLISHED),
            items=tuple(results),
        )

    def read_context(
        self,
        publication: CanonicalPublication,
    ) -> PublishedCanonicalContext | None:
        """Retrieve proof from DataHub; never infer current state from a local attempt."""

        try:
            from datahub.metadata.schema_classes import (
                DatasetPropertiesClass,
                DocumentInfoClass,
                GlossaryTermInfoClass,
                GlossaryTermsClass,
                LogicalParentClass,
                SchemaMetadataClass,
                StructuredPropertiesClass,
            )

            self._verify_runtime_identity()
            client = self._client()
            graph = client._graph
            properties = graph.get_aspect(LOGICAL_CUSTOMER_URN, DatasetPropertiesClass)
            expected_properties = _decision_properties(publication)
            if (
                properties is None
                or properties.customProperties.get("schemabridge.publicationFingerprint")
                != publication.fingerprint
                or any(
                    properties.customProperties.get(key) != value
                    for key, value in expected_properties.items()
                )
            ):
                return None
            if not _has_decision_property(
                graph, LOGICAL_CUSTOMER_URN, StructuredPropertiesClass, publication
            ):
                return None
            schema = graph.get_aspect(LOGICAL_CUSTOMER_URN, SchemaMetadataClass)
            expected_fields = {field.id.root.rsplit(".", 1)[1] for field in publication.fields}
            if schema is None or {field.fieldPath for field in schema.fields} != expected_fields:
                return None
            terms = graph.get_aspect(LOGICAL_CUSTOMER_URN, GlossaryTermsClass)
            expected_terms = {CUSTOMER_KEY_TERM_URN, REGISTRATION_DATE_TERM_URN}
            if terms is None or {term.urn for term in terms.terms} != expected_terms:
                return None
            datasets = _physical_dataset_names(publication)
            for dataset in datasets:
                parent = graph.get_aspect(_physical_urn(dataset), LogicalParentClass)
                if parent is None or parent.parent is None:
                    return None
                if parent.parent.destinationUrn != LOGICAL_CUSTOMER_URN:
                    return None
                for mapping in publication.mappings:
                    if mapping.physical_field.root.rsplit(".", 1)[0] != dataset:
                        continue
                    physical_field_urn = _schema_field_urn(
                        _physical_urn(dataset),
                        mapping.physical_field.root.rsplit(".", 1)[1],
                    )
                    logical_field_urn = _schema_field_urn(
                        LOGICAL_CUSTOMER_URN,
                        mapping.logical_field.root.rsplit(".", 1)[1],
                    )
                    field_parent = graph.get_aspect(physical_field_urn, LogicalParentClass)
                    if field_parent is None or field_parent.parent is None:
                        return None
                    if field_parent.parent.destinationUrn != logical_field_urn:
                        return None
            document_urn = _document_urn(publication)
            required_entities = (
                DECISION_PROPERTY_URN,
                CUSTOMER_KEY_TERM_URN,
                REGISTRATION_DATE_TERM_URN,
                document_urn,
            )
            if any(not graph.exists(urn) for urn in required_entities):
                return None
            for term_urn in expected_terms:
                term_info = graph.get_aspect(term_urn, GlossaryTermInfoClass)
                if term_info is None or any(
                    term_info.customProperties.get(key) != value
                    for key, value in expected_properties.items()
                ):
                    return None
                if not _has_decision_property(
                    graph, term_urn, StructuredPropertiesClass, publication
                ):
                    return None
            document = graph.get_aspect(document_urn, DocumentInfoClass)
            expected_assets = {
                LOGICAL_CUSTOMER_URN,
                *(_physical_urn(dataset) for dataset in datasets),
            }
            if (
                document is None
                or any(
                    document.customProperties.get(key) != value
                    for key, value in expected_properties.items()
                )
                or {asset.asset for asset in document.relatedAssets or ()} != expected_assets
            ):
                return None
            if not _has_decision_property(
                graph, document_urn, StructuredPropertiesClass, publication
            ):
                return None
            return PublishedCanonicalContext(
                logical_model_urn=LOGICAL_CUSTOMER_URN,
                fingerprint=publication.fingerprint,
                fields=tuple(field.id for field in publication.fields),
                physical_links=datasets,
                glossary_terms=tuple(sorted(expected_terms)),
                decision_document_urn=document_urn,
                decision_refs=_decision_refs(publication),
            )
        except ReviewWorkflowError:
            raise
        except Exception as error:
            code = _review_error_code(error)
            raise ReviewWorkflowError(code, "DataHub canonical-context read failed") from error

    def _target_state(
        self,
        client: Any,
        publication: CanonicalPublication,
        kind: PublicationItemKind,
        target: str,
    ) -> _TargetState:
        """Read the state of the exact target; never infer it from the current marker."""

        if kind is PublicationItemKind.STRUCTURED_PROPERTY:
            current = self._decision_property_is_current()
            return _TargetState(
                previous_fingerprint=None,
                is_current=current,
            )
        if kind is PublicationItemKind.GLOSSARY_TERM:
            return self._term_state(client, publication, target)
        if kind is PublicationItemKind.LOGICAL_MODEL:
            return self._logical_model_state(client, publication)
        if kind is PublicationItemKind.PHYSICAL_LINK:
            current = self._link_is_current(client, publication, target)
            return _TargetState(
                previous_fingerprint=None,
                is_current=current,
            )
        if kind is PublicationItemKind.DECISION_DOCUMENT:
            return self._document_state(client, publication, target)
        if kind is PublicationItemKind.PUBLICATION_MARKER:
            return self._marker_state(client, publication)
        raise ValueError(f"unsupported canonical publication target kind: {kind}")

    def _decision_property_is_current(self) -> bool:
        existing = self._graphql(
            """
            query CanonicalTargetProperty($urn: String!) {
              structuredProperty(urn: $urn) { urn definition { qualifiedName } }
            }
            """,
            {"urn": DECISION_PROPERTY_URN},
        ).get("structuredProperty")
        return (
            isinstance(existing, dict)
            and existing.get("urn") == DECISION_PROPERTY_URN
            and isinstance(existing.get("definition"), dict)
            and existing["definition"].get("qualifiedName") == "io.schemabridge.decisionRef"
        )

    @staticmethod
    def _term_state(
        client: Any,
        publication: CanonicalPublication,
        target: str,
    ) -> _TargetState:
        from datahub.metadata.schema_classes import (
            GlossaryTermInfoClass,
            StructuredPropertiesClass,
        )

        field = next(
            (
                candidate
                for candidate in publication.fields
                if _term_urn(candidate.id.root) == target
            ),
            None,
        )
        if field is None:
            raise ValueError("canonical glossary target is not present in the publication")
        graph = client._graph
        info = graph.get_aspect(target, GlossaryTermInfoClass)
        previous = _aspect_fingerprint(info, _PUBLICATION_FINGERPRINT_PROPERTY)
        expected_properties = _decision_properties(publication)
        current = (
            info is not None
            and previous == publication.fingerprint
            and info.name == field.canonical_name.replace("_", " ").title()
            and info.definition == field.definition
            and all(
                info.customProperties.get(key) == value
                for key, value in expected_properties.items()
            )
            and _has_decision_property(
                graph,
                target,
                StructuredPropertiesClass,
                publication,
            )
        )
        return _TargetState(previous_fingerprint=previous, is_current=current)

    @staticmethod
    def _logical_model_state(
        client: Any,
        publication: CanonicalPublication,
    ) -> _TargetState:
        from datahub.metadata.schema_classes import (
            DatasetPropertiesClass,
            EditableDatasetPropertiesClass,
            GlossaryTermsClass,
            SchemaMetadataClass,
            StructuredPropertiesClass,
        )

        graph = client._graph
        properties = graph.get_aspect(LOGICAL_CUSTOMER_URN, DatasetPropertiesClass)
        previous = _aspect_fingerprint(properties, _LOGICAL_MODEL_FINGERPRINT_PROPERTY)
        editable = graph.get_aspect(LOGICAL_CUSTOMER_URN, EditableDatasetPropertiesClass)
        schema = graph.get_aspect(LOGICAL_CUSTOMER_URN, SchemaMetadataClass)
        terms = graph.get_aspect(LOGICAL_CUSTOMER_URN, GlossaryTermsClass)
        expected_properties = _decision_properties(publication)
        expected_fields = {field.id.root.rsplit(".", 1)[1] for field in publication.fields}
        expected_terms = {CUSTOMER_KEY_TERM_URN, REGISTRATION_DATE_TERM_URN}
        current = (
            properties is not None
            and previous == publication.fingerprint
            and editable is not None
            and editable.description == publication.logical_model.description
            and all(
                properties.customProperties.get(key) == value
                for key, value in expected_properties.items()
            )
            and schema is not None
            and {field.fieldPath for field in schema.fields} == expected_fields
            and terms is not None
            and {term.urn for term in terms.terms} == expected_terms
            and _has_decision_property(
                graph,
                LOGICAL_CUSTOMER_URN,
                StructuredPropertiesClass,
                publication,
            )
        )
        return _TargetState(previous_fingerprint=previous, is_current=current)

    @staticmethod
    def _link_is_current(
        client: Any,
        publication: CanonicalPublication,
        dataset: str,
    ) -> bool:
        from datahub.metadata.schema_classes import LogicalParentClass

        graph = client._graph
        physical_urn = _physical_urn(dataset)
        parent = graph.get_aspect(physical_urn, LogicalParentClass)
        if (
            parent is None
            or parent.parent is None
            or parent.parent.destinationUrn != LOGICAL_CUSTOMER_URN
        ):
            return False
        for mapping in publication.mappings:
            if mapping.physical_field.root.rsplit(".", 1)[0] != dataset:
                continue
            physical_field_urn = _schema_field_urn(
                physical_urn,
                mapping.physical_field.root.rsplit(".", 1)[1],
            )
            logical_field_urn = _schema_field_urn(
                LOGICAL_CUSTOMER_URN,
                mapping.logical_field.root.rsplit(".", 1)[1],
            )
            field_parent = graph.get_aspect(physical_field_urn, LogicalParentClass)
            if (
                field_parent is None
                or field_parent.parent is None
                or field_parent.parent.destinationUrn != logical_field_urn
            ):
                return False
        return True

    @staticmethod
    def _document_state(
        client: Any,
        publication: CanonicalPublication,
        target: str,
    ) -> _TargetState:
        from datahub.metadata.schema_classes import (
            DocumentInfoClass,
            StructuredPropertiesClass,
        )

        graph = client._graph
        document = graph.get_aspect(target, DocumentInfoClass)
        previous = _aspect_fingerprint(document, _PUBLICATION_FINGERPRINT_PROPERTY)
        expected_properties = _decision_properties(publication)
        expected_assets = {
            LOGICAL_CUSTOMER_URN,
            *(_physical_urn(dataset) for dataset in _physical_dataset_names(publication)),
        }
        current = (
            document is not None
            and previous == publication.fingerprint
            and all(
                document.customProperties.get(key) == value
                for key, value in expected_properties.items()
            )
            and {asset.asset for asset in document.relatedAssets or ()} == expected_assets
            and _has_decision_property(
                graph,
                target,
                StructuredPropertiesClass,
                publication,
            )
        )
        return _TargetState(previous_fingerprint=previous, is_current=current)

    @staticmethod
    def _marker_state(
        client: Any,
        publication: CanonicalPublication,
    ) -> _TargetState:
        from datahub.metadata.schema_classes import DatasetPropertiesClass

        properties = client._graph.get_aspect(LOGICAL_CUSTOMER_URN, DatasetPropertiesClass)
        previous = _aspect_fingerprint(properties, _PUBLICATION_FINGERPRINT_PROPERTY)
        return _TargetState(
            previous_fingerprint=previous,
            is_current=previous == publication.fingerprint,
        )

    def _client(self) -> Any:
        try:
            from datahub.sdk.main_client import DataHubClient

            return DataHubClient(server=self._config.server, token=self._config.token)
        except ModuleNotFoundError as error:
            raise ReviewWorkflowError(
                ReviewErrorCode.CATALOG_UNAVAILABLE,
                "DataHub support is not installed; install schemabridge[datahub]",
            ) from error

    def _verify_runtime_identity(self) -> None:
        query = """
        query VerifyWriterRuntime {
          me {
            corpUser { urn }
            platformPrivileges {
              managePolicies manageIdentities manageIngestion manageSecrets manageTokens
              manageServiceAccounts manageGlossaries manageDocuments
              manageStructuredProperties viewStructuredPropertiesPage
            }
          }
        }
        """
        me = self._graphql(query, {}).get("me")
        if not isinstance(me, dict):
            raise ReviewWorkflowError(
                ReviewErrorCode.CATALOG_PERMISSION_DENIED,
                "DataHub writer identity could not be verified",
            )
        corp_user = me.get("corpUser")
        privileges = me.get("platformPrivileges")
        if (
            not isinstance(corp_user, dict)
            or corp_user.get("urn") != self._config.actor_urn
            or not isinstance(privileges, dict)
        ):
            raise ReviewWorkflowError(
                ReviewErrorCode.CATALOG_PERMISSION_DENIED,
                "DataHub writer identity does not match its bounded configuration",
            )
        required = {
            "manageGlossaries",
            "manageDocuments",
            "manageStructuredProperties",
            "viewStructuredPropertiesPage",
        }
        forbidden = {
            "managePolicies",
            "manageIdentities",
            "manageIngestion",
            "manageSecrets",
            "manageTokens",
            "manageServiceAccounts",
        }
        if any(not privileges.get(name) for name in required) or any(
            privileges.get(name) for name in forbidden
        ):
            raise ReviewWorkflowError(
                ReviewErrorCode.CATALOG_PERMISSION_DENIED,
                "DataHub writer privileges do not match the bounded configuration",
            )

    def _actions(
        self,
        client: Any,
        publication: CanonicalPublication,
    ) -> tuple[tuple[PublicationItemKind, str, Callable[[], None]], ...]:
        actions: list[tuple[PublicationItemKind, str, Callable[[], None]]] = [
            (
                PublicationItemKind.STRUCTURED_PROPERTY,
                DECISION_PROPERTY_URN,
                self._ensure_decision_property,
            )
        ]
        for field in publication.fields:
            term_urn = _term_urn(field.id.root)
            actions.append(
                (
                    PublicationItemKind.GLOSSARY_TERM,
                    term_urn,
                    partial(self._upsert_term, client, publication, field),
                )
            )
        actions.append(
            (
                PublicationItemKind.LOGICAL_MODEL,
                LOGICAL_CUSTOMER_URN,
                lambda: self._upsert_logical_model(client, publication, marker=False),
            )
        )
        for dataset in _physical_dataset_names(publication):
            actions.append(
                (
                    PublicationItemKind.PHYSICAL_LINK,
                    dataset,
                    partial(self._upsert_link, client, publication, dataset),
                )
            )
        actions.extend(
            (
                (
                    PublicationItemKind.DECISION_DOCUMENT,
                    _document_urn(publication),
                    lambda: self._upsert_document(client, publication),
                ),
                (
                    PublicationItemKind.PUBLICATION_MARKER,
                    LOGICAL_CUSTOMER_URN,
                    lambda: self._upsert_logical_model(client, publication, marker=True),
                ),
            )
        )
        return tuple(actions)

    def _ensure_decision_property(self) -> None:
        ensure_decision_property(self._graphql)

    def _upsert_term(self, client: Any, publication: CanonicalPublication, field: Any) -> None:
        from datahub.sdk.glossary_term import GlossaryTerm

        term = GlossaryTerm(
            id=f"SchemaBridge.{field.id.root}",
            display_name=field.canonical_name.replace("_", " ").title(),
            definition=field.definition,
            custom_properties={
                **_decision_properties(publication),
                _PUBLICATION_FINGERPRINT_PROPERTY: publication.fingerprint,
            },
        )
        term.set_structured_property(DECISION_PROPERTY_URN, _decision_values(publication))
        client.entities.upsert(term)

    def _upsert_logical_model(
        self,
        client: Any,
        publication: CanonicalPublication,
        *,
        marker: bool,
    ) -> None:
        from datahub.sdk.dataset import Dataset

        custom = _decision_properties(publication)
        custom[_LOGICAL_MODEL_FINGERPRINT_PROPERTY] = publication.fingerprint
        if marker:
            custom[_PUBLICATION_FINGERPRINT_PROPERTY] = publication.fingerprint
        schema = [
            (
                field.canonical_name,
                _native_type(field.canonical_type.value),
                field.definition,
            )
            for field in publication.fields
        ]
        dataset = Dataset(
            platform="logical",
            name="schemabridge.Customer",
            description=publication.logical_model.description,
            display_name="Customer",
            schema=schema,
            terms=(CUSTOMER_KEY_TERM_URN, REGISTRATION_DATE_TERM_URN),
            custom_properties=custom,
            structured_properties={
                DECISION_PROPERTY_URN: _decision_values(publication),
            },
        )
        for schema_field in dataset.schema:
            schema_field.set_terms((_term_urn(f"Customer.{schema_field.field_path}"),))
        client.entities.upsert(dataset)

    def _upsert_link(
        self,
        client: Any,
        publication: CanonicalPublication,
        dataset: str,
    ) -> None:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import EdgeClass, LogicalParentClass

        column_map = {
            mapping.logical_field.root.rsplit(".", 1)[1]: mapping.physical_field.root.rsplit(
                ".", 1
            )[1]
            for mapping in publication.mappings
            if mapping.physical_field.root.rsplit(".", 1)[0] == dataset
        }
        physical_urn = _physical_urn(dataset)
        client._graph.emit(
            MetadataChangeProposalWrapper(
                entityUrn=physical_urn,
                aspect=LogicalParentClass(parent=EdgeClass(destinationUrn=LOGICAL_CUSTOMER_URN)),
            )
        )
        for logical_field, physical_field in column_map.items():
            client._graph.emit(
                MetadataChangeProposalWrapper(
                    entityUrn=_schema_field_urn(physical_urn, physical_field),
                    aspect=LogicalParentClass(
                        parent=EdgeClass(
                            destinationUrn=_schema_field_urn(LOGICAL_CUSTOMER_URN, logical_field)
                        )
                    ),
                )
            )

    def _upsert_document(self, client: Any, publication: CanonicalPublication) -> None:
        from datahub.sdk.document import Document

        mappings = "\n".join(
            f"- `{mapping.physical_field.root}` → `{mapping.logical_field.root}` "
            f"(mapping v{mapping.version})"
            for mapping in publication.mappings
        )
        decisions = "\n".join(
            f"- `{decision.id}`: {decision.action.value}, resulting v{decision.resulting_version}"
            for decision in publication.decisions
        )
        document = Document.create_document(
            id=f"schemabridge-{publication.draft_id}-v{publication.draft_version}",
            title=f"SchemaBridge Customer decision v{publication.draft_version}",
            text=(
                f"# Approved Customer canonical context\n\n"
                f"Payload fingerprint: `{publication.fingerprint}`\n\n"
                f"## Approved mappings\n{mappings}\n\n"
                f"## Versioned decisions\n{decisions}\n"
            ),
            related_assets=(
                LOGICAL_CUSTOMER_URN,
                *(_physical_urn(name) for name in _physical_dataset_names(publication)),
            ),
            custom_properties={
                **_decision_properties(publication),
                _PUBLICATION_FINGERPRINT_PROPERTY: publication.fingerprint,
            },
            structured_properties={
                DECISION_PROPERTY_URN: _decision_values(publication),
            },
        )
        client.entities.upsert(document)

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._config.server}/api/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={
                "Authorization": f"Bearer {self._config.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or payload.get("errors"):
            raise ValueError("DataHub GraphQL returned an error")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("DataHub GraphQL returned no data")
        return data


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key:
            values[key] = value
    return values


def _aspect_fingerprint(aspect: Any, property_name: str) -> str | None:
    if aspect is None:
        return None
    custom_properties = getattr(aspect, "customProperties", None)
    if not isinstance(custom_properties, dict):
        raise ValueError("DataHub canonical target custom properties are invalid")
    fingerprint = custom_properties.get(property_name)
    if fingerprint is None:
        return None
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("DataHub canonical target fingerprint is invalid")
    return fingerprint


def _has_decision_property(
    graph: Any,
    urn: str,
    aspect_type: Any,
    publication: CanonicalPublication,
) -> bool:
    aspect = graph.get_aspect(urn, aspect_type)
    if aspect is None:
        return False
    expected = set(_decision_values(publication))
    return any(
        assignment.propertyUrn == DECISION_PROPERTY_URN and set(assignment.values) == expected
        for assignment in aspect.properties
    )


def _validate_approval(
    publication: CanonicalPublication,
    approval: PublicationApproval,
) -> None:
    if not isinstance(approval, PublicationApproval):
        raise ReviewWorkflowError(
            ReviewErrorCode.APPROVAL_REQUIRED,
            "explicit publication approval is required",
        )
    expected = tuple(
        sorted(
            decision.id
            for decision in publication.decisions
            if decision.action is DecisionAction.APPROVE
            and decision.status is ApprovalStatus.APPROVED
        )
    )
    if (
        approval.draft_id != publication.draft_id
        or approval.draft_version != publication.draft_version
        or approval.payload_fingerprint != publication.fingerprint
        or tuple(sorted(approval.decision_ids)) != expected
    ):
        raise ReviewWorkflowError(
            ReviewErrorCode.APPROVAL_MISMATCH,
            "catalog approval does not match the publication payload",
        )


def _decision_refs(
    publication: CanonicalPublication,
) -> tuple[PublicationDecisionRef, ...]:
    return tuple(
        PublicationDecisionRef(
            id=decision.id,
            target_id=decision.target_id,
            version=decision.resulting_version,
        )
        for decision in publication.decisions
        if decision.action is DecisionAction.APPROVE and decision.status is ApprovalStatus.APPROVED
    )


def _decision_values(publication: CanonicalPublication) -> list[str]:
    return [f"{ref.id}@v{ref.version}" for ref in _decision_refs(publication)]


def _decision_properties(publication: CanonicalPublication) -> dict[str, str]:
    return {
        "schemabridge.draft": publication.draft_id,
        "schemabridge.draftVersion": str(publication.draft_version),
        "schemabridge.decisionRefs": ",".join(_decision_values(publication)),
    }


def _all_current(
    publication: CanonicalPublication,
    approval: PublicationApproval,
    target_states: dict[tuple[PublicationItemKind, str], _TargetState],
) -> PublicationResult:
    refs = _decision_refs(publication)
    return PublicationResult(
        approval_id=approval.id,
        draft_id=publication.draft_id,
        fingerprint=publication.fingerprint,
        status=PublicationStatus.ALREADY_CURRENT,
        items=tuple(
            _item(
                kind,
                target,
                PublicationItemStatus.ALREADY_CURRENT,
                refs,
                publication,
                approval,
                target_states[(kind, target)].previous_fingerprint,
            )
            for kind, target in _targets(publication)
        ),
    )


def _targets(
    publication: CanonicalPublication,
) -> tuple[tuple[PublicationItemKind, str], ...]:
    targets: list[tuple[PublicationItemKind, str]] = [
        (PublicationItemKind.STRUCTURED_PROPERTY, DECISION_PROPERTY_URN),
    ]
    targets.extend(
        (PublicationItemKind.GLOSSARY_TERM, _term_urn(field.id.root))
        for field in publication.fields
    )
    targets.append((PublicationItemKind.LOGICAL_MODEL, LOGICAL_CUSTOMER_URN))
    targets.extend(
        (PublicationItemKind.PHYSICAL_LINK, dataset)
        for dataset in _physical_dataset_names(publication)
    )
    targets.extend(
        (
            (PublicationItemKind.DECISION_DOCUMENT, _document_urn(publication)),
            (PublicationItemKind.PUBLICATION_MARKER, LOGICAL_CUSTOMER_URN),
        )
    )
    return tuple(targets)


def _item(
    kind: PublicationItemKind,
    target: str,
    status: PublicationItemStatus,
    refs: tuple[PublicationDecisionRef, ...],
    publication: CanonicalPublication,
    approval: PublicationApproval,
    previous_fingerprint: str | None,
    *,
    reason_code: str | None = None,
) -> PublicationItemResult:
    outcome = {
        PublicationItemStatus.PUBLISHED: PublicationAuditOutcome.SUCCEEDED,
        PublicationItemStatus.ALREADY_CURRENT: PublicationAuditOutcome.ALREADY_CURRENT,
        PublicationItemStatus.FAILED: PublicationAuditOutcome.FAILED,
        PublicationItemStatus.NOT_ATTEMPTED: PublicationAuditOutcome.NOT_ATTEMPTED,
    }[status]
    return PublicationItemResult(
        kind=kind,
        target=target,
        status=status,
        decision_refs=refs,
        reason_code=reason_code,
        audit_record=PublicationTargetAuditRecord(
            family=PublicationFamily.CANONICAL,
            operation=kind.value,
            target=target,
            approval_id=approval.id,
            actor=approval.actor,
            approved_at=approval.approved_at,
            previous_fingerprint=previous_fingerprint,
            new_fingerprint=publication.fingerprint,
            outcome=outcome,
            decision_ids=tuple(reference.id for reference in refs),
            reason_code=reason_code,
        ),
    )


def _physical_dataset_names(publication: CanonicalPublication) -> tuple[str, ...]:
    return tuple(
        sorted({mapping.physical_field.root.rsplit(".", 1)[0] for mapping in publication.mappings})
    )


def _physical_urn(dataset: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.{dataset},PROD)"


def _schema_field_urn(dataset_urn: str, field_path: str) -> str:
    from datahub.metadata.urns import SchemaFieldUrn

    return str(SchemaFieldUrn(dataset_urn, field_path))


def _term_urn(logical_field: str) -> str:
    return f"urn:li:glossaryTerm:SchemaBridge.{logical_field}"


def _document_urn(publication: CanonicalPublication) -> str:
    return f"urn:li:document:schemabridge-{publication.draft_id}-v{publication.draft_version}"


def _native_type(canonical_type: str) -> str:
    return {
        "string": "varchar",
        "integer": "bigint",
        "decimal": "decimal",
        "boolean": "boolean",
        "date": "date",
        "timestamp": "timestamp",
    }[canonical_type]


def _reason_code(error: Exception) -> str:
    code = _http_status(error)
    if code in {401, 403}:
        return "catalog_permission_denied"
    if isinstance(error, (TimeoutError, ConnectionError, urllib.error.URLError)) or (
        code is not None and code >= 500
    ):
        return "catalog_unavailable"
    return "catalog_write_failed"


def _review_error_code(error: Exception) -> ReviewErrorCode:
    reason = _reason_code(error)
    if reason == "catalog_permission_denied":
        return ReviewErrorCode.CATALOG_PERMISSION_DENIED
    if reason == "catalog_unavailable":
        return ReviewErrorCode.CATALOG_UNAVAILABLE
    return ReviewErrorCode.CATALOG_INVALID_RESPONSE


def _http_status(error: Exception) -> int | None:
    if isinstance(error, urllib.error.HTTPError):
        return error.code
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None
