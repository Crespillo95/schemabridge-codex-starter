"""Typed, immutable validation findings."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field, field_validator

from schemabridge.domain._base import FrozenDomainModel

_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class ValidationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ValidationFinding(FrozenDomainModel):
    code: str = Field(min_length=1)
    severity: ValidationSeverity
    message: str = Field(min_length=1)
    path: tuple[str, ...] = ()

    @field_validator("code")
    @classmethod
    def code_must_be_machine_readable(cls, value: str) -> str:
        if _CODE_PATTERN.fullmatch(value) is None:
            raise ValueError("validation code must use lower snake case")
        return value

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("validation message must not be blank")
        return value


class ValidationResult(FrozenDomainModel):
    findings: tuple[ValidationFinding, ...] = ()

    @property
    def is_valid(self) -> bool:
        return all(finding.severity is not ValidationSeverity.ERROR for finding in self.findings)
