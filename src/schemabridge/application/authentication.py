"""Sanitized authentication-boundary failures for entrypoints."""

from __future__ import annotations


class AuthenticationBoundaryError(RuntimeError):
    """Reject an authenticated transport without retaining or exposing its claims."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The authenticated session was rejected.")
