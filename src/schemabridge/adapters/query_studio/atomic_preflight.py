"""Boundary-screened local preflight for deterministic description expansion."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.adapters.language.openai_boundary import (
    OpenAIAdapterError,
    OpenAIAdapterErrorCode,
    normalize_and_screen_user_text,
)
from schemabridge.adapters.language.openai_query_studio import (
    local_analytical_description_expansion,
)
from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.domain.query_studio import (
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    DescriptionExpansionRoute,
    atomic_description_expansion,
    classify_description_expansion_route,
)


@dataclass(frozen=True, slots=True)
class BoundaryScreenedDescriptionExpansionPreflight:
    """Screen first, then expand either supported route without provider egress."""

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        """Implement the production expansion port with explicitly local usage."""

        expansion = self.expand_if_local(value)
        if expansion is None:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "local description expansion did not resolve the declared route",
            )
        return DescriptionExpansionResult(expansion=expansion, usage=None)

    def expand_if_local(
        self,
        value: DescriptionExpansionInput,
    ) -> DescriptionExpansion | None:
        try:
            screened = normalize_and_screen_user_text(value.text.root)
        except OpenAIAdapterError as error:
            code = (
                QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED
                if error.code is OpenAIAdapterErrorCode.SENSITIVE_INPUT_BLOCKED
                else QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
            )
            raise QueryStudioPortError(
                code,
                "live AI preflight rejected the bounded business description",
            ) from None
        if screened != value.text.root:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "live AI preflight normalization changed the business description",
            )
        if classify_description_expansion_route(value) is DescriptionExpansionRoute.ANALYTICAL:
            return local_analytical_description_expansion(value)
        try:
            return atomic_description_expansion(value)
        except ValueError:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "local field-match descriptions must fit the atomic preflight bounds",
            ) from None


__all__ = ["BoundaryScreenedDescriptionExpansionPreflight"]
