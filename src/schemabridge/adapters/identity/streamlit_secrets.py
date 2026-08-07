"""Bounded reader for Streamlit secrets projected by Kubernetes."""

from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

MAX_STREAMLIT_SECRETS_BYTES: Final = 64 * 1_024


class StreamlitSecretsFileError(RuntimeError):
    """Sanitized failure while reading projected Streamlit authentication data."""


@dataclass(frozen=True, slots=True)
class ProjectedStreamlitSecrets:
    """Read one stable, bounded TOML projection without exposing private values."""

    secrets_file: Path = field(repr=False)
    mount_root: Path = field(repr=False)
    max_bytes: int = MAX_STREAMLIT_SECRETS_BYTES

    def __post_init__(self) -> None:
        if (
            not isinstance(self.secrets_file, Path)
            or not self.secrets_file.is_absolute()
            or not isinstance(self.mount_root, Path)
            or not self.mount_root.is_absolute()
            or self.secrets_file == self.mount_root
            or not self.secrets_file.is_relative_to(self.mount_root)
            or type(self.max_bytes) is not int
            or not 1_024 <= self.max_bytes <= MAX_STREAMLIT_SECRETS_BYTES
        ):
            raise ValueError("projected Streamlit secrets configuration is invalid")

    def read(self) -> Mapping[str, object]:
        """Return parsed TOML only when the projected file remains stable and private."""

        descriptor: int | None = None
        try:
            root = self.mount_root.resolve(strict=True)
            if not root.is_dir():
                raise ValueError
            resolved_before = self.secrets_file.resolve(strict=True)
            if not resolved_before.is_relative_to(root):
                raise ValueError
            metadata_before = resolved_before.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata_before.st_mode)
                or metadata_before.st_uid not in {0, os.geteuid()}
                or metadata_before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not 1 <= metadata_before.st_size <= self.max_bytes
            ):
                raise ValueError

            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(resolved_before, flags)
            opened = os.fstat(descriptor)
            if _file_identity(metadata_before) != _file_identity(opened):
                raise ValueError

            raw = _read_exact(descriptor, opened.st_size)
            closed_over = os.fstat(descriptor)
            resolved_after = self.secrets_file.resolve(strict=True)
            if (
                resolved_after != resolved_before
                or _file_identity(opened) != _file_identity(closed_over)
                or len(raw) != closed_over.st_size
            ):
                raise ValueError
            return tomllib.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise StreamlitSecretsFileError(
                "Streamlit authentication configuration is unavailable"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise ValueError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
