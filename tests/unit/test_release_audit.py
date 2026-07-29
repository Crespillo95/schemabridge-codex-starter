"""M16 regressions for release architecture and repository scans."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.release_audit import (
    candidate_files,
    scan_architecture,
    scan_candidate_artifacts,
    scan_markdown_links,
    scan_secrets,
)

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


@pytest.mark.parametrize(
    ("value", "expected_code"),
    [
        ("AIza" + "A" * 35, "google_api_key"),
        ("glpat-" + "A" * 24, "gitlab_token"),
        ("xoxb-" + "A" * 24, "slack_token"),
        ("sk_live_" + "A" * 24, "stripe_live_key"),
    ],
)
def test_additional_high_confidence_secret_formats_are_rejected(
    tmp_path: Path,
    value: str,
    expected_code: str,
) -> None:
    candidate = tmp_path / "unsafe.txt"
    candidate.write_text(f"credential={value}\n", encoding="utf-8")

    findings = scan_secrets(tmp_path, (candidate,))

    assert [finding.code for finding in findings] == [expected_code]


@pytest.mark.parametrize(
    "name",
    [
        ".streamlit/secrets.toml",
        "client.p12",
        "client.pfx",
        "truststore.jks",
        "runtime.keystore",
    ],
)
def test_private_credential_artifacts_are_rejected(tmp_path: Path, name: str) -> None:
    candidate = tmp_path / name
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("synthetic\n", encoding="utf-8")

    findings = scan_candidate_artifacts(tmp_path, (candidate,))

    assert [finding.code for finding in findings] == ["secret_or_os_artifact"]


def test_forced_commit_visible_parallel_coverage_artifact_is_rejected(tmp_path: Path) -> None:
    subprocess.run(("git", "init", "--quiet"), cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".coverage.*\n", encoding="utf-8")
    parallel_coverage = tmp_path / ".coverage.worker-1"
    parallel_coverage.write_text("synthetic coverage data\n", encoding="utf-8")
    similarly_named = tmp_path / ".coverage-report"
    similarly_named.write_text("synthetic report\n", encoding="utf-8")
    subprocess.run(
        ("git", "add", "--force", parallel_coverage.name),
        cwd=tmp_path,
        check=True,
    )

    candidates = candidate_files(tmp_path)
    findings = scan_candidate_artifacts(tmp_path, candidates)

    assert parallel_coverage in candidates
    assert similarly_named in candidates
    assert [(finding.code, finding.path) for finding in findings] == [
        ("runtime_artifact", ".coverage.worker-1")
    ]
