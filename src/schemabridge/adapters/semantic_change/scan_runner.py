"""Concrete read-only runner for one durable semantic-change scan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from schemabridge.application.ports.semantic_change_scans import (
    SemanticChangeScanInspection,
    SemanticChangeScanRunnerError,
    SemanticChangeScanRunnerErrorCode,
)
from schemabridge.application.semantic_change import (
    InspectSemanticChange,
    SemanticChangeError,
    SemanticChangeErrorCode,
)
from schemabridge.domain.semantic_change import (
    SemanticBindingSelectionSet,
)
from schemabridge.domain.semantic_change_scans import (
    SemanticChangeScanRequest,
    SemanticChangeScanSourceKind,
    SemanticChangeScanStatus,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

SemanticChangeScopeResolver = Callable[
    [SemanticChangeScanRequest],
    SemanticRegistryScope,
]
SemanticChangeInspectorFactory = Callable[
    [SemanticRegistryScope, SemanticChangeScanRequest],
    InspectSemanticChange,
]
SemanticDependencyPreparer = Callable[
    [SemanticChangeScanRequest, SemanticRegistryScope, Callable[[], bool]],
    object,
]
SemanticBindingSelectionLoader = Callable[
    [SemanticChangeScanRequest, SemanticRegistryScope],
    SemanticBindingSelectionSet | None,
]


def _no_binding_selections(
    request: SemanticChangeScanRequest,
    scope: SemanticRegistryScope,
) -> None:
    del request, scope
    return None


@dataclass(frozen=True, slots=True)
class InspectSemanticChangeScanRunner:
    """Resolve one exact scope and capture evidence without persisting it.

    Catalog-generation requests deliberately need an injected resolver because
    their durable trigger contains a workspace/connection/generation, not a
    registry identity.  The resolver must fail when that trigger is ambiguous.
    """

    scope_resolver: SemanticChangeScopeResolver = field(repr=False)
    inspector_factory: SemanticChangeInspectorFactory = field(repr=False)
    prepare_dependencies: SemanticDependencyPreparer = field(repr=False)
    binding_selections: SemanticBindingSelectionLoader = field(
        default=_no_binding_selections,
        repr=False,
    )

    def inspect(
        self,
        request: SemanticChangeScanRequest,
        *,
        should_continue: Callable[[], bool],
    ) -> SemanticChangeScanInspection:
        """Capture one current report after dependency-index reconciliation."""

        checked_request = _request(request)
        if checked_request.status is not SemanticChangeScanStatus.LEASED:
            raise _invalid("semantic scan runner requires one leased request")
        _continue_or_stop(should_continue)

        try:
            scope = self.scope_resolver(checked_request)
        except SemanticChangeScanRunnerError:
            raise
        except Exception:
            raise SemanticChangeScanRunnerError(
                SemanticChangeScanRunnerErrorCode.REGISTRY_UNAVAILABLE,
                "semantic scan scope is unavailable",
            ) from None
        scope = _scope(scope)
        if not _scope_matches_request(scope, checked_request):
            raise _invalid("semantic scan scope does not match its durable trigger")
        _continue_or_stop(should_continue)

        # This hook is intentionally before capture_current.  It may reconcile
        # managed workflow/recipe dependencies, but a partial or failed pass must
        # never be followed by an immutable report commit.
        try:
            self.prepare_dependencies(checked_request, scope, should_continue)
        except Exception:
            raise SemanticChangeScanRunnerError(
                SemanticChangeScanRunnerErrorCode.DEPENDENCY_INDEX_INCOMPLETE,
                "semantic dependency index could not be prepared completely",
            ) from None
        _continue_or_stop(should_continue)

        try:
            inspector = self.inspector_factory(scope, checked_request)
        except SemanticChangeScanRunnerError:
            raise
        except Exception:
            raise SemanticChangeScanRunnerError(
                SemanticChangeScanRunnerErrorCode.REGISTRY_UNAVAILABLE,
                "semantic change inspector is unavailable",
            ) from None
        if not isinstance(inspector, InspectSemanticChange) or inspector.scope != scope:
            raise _invalid("semantic change inspector does not match the scan scope")

        try:
            selections = self.binding_selections(checked_request, scope)
        except SemanticChangeScanRunnerError:
            raise
        except Exception:
            raise SemanticChangeScanRunnerError(
                SemanticChangeScanRunnerErrorCode.EVIDENCE_UNAVAILABLE,
                "semantic binding selections are unavailable",
            ) from None
        selections = _selections(selections, scope)
        _continue_or_stop(should_continue)

        try:
            report, observation = inspector.capture_current(
                binding_selections=selections,
            )
        except SemanticChangeError as error:
            raise _inspection_error(error) from None
        except Exception:
            raise SemanticChangeScanRunnerError(
                SemanticChangeScanRunnerErrorCode.INVALID_INSPECTION,
                "semantic change inspection failed",
            ) from None
        _continue_or_stop(should_continue)

        try:
            inspection = SemanticChangeScanInspection(
                report=report,
                observation=observation,
            )
        except (TypeError, ValueError):
            raise _invalid("semantic change inspection is invalid") from None
        if not _inspection_matches_request(inspection, checked_request):
            raise _invalid("semantic change inspection does not match its durable trigger")
        return inspection


def _request(value: object) -> SemanticChangeScanRequest:
    try:
        checked = SemanticChangeScanRequest.model_validate(
            value.model_dump(mode="python", warnings=False)  # type: ignore[attr-defined]
        )
    except (AttributeError, TypeError, ValueError):
        raise _invalid("semantic scan request is invalid") from None
    if checked != value:
        raise _invalid("semantic scan request is non-canonical")
    return checked


def _scope(value: object) -> SemanticRegistryScope:
    try:
        checked = SemanticRegistryScope.model_validate(
            value.model_dump(mode="python", warnings=False)  # type: ignore[attr-defined]
        )
    except (AttributeError, TypeError, ValueError):
        raise _invalid("semantic scan scope is invalid") from None
    if checked != value:
        raise _invalid("semantic scan scope is non-canonical")
    return checked


def _scope_matches_request(
    scope: SemanticRegistryScope,
    request: SemanticChangeScanRequest,
) -> bool:
    if scope.workspace_id != request.workspace_id:
        return False
    return scope.catalog_scope == request.catalog_scope and scope.registry_id == request.registry_id


def _selections(
    value: object,
    scope: SemanticRegistryScope,
) -> SemanticBindingSelectionSet | None:
    if value is None:
        return None
    try:
        checked = SemanticBindingSelectionSet.model_validate(
            value.model_dump(mode="python", warnings=False)  # type: ignore[attr-defined]
        )
    except (AttributeError, TypeError, ValueError):
        raise _invalid("semantic binding selections are invalid") from None
    if checked != value or checked.scope != scope:
        raise _invalid("semantic binding selections do not match the scan scope")
    return checked


def _continue_or_stop(callback: Callable[[], bool]) -> None:
    value = callback()
    if not isinstance(value, bool):
        raise _invalid("semantic scan continuation signal is invalid")
    if not value:
        raise SemanticChangeScanRunnerError(
            SemanticChangeScanRunnerErrorCode.STOP_REQUESTED,
            "semantic scan stop was requested",
        )


def _inspection_matches_request(
    inspection: SemanticChangeScanInspection,
    request: SemanticChangeScanRequest,
) -> bool:
    report = inspection.report
    if report.context.scope.workspace_id != request.workspace_id:
        return False
    if request.source_kind is SemanticChangeScanSourceKind.REGISTRY_POINTER:
        return (
            report.context.scope.catalog_scope == request.catalog_scope
            and report.context.scope.registry_id == request.registry_id
            and report.context.pointer_generation == request.registry_generation
        )
    return any(
        item.connection_id.root == request.connection_id
        and item.generation == request.observed_catalog_generation
        for item in report.catalog_generations.observations
    )


def _inspection_error(
    error: SemanticChangeError,
) -> SemanticChangeScanRunnerError:
    code = {
        SemanticChangeErrorCode.UNAVAILABLE: (
            SemanticChangeScanRunnerErrorCode.REGISTRY_UNAVAILABLE
        ),
        SemanticChangeErrorCode.EVIDENCE_UNAVAILABLE: (
            SemanticChangeScanRunnerErrorCode.EVIDENCE_UNAVAILABLE
        ),
        SemanticChangeErrorCode.DEPENDENCY_INDEX_INCOMPLETE: (
            SemanticChangeScanRunnerErrorCode.DEPENDENCY_INDEX_INCOMPLETE
        ),
    }.get(error.code, SemanticChangeScanRunnerErrorCode.INVALID_INSPECTION)
    return SemanticChangeScanRunnerError(
        code,
        "semantic change inspection is unavailable or invalid",
    )


def _invalid(message: str) -> SemanticChangeScanRunnerError:
    return SemanticChangeScanRunnerError(
        SemanticChangeScanRunnerErrorCode.INVALID_INSPECTION,
        message,
    )


__all__ = [
    "InspectSemanticChangeScanRunner",
    "SemanticBindingSelectionLoader",
    "SemanticChangeInspectorFactory",
    "SemanticChangeScopeResolver",
    "SemanticDependencyPreparer",
]
