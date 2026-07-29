from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.verify_supply_chain import (
    SupplyChainViolation,
    build_cyclonedx_sbom,
    build_provenance,
    direct_license_inventory,
    parse_hashed_requirements,
    static_findings,
    verify_cyclonedx_sbom,
    verify_docker_context,
    verify_images,
    verify_provenance,
    verify_vulnerability_reports,
    verify_workflows,
)

ROOT = Path(__file__).resolve().parents[2]
REVISION = "1" * 40
DIGEST = "2" * 64
WORKFLOW = ".github/workflows/ci.yml"
IMAGE_NAME = "schemabridge-runtime:test"
OCI_IMAGE_NAME = "ghcr.io/example/schemabridge"
DIFF_ID = f"sha256:{'3' * 64}"


def _write_runtime_requirements(root: Path, *requirements: tuple[str, str]) -> None:
    directory = root / "requirements"
    directory.mkdir(exist_ok=True)
    blocks = [
        f"{name}=={version} \\\n"
        "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        for name, version in requirements
    ]
    (directory / "runtime.txt").write_text("".join(blocks), encoding="utf-8")


def _pip_audit_payload(*requirements: tuple[str, str]) -> dict[str, object]:
    return {
        "dependencies": [
            {"name": name, "version": version, "vulns": []} for name, version in requirements
        ],
        "fixes": [],
    }


def _trivy_payload(
    vulnerabilities: list[dict[str, str]] | None = None,
    *,
    artifact_name: str = IMAGE_NAME,
    image_id: str = f"sha256:{DIGEST}",
    repo_digests: list[str] | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "ImageID": image_id,
        "DiffIDs": [DIFF_ID],
    }
    if repo_digests is not None:
        metadata["RepoDigests"] = repo_digests
    return {
        "SchemaVersion": 2,
        "ArtifactName": artifact_name,
        "ArtifactType": "container_image",
        "Metadata": metadata,
        "Results": [
            {
                "Target": "debian",
                "Class": "os-pkgs",
                "Type": "debian",
                "Packages": [{"Name": "example", "Version": "1.2.3"}],
                "Vulnerabilities": vulnerabilities or [],
            }
        ],
    }


def _mutated_release_workflow(
    tmp_path: Path,
    *,
    before: str,
    after: str,
) -> Path:
    workflow_directory = tmp_path / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    source = (ROOT / ".github" / "workflows" / "release-evidence.yml").read_text(encoding="utf-8")
    assert source.count(before) >= 1
    path = workflow_directory / "release-evidence.yml"
    path.write_text(source.replace(before, after, 1), encoding="utf-8")
    return path


def test_repository_supply_chain_inputs_are_frozen_and_immutable() -> None:
    assert static_findings(ROOT) == ()


def test_hashed_requirements_require_exact_versions_and_sha256() -> None:
    parsed = parse_hashed_requirements(
        "example-package==1.2.3 \\\n"
        "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        source="requirements.txt",
    )

    assert parsed[0].normalized_name == "example-package"
    assert parsed[0].version == "1.2.3"
    with pytest.raises(SupplyChainViolation) as unpinned:
        parse_hashed_requirements(
            "example-package>=1.2.3 \\\n"
            "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
            source="requirements.txt",
        )
    assert unpinned.value.findings[0].code == "requirement_not_exact"
    with pytest.raises(SupplyChainViolation) as unhashed:
        parse_hashed_requirements("example-package==1.2.3\n", source="requirements.txt")
    assert unhashed.value.findings[0].code == "requirement_hash_missing"


def test_mutable_action_and_runtime_image_references_are_rejected(tmp_path: Path) -> None:
    workflow_directory = tmp_path / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    (workflow_directory / "ci.yml").write_text(
        "name: CI\n"
        "on: [push]\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  quality:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4 # v4.2.2\n",
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile.runtime").write_text(
        "FROM python:3.13-slim\nRUN python -m pip install '.[api,postgres,sql]'\n",
        encoding="utf-8",
    )

    assert "action_not_sha_pinned" in {finding.code for finding in verify_workflows(tmp_path)}
    image_codes = {finding.code for finding in verify_images(tmp_path)}
    assert "base_image_not_digest_pinned" in image_codes
    assert "runtime_mutable_resolution" in image_codes


@pytest.mark.parametrize(
    ("before", "after", "expected_code"),
    [
        (
            "  push:\n    branches:\n      - main\n",
            "  push:\n",
            "ci_push_scope_invalid",
        ),
        (
            "  cancel-in-progress: true\n",
            "  cancel-in-progress: false\n",
            "ci_concurrency_invalid",
        ),
        (
            "    timeout-minutes: 60\n",
            "",
            "ci_job_timeout_invalid",
        ),
        (
            "    timeout-minutes: 45\n",
            "",
            "ci_job_timeout_invalid",
        ),
        (
            "          persist-credentials: false\n",
            "          persist-credentials: true\n",
            "checkout_credentials_persisted",
        ),
        (
            "            .local/supply-chain/trivy-image.json\n",
            "            .local/supply-chain/unreviewed.json\n",
            "supply_chain_artifact_upload_invalid",
        ),
        (
            "          include-hidden-files: true\n",
            "          include-hidden-files: false\n",
            "supply_chain_artifact_upload_invalid",
        ),
    ],
)
def test_ci_operational_controls_fail_closed(
    tmp_path: Path,
    before: str,
    after: str,
    expected_code: str,
) -> None:
    workflow_directory = tmp_path / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    source = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert before in source
    (workflow_directory / "ci.yml").write_text(
        source.replace(before, after, 1),
        encoding="utf-8",
    )

    assert expected_code in {finding.code for finding in verify_workflows(tmp_path)}


@pytest.mark.parametrize(
    "missing_pattern",
    [
        ".streamlit/secrets.toml",
        "*.p12",
        "*.pfx",
        "*.jks",
        "*.keystore",
    ],
)
def test_docker_context_requires_sensitive_file_exclusions(
    tmp_path: Path,
    missing_pattern: str,
) -> None:
    patterns = (
        ".streamlit/secrets.toml",
        "*.p12",
        "*.pfx",
        "*.jks",
        "*.keystore",
    )
    (tmp_path / ".dockerignore").write_text(
        "\n".join(pattern for pattern in patterns if pattern != missing_pattern) + "\n",
        encoding="utf-8",
    )

    assert "docker_context_secret_exclusion_missing" in {
        finding.code for finding in verify_docker_context(tmp_path)
    }


def test_docker_context_rejects_late_secret_reinclusion(tmp_path: Path) -> None:
    (tmp_path / ".dockerignore").write_text(
        (".streamlit/secrets.toml\n*.p12\n*.pfx\n*.jks\n*.keystore\n!certificates/release.p12\n"),
        encoding="utf-8",
    )

    assert "docker_context_secret_exclusion_missing" in {
        finding.code for finding in verify_docker_context(tmp_path)
    }


def test_yaml_security_controls_are_structural_and_reject_inline_bypasses(
    tmp_path: Path,
) -> None:
    workflow_directory = tmp_path / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    (workflow_directory / "ci.yml").write_text(
        "name: CI\n"
        "on: [pull_request_target]\n"
        "permissions: {contents: read, id-token: write}\n"
        "jobs:\n"
        "  quality:\n"
        "    runs-on: ubuntu-latest\n"
        "    container: {image: python:latest}\n"
        "    steps:\n"
        '      - uses: "actions/checkout@v4" # v4.2.2\n',
        encoding="utf-8",
    )
    (tmp_path / "docker-compose.demo.yml").write_text(
        "services: {database: {image: postgres:latest}}\n",
        encoding="utf-8",
    )

    workflow_codes = {finding.code for finding in verify_workflows(tmp_path)}
    assert {
        "action_not_sha_pinned",
        "release_attestation_missing",
        "unsafe_workflow_trigger",
        "workflow_permissions_not_minimal",
    } <= workflow_codes
    image_codes = {finding.code for finding in verify_images(tmp_path)}
    assert "workflow_image_not_digest_pinned" in image_codes
    assert "compose_image_not_digest_pinned" in image_codes


def test_duplicate_workflow_keys_and_missing_release_attestations_fail_closed(
    tmp_path: Path,
) -> None:
    workflow_directory = tmp_path / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    (workflow_directory / "ci.yml").write_text(
        "name: CI\nname: attacker\non: [push]\npermissions: {contents: read}\njobs: {}\n",
        encoding="utf-8",
    )
    (workflow_directory / "release-evidence.yml").write_text(
        "name: Release\n"
        "on:\n"
        "  release:\n"
        "    types: [published]\n"
        "permissions: {contents: read}\n"
        "jobs:\n"
        "  attest:\n"
        "    environment: production-release\n"
        "    permissions: {contents: read, id-token: write, attestations: write}\n"
        "    steps:\n"
        "      - uses: "
        "actions/attest-build-provenance@"
        "977bb373ede98d70efdf65b84cb5f73e068dcc2a # v3.0.0\n"
        "        with: {subject-path: .local/supply-chain/dist/*.whl}\n",
        encoding="utf-8",
    )

    codes = {finding.code for finding in verify_workflows(tmp_path)}
    assert "yaml_structure_invalid" in codes
    assert "release_attestation_missing" in codes


@pytest.mark.parametrize(
    ("before", "after", "expected_code"),
    [
        (
            "${{ secrets.SCHEMABRIDGE_RELEASE_APPROVAL_SENTINEL }}",
            "${{ vars.SCHEMABRIDGE_RELEASE_APPROVAL_SENTINEL }}",
            "release_approval_sentinel_missing",
        ),
        (
            ".venv/bin/python scripts/release_audit.py --require-release",
            ".venv/bin/python scripts/release_audit.py",
            "release_clean_audit_missing",
        ),
        (
            "      packages: write\n",
            "      packages: read\n",
            "release_registry_permission_missing",
        ),
        (
            '          image_name="ghcr.io/${REPOSITORY_SLUG,,}"\n',
            '          image_name="ghcr.io/$REPOSITORY_SLUG"\n',
            "release_image_name_invalid",
        ),
        (
            "          registry: ghcr.io\n",
            "          registry: docker.io\n",
            "release_registry_login_missing",
        ),
        (
            "          push: true\n",
            "          push: false\n",
            "release_build_push_missing",
        ),
        (
            "          image-ref: ${{ steps.image_name.outputs.name }}@${{ steps.build.outputs.digest }}\n",
            "          image-ref: ${{ steps.image_name.outputs.name }}\n",
            "release_remote_scan_missing",
        ),
        (
            '            --image-digest "${{ steps.build.outputs.digest }}" \\\n',
            '            --image-digest "${{ steps.image.outputs.digest }}" \\\n',
            "release_manifest_digest_binding_missing",
        ),
        (
            "          subject-digest: ${{ steps.build.outputs.digest }}\n",
            "          subject-digest: ${{ steps.image.outputs.digest }}\n",
            "release_registry_attestation_missing",
        ),
        (
            "          subject-name: ${{ steps.image_name.outputs.name }}\n",
            "          subject-name: ghcr.io/${{ github.repository }}\n",
            "release_registry_attestation_missing",
        ),
        (
            "          push-to-registry: true\n",
            "          push-to-registry: false\n",
            "release_registry_attestation_missing",
        ),
        (
            "    timeout-minutes: 45\n",
            "",
            "release_attest_timeout_invalid",
        ),
        (
            "            .local/supply-chain/trivy-image.json\n",
            "            .local/supply-chain/unreviewed.json\n",
            "supply_chain_artifact_upload_invalid",
        ),
        (
            "          include-hidden-files: true\n",
            "          include-hidden-files: false\n",
            "supply_chain_artifact_upload_invalid",
        ),
    ],
)
def test_release_requires_digest_bound_ghcr_publication_and_attestation(
    tmp_path: Path,
    before: str,
    after: str,
    expected_code: str,
) -> None:
    _mutated_release_workflow(tmp_path, before=before, after=after)

    assert expected_code in {finding.code for finding in verify_workflows(tmp_path)}


def test_static_gate_scans_candidate_secrets_artifacts_and_architecture() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    static_target = makefile.split("supply-chain-static:", maxsplit=1)[1].split("\n\n", maxsplit=1)[
        0
    ]

    assert "$(BIN)/python scripts/verify_supply_chain.py static" in static_target
    assert "$(BIN)/python scripts/release_audit.py" in static_target
    assert "--check-external" not in static_target


def test_release_rejects_local_config_digest_as_oci_manifest_evidence(
    tmp_path: Path,
) -> None:
    _mutated_release_workflow(
        tmp_path,
        before="      - name: Generate the complete runtime-image CycloneDX SBOM\n",
        after=(
            "      - name: Record the local config digest\n"
            "        id: image\n"
            "        run: docker image inspect --format '{{.Id}}' \"$IMAGE_NAME\"\n"
            "      - name: Generate the complete runtime-image CycloneDX SBOM\n"
        ),
    )

    assert "release_local_image_digest_forbidden" in {
        finding.code for finding in verify_workflows(tmp_path)
    }


def test_direct_dependency_licenses_are_known_allowed_and_locked() -> None:
    inventory = direct_license_inventory(ROOT)

    observed = {item.package for item in inventory}
    assert {
        "fastapi",
        "openai",
        "psycopg",
        "pydantic",
        "streamlit",
        "uv",
    } <= observed
    assert all(item.license != "unknown" for item in inventory)


def test_sbom_covers_exact_runtime_export_and_binds_artifact() -> None:
    payload = build_cyclonedx_sbom(
        root=ROOT,
        artifact_name="schemabridge.whl",
        artifact_type="application",
        artifact_digest=DIGEST,
        source_revision=REVISION,
    )

    verify_cyclonedx_sbom(
        payload,
        root=ROOT,
        artifact_digest=DIGEST,
        source_revision=REVISION,
    )
    tampered = json.loads(json.dumps(payload))
    tampered["components"].pop()
    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            tampered,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
        )
    assert error.value.findings[0].code == "sbom_component_mismatch"


def test_provenance_binds_source_artifacts_lock_and_both_sboms(tmp_path: Path) -> None:
    wheel = tmp_path / "schemabridge.whl"
    wheel.write_bytes(b"wheel")
    wheel_sbom = tmp_path / "wheel.cdx.json"
    image_sbom = tmp_path / "runtime-image.cdx.json"
    wheel_sbom.write_text("{}\n", encoding="utf-8")
    image_sbom.write_text("{}\n", encoding="utf-8")
    repository = "https://github.com/example/schemabridge"
    payload = build_provenance(
        root=ROOT,
        repository=repository,
        revision=REVISION,
        wheel=wheel,
        image_name="schemabridge-runtime:test",
        image_digest=DIGEST,
        wheel_sbom=wheel_sbom,
        image_sbom=image_sbom,
        workflow=WORKFLOW,
    )

    verify_provenance(
        payload,
        root=ROOT,
        repository=repository,
        revision=REVISION,
        wheel=wheel,
        image_name="schemabridge-runtime:test",
        image_digest=DIGEST,
        wheel_sbom=wheel_sbom,
        image_sbom=image_sbom,
        workflow=WORKFLOW,
    )
    subject_tampered = json.loads(json.dumps(payload))
    subject_tampered["subject"][0]["digest"]["sha256"] = "3" * 64
    with pytest.raises(SupplyChainViolation) as error:
        verify_provenance(
            subject_tampered,
            root=ROOT,
            repository=repository,
            revision=REVISION,
            wheel=wheel,
            image_name="schemabridge-runtime:test",
            image_digest=DIGEST,
            wheel_sbom=wheel_sbom,
            image_sbom=image_sbom,
            workflow=WORKFLOW,
        )
    assert error.value.findings[0].code == "provenance_subject_mismatch"

    builder_tampered = json.loads(json.dumps(payload))
    builder_tampered["predicate"]["runDetails"]["builder"]["id"] = "https://example.invalid"
    with pytest.raises(SupplyChainViolation) as error:
        verify_provenance(
            builder_tampered,
            root=ROOT,
            repository=repository,
            revision=REVISION,
            wheel=wheel,
            image_name="schemabridge-runtime:test",
            image_digest=DIGEST,
            wheel_sbom=wheel_sbom,
            image_sbom=image_sbom,
            workflow=WORKFLOW,
        )
    assert error.value.findings[0].code == "provenance_builder_mismatch"


def test_high_vulnerability_requires_a_dated_owned_exception(tmp_path: Path) -> None:
    _write_runtime_requirements(tmp_path, ("example", "1.2.3"))
    pip_report = tmp_path / "pip-audit.json"
    pip_report.write_text(
        json.dumps(_pip_audit_payload(("example", "1.2.3"))),
        encoding="utf-8",
    )
    trivy_report = tmp_path / "trivy.json"
    trivy_report.write_text(
        json.dumps(
            _trivy_payload(
                [
                    {
                        "VulnerabilityID": "CVE-2099-0001",
                        "PkgName": "example",
                        "Severity": "HIGH",
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    exceptions = tmp_path / "exceptions.json"
    exceptions.write_text(
        json.dumps({"schema_version": 1, "exceptions": []}),
        encoding="utf-8",
    )

    trivy_report.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SupplyChainViolation) as malformed:
        verify_vulnerability_reports(
            (pip_report, trivy_report),
            exceptions,
            root=tmp_path,
            image_name=IMAGE_NAME,
            image_digest=DIGEST,
        )
    assert "vulnerability_report_invalid" in {finding.code for finding in malformed.value.findings}

    trivy_report.write_text(
        json.dumps(
            _trivy_payload(
                [
                    {
                        "VulnerabilityID": "CVE-2099-0001",
                        "PkgName": "example",
                        "Severity": "HIGH",
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(SupplyChainViolation) as blocked:
        verify_vulnerability_reports(
            (pip_report, trivy_report),
            exceptions,
            root=tmp_path,
            image_name=IMAGE_NAME,
            image_digest=DIGEST,
        )
    assert blocked.value.findings[0].code == "vulnerability_unexcepted"

    exceptions.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exceptions": [
                    {
                        "id": "CVE-2099-0001",
                        "package": "example",
                        "expires": "2099-12-31",
                        "owner": "security@example.invalid",
                        "reason": "Synthetic test-only exception.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert verify_vulnerability_reports(
        (pip_report, trivy_report),
        exceptions,
        root=tmp_path,
        image_name=IMAGE_NAME,
        image_digest=DIGEST,
    ) == (("CVE-2099-0001", "example", "HIGH"),)


def test_trivy_release_report_binds_the_remote_manifest_reference(tmp_path: Path) -> None:
    _write_runtime_requirements(tmp_path, ("example", "1.2.3"))
    pip_report = tmp_path / "pip-audit.json"
    pip_report.write_text(
        json.dumps(_pip_audit_payload(("example", "1.2.3"))),
        encoding="utf-8",
    )
    trivy_report = tmp_path / "trivy.json"
    trivy_report.write_text(
        json.dumps(
            _trivy_payload(
                artifact_name=f"{OCI_IMAGE_NAME}@sha256:{DIGEST}",
                image_id=f"sha256:{'4' * 64}",
                repo_digests=[f"{OCI_IMAGE_NAME}@sha256:{DIGEST}"],
            )
        ),
        encoding="utf-8",
    )
    exceptions = tmp_path / "exceptions.json"
    exceptions.write_text(
        json.dumps({"schema_version": 1, "exceptions": []}),
        encoding="utf-8",
    )

    assert (
        verify_vulnerability_reports(
            (pip_report, trivy_report),
            exceptions,
            root=tmp_path,
            image_name=OCI_IMAGE_NAME,
            image_digest=DIGEST,
        )
        == ()
    )

    trivy_report.write_text(
        json.dumps(
            _trivy_payload(
                artifact_name=f"{OCI_IMAGE_NAME}@sha256:{DIGEST}",
                image_id=f"sha256:{DIGEST}",
                repo_digests=[f"{OCI_IMAGE_NAME}@sha256:{'5' * 64}"],
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(SupplyChainViolation) as wrong_manifest:
        verify_vulnerability_reports(
            (pip_report, trivy_report),
            exceptions,
            root=tmp_path,
            image_name=OCI_IMAGE_NAME,
            image_digest=DIGEST,
        )
    assert "vulnerability_report_incomplete" in {
        finding.code for finding in wrong_manifest.value.findings
    }


@pytest.mark.parametrize(
    ("pip_payload", "trivy_payload"),
    [
        ({"dependencies": [], "fixes": []}, _trivy_payload()),
        (
            {
                "dependencies": [
                    {"name": "example", "version": "1.2.3", "vulns": []},
                ],
                "fixes": [],
            },
            {
                "SchemaVersion": 2,
                "ArtifactName": IMAGE_NAME,
                "ArtifactType": "container_image",
                "Metadata": {
                    "ImageID": f"sha256:{DIGEST}",
                    "DiffIDs": [DIFF_ID],
                },
                "Results": [],
            },
        ),
    ],
)
def test_empty_or_incomplete_vulnerability_reports_are_rejected(
    tmp_path: Path,
    pip_payload: dict[str, object],
    trivy_payload: dict[str, object],
) -> None:
    _write_runtime_requirements(
        tmp_path,
        ("example", "1.2.3"),
        ("second", "2.0.0"),
    )
    pip_report = tmp_path / "pip-audit.json"
    pip_report.write_text(json.dumps(pip_payload), encoding="utf-8")
    trivy_report = tmp_path / "trivy.json"
    trivy_report.write_text(json.dumps(trivy_payload), encoding="utf-8")
    exceptions = tmp_path / "exceptions.json"
    exceptions.write_text(
        json.dumps({"schema_version": 1, "exceptions": []}),
        encoding="utf-8",
    )

    with pytest.raises(SupplyChainViolation) as raised:
        verify_vulnerability_reports(
            (pip_report, trivy_report),
            exceptions,
            root=tmp_path,
            image_name=IMAGE_NAME,
            image_digest=DIGEST,
        )

    assert "vulnerability_report_incomplete" in {finding.code for finding in raised.value.findings}
