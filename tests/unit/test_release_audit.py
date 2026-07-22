"""M16 regressions for release architecture and repository scans."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.release_audit import scan_architecture, scan_markdown_links, scan_secrets

from schemabridge.domain import candidates, joins, request_context


def test_architecture_scan_covers_all_domain_and_application_modules(tmp_path: Path) -> None:
    domain = tmp_path / "src/schemabridge/domain"
    application = tmp_path / "src/schemabridge/application"
    domain.mkdir(parents=True)
    application.mkdir(parents=True)
    (domain / "bad.py").write_text("from schemabridge.adapters.sql import guard\n")
    (application / "bad.py").write_text("import schemabridge.entrypoints.cli.main\n")

    findings = scan_architecture(tmp_path)

    assert [(finding.code, finding.path) for finding in findings] == [
        ("dependency_direction", "src/schemabridge/application/bad.py:1"),
        ("dependency_direction", "src/schemabridge/domain/bad.py:1"),
    ]


def test_domain_lookup_constants_cannot_be_mutated() -> None:
    with pytest.raises((AttributeError, TypeError)):
        candidates._TOKEN_ALIASES["client"] = "wrong"  # type: ignore[index]
    with pytest.raises((AttributeError, TypeError)):
        joins._TOKEN_ALIASES["client"] = "wrong"  # type: ignore[index]
    with pytest.raises((AttributeError, TypeError)):
        request_context._NUMERIC_TYPES.add("string")  # type: ignore[attr-defined]


def test_secret_and_broken_link_scans_report_exact_candidate(tmp_path: Path) -> None:
    secret = tmp_path / "unsafe.txt"
    fake_key = "sk-" + "abcdefghijklmnopqrstuvwxyz"
    secret.write_text(f"OPENAI_API_KEY={fake_key}\n", encoding="utf-8")
    markdown = tmp_path / "README.md"
    markdown.write_text("[missing](docs/missing.md)\n", encoding="utf-8")

    secret_findings = scan_secrets(tmp_path, (secret, markdown))
    link_findings = scan_markdown_links(tmp_path, (secret, markdown))

    assert secret_findings[0].code == "openai_key"
    assert secret_findings[0].path == "unsafe.txt:1"
    assert link_findings[0].code == "broken_local_link"
    assert link_findings[0].path == "README.md:1"
