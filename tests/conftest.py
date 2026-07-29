"""Test-process credential isolation."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_real_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never let an operator's real provider credentials enter automated tests."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DATAHUB_GMS_TOKEN", raising=False)
