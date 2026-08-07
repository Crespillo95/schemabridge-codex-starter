"""Manifest-backed, integrity-checked synthetic semantic-registry adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from schemabridge.application.ports.planning import PlanningPortError, PlanningPortErrorCode
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_REGISTRY_BYTES = 2 * 1024 * 1024
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class _ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    registry_id: str = Field(min_length=3, max_length=80)
    version: int = Field(ge=1)
    catalog_scope: str = Field(min_length=3, max_length=120)
    path: str = Field(min_length=1, max_length=240)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    fingerprint: str = Field(pattern=_SHA256_PATTERN)


class _RegistryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: int = Field(default=1, ge=1, le=1)
    active_registry: str = Field(min_length=3, max_length=80)
    registries: tuple[_ManifestEntry, ...] = Field(min_length=1, max_length=32)


class RecordedGovernedSemanticRegistry:
    """Load only the manifest's exact active bundle under one bound runtime scope."""

    def __init__(self, manifest_path: Path, scope: SemanticRegistryScope) -> None:
        self._manifest_path = manifest_path
        self._scope = scope

    @property
    def scope(self) -> SemanticRegistryScope:
        return self._scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        try:
            manifest_path = _safe_existing_file(
                self._manifest_path,
                root=self._manifest_path.parent.resolve(),
                maximum_bytes=_MAX_MANIFEST_BYTES,
            )
            manifest = _RegistryManifest.model_validate(
                _load_unique_yaml(manifest_path.read_text(encoding="utf-8"))
            )
            entries = {entry.registry_id: entry for entry in manifest.registries}
            if len(entries) != len(manifest.registries):
                raise ValueError("semantic registry manifest repeats a registry id")
            if manifest.active_registry != self._scope.registry_id:
                raise PlanningPortError(
                    PlanningPortErrorCode.REGISTRY_NOT_FOUND,
                    "the requested semantic registry is not the active deployment registry",
                )
            entry = entries.get(manifest.active_registry)
            if entry is None:
                raise ValueError("semantic registry manifest active pointer is missing")
            if entry.catalog_scope != self._scope.catalog_scope:
                raise PlanningPortError(
                    PlanningPortErrorCode.REGISTRY_SCOPE_MISMATCH,
                    "the semantic registry is not valid for this catalog scope",
                )
            registry_path = _registry_path(manifest_path.parent.resolve(), entry.path)
            payload = registry_path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != entry.sha256:
                raise PlanningPortError(
                    PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
                    "semantic registry artifact checksum verification failed",
                )
            registry = GovernedSemanticRegistrySnapshot.model_validate(
                _load_unique_yaml(payload.decode("utf-8"))
            )
            if (
                registry.registry_id != entry.registry_id
                or registry.version != entry.version
                or registry.catalog_scope != entry.catalog_scope
                or registry.fingerprint != entry.fingerprint
            ):
                raise PlanningPortError(
                    PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
                    "semantic registry identity or fingerprint verification failed",
                )
            return ScopedSemanticRegistrySnapshot(scope=self._scope, registry=registry)
        except PlanningPortError:
            raise
        except FileNotFoundError as error:
            raise PlanningPortError(
                PlanningPortErrorCode.CONTEXT_UNAVAILABLE,
                "governed semantic registry is unavailable",
            ) from error
        except (
            OSError,
            UnicodeError,
            ValidationError,
            yaml.YAMLError,
            TypeError,
            ValueError,
        ) as error:
            raise PlanningPortError(
                PlanningPortErrorCode.CONTEXT_INVALID,
                "governed semantic registry is invalid",
            ) from error


def _registry_path(root: Path, configured: str) -> Path:
    candidate = Path(configured)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("semantic registry path must remain inside the manifest directory")
    return _safe_existing_file(
        root / candidate,
        root=root,
        maximum_bytes=_MAX_REGISTRY_BYTES,
    )


def _safe_existing_file(path: Path, *, root: Path, maximum_bytes: int) -> Path:
    if path.is_symlink():
        raise ValueError("semantic registry files may not be symlinks")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("semantic registry file escaped its manifest directory")
    if not resolved.is_file():
        raise ValueError("semantic registry path must be a regular file")
    size = resolved.stat().st_size
    if size < 1 or size > maximum_bytes:
        raise ValueError("semantic registry file size is outside the configured bound")
    return resolved


def _load_unique_yaml(payload: str) -> Any:
    return yaml.load(payload, Loader=_UniqueKeyLoader)
