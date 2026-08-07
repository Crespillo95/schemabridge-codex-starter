"""Dependency-injected operational observer entrypoint."""

from schemabridge.entrypoints.observer.main import (
    ObserverServices,
    build_observer_application,
    create_observer_app,
)

__all__ = [
    "ObserverServices",
    "build_observer_application",
    "create_observer_app",
]
