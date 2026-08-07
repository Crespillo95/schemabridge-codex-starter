"""Operator ports and sanitized failures for control-plane backup/restore."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol

from schemabridge.domain.control_plane_operations import (
    ControlPlaneBackupManifest,
    ControlPlaneRestoreVerification,
)


class ControlPlaneOperationErrorCode(StrEnum):
    CONFIGURATION_INVALID = "control_plane_operation_configuration_invalid"
    TOOL_UNAVAILABLE = "control_plane_operation_tool_unavailable"
    DATABASE_UNAVAILABLE = "control_plane_operation_database_unavailable"
    BACKUP_FAILED = "control_plane_backup_failed"
    ARTIFACT_INVALID = "control_plane_backup_artifact_invalid"
    TARGET_NOT_FRESH = "control_plane_restore_target_not_fresh"
    TARGET_IS_SOURCE = "control_plane_restore_target_is_source"
    RESTORE_FAILED = "control_plane_restore_failed"
    VERIFICATION_FAILED = "control_plane_restore_verification_failed"


class ControlPlaneOperationError(RuntimeError):
    """Safe operator-facing failure without command output, paths, or credentials."""

    def __init__(
        self,
        code: ControlPlaneOperationErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class ControlPlaneBackupPort(Protocol):
    def create_backup(self, destination: Path) -> tuple[Path, Path, ControlPlaneBackupManifest]:
        """Create one owner-only archive and its signed manifest."""


class ControlPlaneRestorePort(Protocol):
    def restore_backup(
        self,
        archive: Path,
        manifest: Path,
    ) -> ControlPlaneRestoreVerification:
        """Restore into one fresh target and verify it before cutover."""
