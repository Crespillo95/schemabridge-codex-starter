from __future__ import annotations

import hashlib
import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import scripts.verify_supply_chain as supply_chain
from scripts.verify_supply_chain import (
    POSTGRES_CLIENT_ALPINE_COMPONENTS,
    POSTGRES_CLIENT_APK_MATRIX,
    RUNTIME_ALPINE_COMPONENTS,
    RUNTIME_ALPINE_NOARCH_COMPONENTS,
    RUNTIME_ALPINE_PLATFORM_VIRTUAL_COMPONENT,
    SupplyChainViolation,
    bind_existing_cyclonedx_sbom,
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


def _linux_runtime_sbom(*, architecture: str = "aarch64") -> dict[str, object]:
    payload = json.loads(
        json.dumps(
            build_cyclonedx_sbom(
                root=ROOT,
                artifact_name=IMAGE_NAME,
                artifact_type="container",
                artifact_digest=DIGEST,
                source_revision=REVISION,
            )
        )
    )
    components = payload["components"]
    assert isinstance(components, list)
    removed_refs: set[str] = set()
    retained_components: list[dict[str, object]] = []
    for component in components:
        assert isinstance(component, dict)
        properties = component.get("properties")
        marker_values = (
            {
                value.get("value")
                for value in properties
                if isinstance(value, dict)
                and value.get("name") == "schemabridge:environment-marker"
            }
            if isinstance(properties, list)
            else set()
        )
        if any(
            isinstance(value, str) and "sys_platform == 'win32'" in value for value in marker_values
        ):
            removed_refs.add(str(component["bom-ref"]))
        else:
            retained_components.append(component)
    components[:] = retained_components

    python_base_refs: list[str] = []
    for name, version in (("pip", "26.1.2"), ("schemabridge", "0.1.0")):
        purl = f"pkg:pypi/{name}@{version}"
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": purl,
                "bom-ref": purl,
            }
        )
        python_base_refs.append(purl)

    apk_refs: list[str] = []
    runtime_alpine_components = RUNTIME_ALPINE_COMPONENTS | {
        RUNTIME_ALPINE_PLATFORM_VIRTUAL_COMPONENT[architecture]
    }
    for index, (name, version) in enumerate(sorted(runtime_alpine_components), start=1):
        component_architecture = (
            "noarch" if (name, version) in RUNTIME_ALPINE_NOARCH_COMPONENTS else architecture
        )
        purl = f"pkg:apk/alpine/{name}@{version}?arch={component_architecture}&distro=3.24.1"
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": purl,
                "bom-ref": purl,
                "hashes": [{"alg": "SHA-1", "content": f"{index:040x}"}],
                "properties": [],
            }
        )
        apk_refs.append(purl)

    dependencies = payload["dependencies"]
    assert isinstance(dependencies, list)
    dependencies[:] = [
        dependency
        for dependency in dependencies
        if isinstance(dependency, dict) and dependency.get("ref") not in removed_refs
    ]
    root_ref = payload["metadata"]["component"]["bom-ref"]
    root_dependency = next(
        dependency
        for dependency in dependencies
        if isinstance(dependency, dict) and dependency.get("ref") == root_ref
    )
    root_depends_on = root_dependency["dependsOn"]
    assert isinstance(root_depends_on, list)
    root_dependency["dependsOn"] = (
        [reference for reference in root_depends_on if reference not in removed_refs]
        + python_base_refs
        + apk_refs
    )
    dependencies.extend(
        {"ref": reference, "dependsOn": []} for reference in (*python_base_refs, *apk_refs)
    )
    return dict(
        bind_existing_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_name=IMAGE_NAME,
            artifact_digest=DIGEST,
            source_revision=REVISION,
        )
    )


def _sbom_component(payload: dict[str, object], name: str) -> dict[str, object]:
    components = payload["components"]
    assert isinstance(components, list)
    matches = [item for item in components if isinstance(item, dict) and item.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _replace_sbom_apk_identity(
    payload: dict[str, object],
    name: str,
    *,
    version: str | None = None,
    architecture: str | None = None,
) -> dict[str, object]:
    component = _sbom_component(payload, name)
    old_ref = component["purl"]
    old_version = component["version"]
    assert isinstance(old_ref, str)
    assert isinstance(old_version, str)
    old_architecture = old_ref.split("?arch=", maxsplit=1)[1].split("&", maxsplit=1)[0]
    new_version = version if version is not None else old_version
    new_architecture = architecture if architecture is not None else old_architecture
    new_ref = old_ref.replace(
        f"@{old_version}?arch={old_architecture}",
        f"@{new_version}?arch={new_architecture}",
        1,
    )
    component["version"] = new_version
    component["purl"] = new_ref
    component["bom-ref"] = new_ref

    dependencies = payload["dependencies"]
    assert isinstance(dependencies, list)
    for dependency in dependencies:
        assert isinstance(dependency, dict)
        if dependency.get("ref") == old_ref:
            dependency["ref"] = new_ref
        depends_on = dependency.get("dependsOn")
        assert isinstance(depends_on, list)
        dependency["dependsOn"] = [
            new_ref if reference == old_ref else reference for reference in depends_on
        ]
    return component


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
        (
            "          cache-dir: .local/trivy-cache\n",
            "          cache-dir: .cache/trivy\n",
            "trivy_cache_path_invalid",
        ),
        (
            ".venv/bin/pip-audit --disable-pip --require-hashes --format json \\\n",
            ".venv/bin/pip-audit --require-hashes --format json \\\n",
            "pip_audit_resolution_invalid",
        ),
        (
            "            --requirement requirements/watchdog-build.txt\n",
            "",
            "pip_audit_resolution_invalid",
        ),
        (
            '      DOCKER_BUILDKIT: "1"\n',
            '      DOCKER_BUILDKIT: "0"\n',
            "runtime_buildkit_invalid",
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
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "*.jks",
        "*.keystore",
        "node_modules",
    ],
)
def test_docker_context_requires_sensitive_file_exclusions(
    tmp_path: Path,
    missing_pattern: str,
) -> None:
    patterns = (
        ".streamlit/secrets.toml",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "*.jks",
        "*.keystore",
        "node_modules",
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
        (
            ".streamlit/secrets.toml\n*.pem\n*.key\n*.p12\n*.pfx\n*.jks\n*.keystore\n"
            "node_modules\n!certificates/release.p12\n"
        ),
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
    assert "release_topology_invalid" in codes


def test_release_workflow_digest_rejects_any_byte_change(tmp_path: Path) -> None:
    path = _mutated_release_workflow(
        tmp_path,
        before="name: Release evidence\n",
        after="name: Release evidence # unreviewed byte\n",
    )

    assert hashlib.sha256(path.read_bytes()).hexdigest() != supply_chain._RELEASE_WORKFLOW_SHA256
    assert "release_workflow_not_exact" in {finding.code for finding in verify_workflows(tmp_path)}


def test_m30_manifest_attestation_workflow_is_exact_and_tamper_evident(
    tmp_path: Path,
) -> None:
    current_codes = {
        finding.code
        for finding in verify_workflows(ROOT)
        if finding.path == ".github/workflows/m30-manifest-attestation.yml"
        or finding.path.startswith(".github/workflows/m30-manifest-attestation.yml:")
    }
    assert current_codes == set()

    workflow_directory = tmp_path / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    release = ROOT / ".github" / "workflows" / "release-evidence.yml"
    (workflow_directory / "release-evidence.yml").write_bytes(release.read_bytes())
    source = (ROOT / ".github" / "workflows" / "m30-manifest-attestation.yml").read_text(
        encoding="utf-8"
    )
    (workflow_directory / "m30-manifest-attestation.yml").write_text(
        source.replace(
            "    environment: m30-manifest-attestation\n",
            "    environment: production-release\n",
            1,
        ),
        encoding="utf-8",
    )

    codes = {finding.code for finding in verify_workflows(tmp_path)}
    assert "m30_campaign_workflow_not_exact" in codes
    assert "m30_campaign_workflow_topology_invalid" in codes
    assert "release_permission_unprotected" in codes


def test_m30_manifest_attestation_workflow_absence_fails_closed(tmp_path: Path) -> None:
    workflow_directory = tmp_path / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    release = ROOT / ".github" / "workflows" / "release-evidence.yml"
    (workflow_directory / "release-evidence.yml").write_bytes(release.read_bytes())

    codes = {finding.code for finding in verify_workflows(tmp_path)}

    assert "m30_manifest_attestation_missing" in codes


def test_m30_write_scoped_job_cannot_execute_candidate_code(tmp_path: Path) -> None:
    workflow_directory = tmp_path / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    release = ROOT / ".github" / "workflows" / "release-evidence.yml"
    (workflow_directory / "release-evidence.yml").write_bytes(release.read_bytes())
    source = (ROOT / ".github" / "workflows" / "m30-manifest-attestation.yml").read_text(
        encoding="utf-8"
    )
    marker = '          test -n "$ATTESTATION_ID"\n'
    assert source.count(marker) == 1
    mutated = source.replace(
        marker,
        "          make check\n" + marker,
        1,
    )
    (workflow_directory / "m30-manifest-attestation.yml").write_text(
        mutated,
        encoding="utf-8",
    )

    codes = {finding.code for finding in verify_workflows(tmp_path)}
    assert "m30_campaign_workflow_not_exact" in codes
    assert "m30_campaign_signing_code_invalid" in codes


@pytest.mark.parametrize(
    ("target", "variable", "required_arguments"),
    (
        ("m30-manifest-validate", "M30_MANIFEST", ()),
        (
            "m30-authenticate-manifest",
            "M30_ATTESTATION_BUNDLE",
            ("M30_MANIFEST=/external/manifest.json",),
        ),
        (
            "m30-authenticate-manifest",
            "M30_AUTHENTICATION_OUTPUT",
            (
                "M30_MANIFEST=/external/manifest.json",
                "M30_ATTESTATION_BUNDLE=/external/attestation.jsonl",
            ),
        ),
    ),
)
def test_m30_make_path_arguments_are_not_shell_interpreted(
    tmp_path: Path,
    target: str,
    variable: str,
    required_arguments: tuple[str, ...],
) -> None:
    sentinel = tmp_path / f"{variable.casefold()}-shell-injection-must-not-run"
    hostile_path = f"{tmp_path}/manifest'$$(touch {sentinel})'json"
    environment = os.environ.copy()
    for key in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL"):
        environment.pop(key, None)

    completed = subprocess.run(
        (
            "make",
            "--no-print-directory",
            target,
            *required_arguments,
            f"{variable}={hostile_path}",
        ),
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode != 0
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("before", "after", "expected_code"),
    [
        (
            "  group: schemabridge-global-release-publication\n",
            "  group: release-${{ inputs.release_tag }}\n",
            "release_topology_invalid",
        ),
        (
            "  queue: max\n",
            "  queue: single\n",
            "release_topology_invalid",
        ),
        (
            "        description: Existing annotated canonical SemVer tag to promote\n",
            "        description: Any tag to promote\n",
            "release_topology_invalid",
        ),
        (
            '          test "$REF_TYPE" = "tag"\n',
            "",
            "release_prepare_policy_invalid",
        ),
        (
            "  prepare:\n",
            "  prepare:\n    environment: production-release\n",
            "release_authority_boundary_invalid",
        ),
        (
            "  candidate:\n    needs: prepare\n    runs-on: ubuntu-24.04\n",
            "  candidate:\n    needs: prepare\n    runs-on: ubuntu-latest\n",
            "release_authority_boundary_invalid",
        ),
        (
            "  candidate:\n    needs: prepare\n",
            "  candidate:\n",
            "release_authority_boundary_invalid",
        ),
        (
            "      CANDIDATE_TAG: candidate-${{ github.sha }}\n",
            "      CANDIDATE_TAG: candidate-${{ github.run_id }}-${{ github.run_attempt }}\n",
            "release_rerun_identity_invalid",
        ),
        (
            "      ARTIFACT_ID: ${{ needs.prepare.outputs.artifact-id }}\n",
            "      ARTIFACT_ID: ${{ github.run_id }}\n",
            "release_rerun_identity_invalid",
        ),
        (
            "      ARTIFACT_ID: ${{ needs.scan.outputs.artifact-id }}\n",
            "      ARTIFACT_ID: ${{ github.run_id }}\n",
            "release_rerun_identity_invalid",
        ),
        (
            '          test "$(sha256sum "$archive_path" | cut -d\' \' -f1)" = "$ARTIFACT_DIGEST"\n',
            "",
            "release_boundary_verification_invalid",
        ),
        (
            '          test "$(jq -r \'.digest\' <<<"$artifact_payload")" = "sha256:$ARTIFACT_DIGEST"\n',
            "",
            "release_boundary_verification_invalid",
        ),
        (
            '                jq -r \'.conditions.ref_name.include == ["refs/tags/v*"]\n',
            '                jq -r \'.conditions.ref_name.include == ["refs/tags/*"]\n',
            "release_prepare_policy_invalid",
        ),
        (
            '                    and has("bypass_actors")\n',
            "",
            "release_ruleset_audit_invalid",
        ),
        (
            '          test "$(remote_tag_commit)" = "$SOURCE_REVISION"\n',
            "",
            "release_prepare_policy_invalid",
        ),
        (
            '            if [[ "$code" = "404" ]]; then\n',
            '            if [[ "$code" = "401" || "$code" = "404" ]]; then\n',
            "release_audit_preflight_invalid",
        ),
        (
            "          release_matches() {\n",
            '          gh release view "$RELEASE_TAG" >/dev/null 2>&1 || true\n'
            "          release_matches() {\n",
            "release_opcode_forbidden",
        ),
        (
            "          release_matches() {\n",
            '          gh release delete "$RELEASE_TAG" --yes\n          release_matches() {\n',
            "release_opcode_forbidden",
        ),
        (
            '          archive_path="$RUNNER_TEMP/prepared-release.zip"\n',
            "          python scripts/verify_supply_chain.py static\n"
            '          archive_path="$RUNNER_TEMP/prepared-release.zip"\n',
            "release_unsealed_code_execution",
        ),
        (
            "jobs:\n",
            "jobs:\n  attacker: {runs-on: ubuntu-latest, steps: []}\n",
            "release_topology_invalid",
        ),
        (
            "    steps:\n"
            "      - name: Verify prepared artifact and release boundary before first package mutation\n",
            "    steps:\n      - run: echo unreviewed\n"
            "      - name: Verify prepared artifact and release boundary before first package mutation\n",
            "release_step_program_invalid",
        ),
        (
            '          lock_digest="$(jq -er \'.uv_lock_sha256 | select(test("^[0-9a-f]{64}$"))\' "$manifest")"\n',
            '          test -n "$ARTIFACT_ID"\n'
            '          lock_digest="$(jq -er \'.uv_lock_sha256 | select(test("^[0-9a-f]{64}$"))\' "$manifest")"\n',
            "release_evidence_determinism_invalid",
        ),
        (
            '          test "$registry_components" = "$prepared_components"\n',
            '          test "$registry_components" = "$untrusted_components"\n',
            "release_evidence_determinism_invalid",
        ),
        (
            '          SOURCE_DATE_EPOCH: "1730470033"\n',
            '          SOURCE_DATE_EPOCH: "0"\n',
            "release_evidence_determinism_invalid",
        ),
        (
            '            --arg body_sha256 "$(\n',
            '            --arg ignored_body_sha256 "$(\n',
            "release_body_contract_invalid",
        ),
        (
            '            "- Source: \\`$SOURCE_REVISION\\`" \\\n',
            '            "- Revision: \\`$SOURCE_REVISION\\`" \\\n',
            "release_body_contract_invalid",
        ),
        (
            '            test "$(jq -r \'.immutable\' <<<"$release")" = "true"\n',
            "",
            "release_audit_preflight_invalid",
        ),
        (
            "                and ([.[].name] | unique | length) == 10\n",
            "",
            "release_audit_preflight_invalid",
        ),
        (
            "            --bundle-from-oci\n",
            "",
            "release_audit_preflight_invalid",
        ),
        (
            "            --draft=false \\\n",
            "            --draft=true \\\n",
            "release_latest_policy_invalid",
        ),
        (
            '          if [[ "$(jq \'length\' <<<"$releases")" = "0" ]]; then\n'
            "            reject_newer_stable_release\n",
            '          if [[ "$(jq \'length\' <<<"$releases")" = "0" ]]; then\n',
            "release_latest_policy_invalid",
        ),
        (
            "          verify_default_head\n"
            "          verify_authoritative_release_controls\n"
            '          releases="$(release_matches)"\n',
            "          verify_authoritative_release_controls\n"
            '          releases="$(release_matches)"\n',
            "release_default_head_invalid",
        ),
        (
            '          release="$(fetch_release "$release_id")"\n'
            '          verify_postpublication_current_latest "$release" "$release_id"\n',
            '          verify_postpublication_current_latest "$release" "$release_id"\n',
            "release_latest_policy_invalid",
        ),
        (
            "              --request PUT \\\n",
            "              --request GET \\\n",
            "release_promotion_order_invalid",
        ),
        (
            "      contents: write\n      packages: read\n",
            "      contents: write\n      packages: write\n",
            "release_authority_boundary_invalid",
        ),
        (
            '          gh release upload "$RELEASE_TAG" "$RELEASE_PAYLOAD_DIRECTORY/$filename"\n',
            '          gh release upload "$RELEASE_TAG" "$RELEASE_PAYLOAD_DIRECTORY/$filename" --clobber\n',
            "release_asset_contract_invalid",
        ),
        (
            "                gh api --method GET --paginate --slurp \\\n"
            '                  --header "X-GitHub-Api-Version: $GH_API_VERSION" \\\n'
            '                  "repos/$GITHUB_REPOSITORY/rulesets?includes_parents=true&targets=tag&per_page=100" |\n',
            "                gh api --method POST --paginate --slurp \\\n"
            '                  --header "X-GitHub-Api-Version: $GH_API_VERSION" \\\n'
            '                  "repos/$GITHUB_REPOSITORY/rules/tags/$RELEASE_TAG" |\n',
            "release_ruleset_audit_invalid",
        ),
        (
            "          set -euo pipefail\n",
            "          set +e\n",
            "release_strict_shell_invalid",
        ),
        (
            "                      or entry.flag_bits & 0x1\n",
            "",
            "release_zip_validation_invalid",
        ),
        (
            "              corrupt = payload.testzip()\n",
            "              corrupt = None\n",
            "release_zip_validation_invalid",
        ),
        (
            "                  mode = (entry.external_attr >> 16) & 0xFFFF\n",
            "                  mode = 0\n",
            "release_zip_validation_invalid",
        ),
        (
            "                      or entry.file_size / entry.compress_size > 100\n",
            "",
            "release_zip_validation_invalid",
        ),
        (
            '          test ! -e "$PREPARED_DIRECTORY"\n',
            '          mkdir -p "$PREPARED_DIRECTORY"\n',
            "release_zip_validation_invalid",
        ),
        (
            '          DOCKER_BUILD_RECORD_UPLOAD: "false"\n',
            '          DOCKER_BUILD_RECORD_UPLOAD: "true"\n',
            "release_evidence_determinism_invalid",
        ),
        (
            '            --arg release_body_sha256 "$release_body_digest" \\\n',
            '            --arg ignored_body_sha256 "$release_body_digest" \\\n',
            "release_body_contract_invalid",
        ),
        (
            "            .local/release-payload/release-body.md\n",
            "",
            "release_attestation_verification_invalid",
        ),
        (
            '          if [[ "$(jq -r \'.draft\' <<<"$release")" = "false" ]]; then\n'
            '            verify_exact_published_release "$release" "$release_id"\n',
            '          if [[ "$(jq -r \'.draft\' <<<"$release")" = "false" ]]; then\n'
            '            gh release edit "$RELEASE_TAG" --latest\n'
            '            verify_exact_published_release "$release" "$release_id"\n',
            "release_latest_policy_invalid",
        ),
        (
            '            test "${candidate_state##*|}" = "$LOCAL_IMAGE_ID"\n',
            '            test "${candidate_state##*|}" = "$UNTRUSTED_IMAGE_ID"\n',
            "release_partial_dispatch_invalid",
        ),
        (
            "          release_matches() {\n",
            "          gh api --method POST repos/example/releases\n"
            "          release_matches() {\n",
            "release_opcode_forbidden",
        ),
        (
            "          release_matches() {\n",
            "          curl --request DELETE https://example.invalid/manifest\n"
            "          release_matches() {\n",
            "release_opcode_forbidden",
        ),
        (
            "          release_matches() {\n",
            "          git push origin main\n          release_matches() {\n",
            "release_opcode_forbidden",
        ),
        (
            "          release_matches() {\n",
            '          eval "$unreviewed_payload"\n          release_matches() {\n',
            "release_opcode_forbidden",
        ),
        (
            "          release_matches() {\n",
            "          bash -c 'echo unreviewed'\n          release_matches() {\n",
            "release_opcode_forbidden",
        ),
        (
            "          release_matches() {\n",
            "          if false; then echo unreachable; fi\n          release_matches() {\n",
            "release_opcode_forbidden",
        ),
        (
            "          release_matches() {\n",
            "          docker push ghcr.io/example/unreviewed\n          release_matches() {\n",
            "release_opcode_forbidden",
        ),
        (
            "          release_matches() {\n",
            "          curl --request PUT https://example.invalid/manifest\n"
            "          release_matches() {\n",
            "release_opcode_forbidden",
        ),
        (
            '          archive_path="$RUNNER_TEMP/prepared-release.zip"\n',
            '          gh release create "$RELEASE_TAG" --draft\n'
            '          archive_path="$RUNNER_TEMP/prepared-release.zip"\n',
            "release_opcode_forbidden",
        ),
        (
            "      published-noop: ${{ steps.preflight.outputs.published-noop }}\n",
            "",
            "release_audit_preflight_invalid",
        ),
        (
            "              printf '%s\\n' \"a newer stable release already exists\" >&2\n",
            "              printf '%s\\n' \"ignored newer release\" >&2\n",
            "release_audit_preflight_invalid",
        ),
        (
            "              printf '%s\\n' \"a newer stable registry tag already exists\" >&2\n",
            "              printf '%s\\n' \"ignored newer registry tag\" >&2\n",
            "release_audit_preflight_invalid",
        ),
        (
            '          registry_tags_url="https://ghcr.io/v2/$registry_path/tags/list?n=100"\n',
            '          registry_tags_url="https://ghcr.io/v2/$registry_path/tags/list?n=1"\n',
            "release_audit_preflight_invalid",
        ),
        (
            "                --max-filesize 4194304 \\\n",
            "                --max-filesize 999999999 \\\n",
            "release_registry_state_invalid",
        ),
        (
            "          verify_checksum_manifest_exact() {\n",
            "          verify_untrusted_checksum_manifest() {\n",
            "release_checksum_manifest_invalid",
        ),
        (
            '          test "$trivy_version" = "0.69.3"\n',
            '          test -n "$trivy_version"\n',
            "release_scanner_snapshot_invalid",
        ),
        (
            "              --vulnerability-service pypi \\\n",
            "              --vulnerability-service osv \\\n",
            "release_scanner_snapshot_invalid",
        ),
        (
            '          database_sha256="$(sha256sum "$database_file" | cut -d\' \' -f1)"\n',
            '          database_sha256="$(printf ignored)"\n',
            "release_scanner_snapshot_invalid",
        ),
        (
            "      DOCKER_CONFIG: .local/trivy-docker-config\n",
            "      DOCKER_CONFIG: .docker\n",
            "release_scan_auth_invalid",
        ),
        (
            "              if docker logout ghcr.io; then\n",
            "              if printf 'cleanup skipped\\n'; then\n",
            "release_scan_auth_invalid",
        ),
        (
            '          if rm -f "$DOCKER_CONFIG/config.json"; then\n',
            '          if printf "credential retained\\n"; then\n',
            "release_scan_auth_invalid",
        ),
        (
            "      - name: Generate the registry-image CycloneDX SBOM\n"
            "        uses: aquasecurity/trivy-action@57a97c7e7821a5776cebc9bb87c984fa69cba8f1 # v0.35.0\n",
            "      - name: Generate the registry-image CycloneDX SBOM\n"
            "        uses: actions/cache@5a3ec84eff668545956fd18022155c47e93e2684 # v4.2.3\n",
            "release_action_allowlist_invalid",
        ),
    ],
)
def test_release_semantic_validator_rejects_mutation_after_self_hash_reseal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before: str,
    after: str,
    expected_code: str,
) -> None:
    path = _mutated_release_workflow(tmp_path, before=before, after=after)
    monkeypatch.setattr(
        supply_chain,
        "_RELEASE_WORKFLOW_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    codes = {finding.code for finding in verify_workflows(tmp_path)}
    assert "release_workflow_not_exact" not in codes
    assert expected_code in codes


def test_failed_downstream_release_jobs_reuse_exact_upstream_outputs() -> None:
    workflow = (ROOT / ".github/workflows/release-evidence.yml").read_text(encoding="utf-8")
    candidate = workflow.split("\n  candidate:\n", maxsplit=1)[1].split("\n  scan:\n", maxsplit=1)[
        0
    ]
    scan = workflow.split("\n  scan:\n", maxsplit=1)[1].split("\n  attest:\n", maxsplit=1)[0]
    downstream = workflow.split("\n  attest:\n", maxsplit=1)[1]
    derivation = scan.split(
        "      - name: Derive canonical public evidence using runner tools\n", maxsplit=1
    )[1].split("      - name: Upload the immutable canonical release payload\n", maxsplit=1)[0]

    assert "ARTIFACT_ID: ${{ needs.prepare.outputs.artifact-id }}" in candidate
    assert "ARTIFACT_DIGEST: ${{ needs.prepare.outputs.artifact-digest }}" in candidate
    assert "CANDIDATE_TAG: candidate-${{ github.sha }}" in candidate
    assert "ARTIFACT_ID: ${{ needs.prepare.outputs.artifact-id }}" in scan
    assert "IMAGE_DIGEST: ${{ needs.candidate.outputs.image-digest }}" in scan
    assert downstream.count("ARTIFACT_ID: ${{ needs.scan.outputs.artifact-id }}") == 3
    assert downstream.count("CANDIDATE_TAG: ${{ needs.scan.outputs.candidate-tag }}") == 3
    assert not any(
        token in derivation
        for token in (
            "$ARTIFACT_ID",
            "$ARTIFACT_DIGEST",
            "$GITHUB_RUN_ID",
            "$GITHUB_RUN_ATTEMPT",
        )
    )


def test_actual_workflow_fails_full_redispatch_before_regeneration_but_reuses_job_outputs() -> None:
    workflow = (ROOT / ".github/workflows/release-evidence.yml").read_text(encoding="utf-8")
    audit = workflow.split("\n  audit:\n", maxsplit=1)[1].split("\n  prepare:\n", maxsplit=1)[0]
    prepare = workflow.split("\n  prepare:\n", maxsplit=1)[1].split(
        "\n  candidate:\n",
        maxsplit=1,
    )[0]
    candidate = workflow.split("\n  candidate:\n", maxsplit=1)[1].split(
        "\n  scan:\n",
        maxsplit=1,
    )[0]
    scan = workflow.split("\n  scan:\n", maxsplit=1)[1].split("\n  attest:\n", maxsplit=1)[0]
    publish = candidate.split(
        "      - name: Publish the new stable semantic candidate\n",
        maxsplit=1,
    )[1]

    assert "registry reference already exists before this dispatch" in audit
    assert "a newer stable release already exists" in audit
    assert "a newer stable registry tag already exists" in audit
    assert "published-noop=true" in audit
    assert prepare.index("verify_public_external_state_metadata") < prepare.index(
        "      - name: Build the unpublished runtime image once"
    )
    assert "candidate-$SOURCE_REVISION" in prepare
    assert "stable tag already exists; dispatch is not clean" in publish
    assert "adopting the exact candidate created by this run after a retry" in publish
    assert publish.index('test "${candidate_state##*|}" = "$LOCAL_IMAGE_ID"') < publish.index(
        'docker image tag "schemabridge-runtime:$SOURCE_REVISION"'
    )
    assert "Trivy" not in publish
    assert "aquasecurity/trivy-action@" not in candidate
    assert scan.count("aquasecurity/trivy-action@") == 2
    assert "docker login ghcr.io" in scan
    assert "docker logout ghcr.io" in scan
    assert "ARTIFACT_ID: ${{ needs.prepare.outputs.artifact-id }}" in candidate


def test_exact_immutable_published_release_replay_is_historical_and_read_only() -> None:
    workflow = (ROOT / ".github/workflows/release-evidence.yml").read_text(encoding="utf-8")
    audit = workflow.split("\n  audit:\n", maxsplit=1)[1].split("\n  prepare:\n", maxsplit=1)[0]
    audit_verifier = audit.split("          verify_exact_published_release() {\n", maxsplit=1)[
        1
    ].split("          }\n", maxsplit=1)[0]
    release_operation = workflow.split(
        "      - name: Create, resume, or publish the canonical release as the final operation\n",
        maxsplit=1,
    )[1]
    published_branch = release_operation.split(
        '          if [[ "$(jq -r \'.draft\' <<<"$release")" = "false" ]]; then\n',
        maxsplit=1,
    )[1].split("          fi\n", maxsplit=1)[0]
    published_verifier = release_operation.split(
        "          verify_exact_published_release() {\n",
        maxsplit=1,
    )[1].split("          }\n", maxsplit=1)[0]

    for mutation in (
        "gh release create",
        "gh release upload",
        "gh release edit",
        "reconcile_draft_assets",
        "--request PUT",
        "docker push",
    ):
        assert mutation not in published_branch
        assert mutation not in published_verifier
        assert mutation not in audit_verifier
    assert "verify_exact_published_release" in published_branch
    assert "exit 0" in published_branch
    assert "gh attestation verify" in audit_verifier
    assert "sha256sum --strict --check release-assets.sha256" in audit_verifier
    assert audit_verifier.index("gh attestation verify") < audit_verifier.index(
        "sha256sum --strict --check release-assets.sha256"
    )
    assert "releases/latest" not in audit_verifier
    assert "reject_newer_stable_release" not in audit_verifier
    assert "git/ref/heads/main" not in audit_verifier


def test_release_topology_separates_write_candidate_from_read_only_scan() -> None:
    workflow = (ROOT / ".github/workflows/release-evidence.yml").read_text(encoding="utf-8")
    jobs = {
        name: workflow.split(f"\n  {name}:\n", maxsplit=1)[1].split(
            f"\n  {next_name}:\n", maxsplit=1
        )[0]
        for name, next_name in (
            ("audit", "prepare"),
            ("prepare", "candidate"),
            ("candidate", "scan"),
            ("scan", "attest"),
            ("attest", "promote"),
            ("promote", "release"),
        )
    }
    jobs["release"] = workflow.split("\n  release:\n", maxsplit=1)[1]

    assert "environment: production-release" in jobs["audit"]
    assert "needs: audit" in jobs["prepare"]
    assert "if: needs.audit.outputs.published-noop != 'true'" in jobs["prepare"]
    assert "\n      - uses:" not in jobs["candidate"]
    assert "packages: write" in jobs["candidate"]
    assert "packages: read" in jobs["scan"]
    assert "packages: write" not in jobs["scan"]
    assert jobs["scan"].count("aquasecurity/trivy-action@") == 2
    assert "docker login ghcr.io" in jobs["scan"]
    assert "docker logout ghcr.io" in jobs["scan"]


def test_scan_cleanup_removes_credential_even_when_docker_logout_fails(
    tmp_path: Path,
) -> None:
    workflow = (ROOT / ".github/workflows/release-evidence.yml").read_text(encoding="utf-8")
    scan = workflow.split("\n  scan:\n", maxsplit=1)[1].split("\n  attest:\n", maxsplit=1)[0]
    cleanup_step = scan.split(
        "      - name: Remove the read-only registry credential after scanning\n",
        maxsplit=1,
    )[1].split(
        "      - name: Derive canonical public evidence using runner tools\n",
        maxsplit=1,
    )[0]
    cleanup_script = textwrap.dedent(cleanup_step.split("        run: |\n", maxsplit=1)[1])
    config_directory = tmp_path / "docker-config"
    config_directory.mkdir()
    (config_directory / "config.json").write_text(
        '{"auths":{"ghcr.io":{"auth":"redacted"}}}',
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 17\n", encoding="utf-8")
    fake_docker.chmod(0o700)
    fake_jq = fake_bin / "jq"
    fake_jq.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_jq.chmod(0o700)

    result = subprocess.run(
        ["bash", "-c", cleanup_script],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DOCKER_CONFIG": str(config_directory),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 1
    assert not config_directory.exists()


def test_release_docs_require_main_freeze_and_long_lived_get_only_audit_pat() -> None:
    paths = (
        "docs/02_ARCHITECTURE.md",
        "docs/06_SECURITY.md",
        "docs/07_TEST_STRATEGY.md",
        "docs/12_RUNBOOK.md",
        "docs/14_DEPLOYMENT.md",
        "docs/adr/0014-operated-runtime-secrets-observability-and-supply-chain.md",
    )

    for relative in paths:
        document = (ROOT / relative).read_text(encoding="utf-8")
        normalized = " ".join(document.split())
        assert "`main`" in normalized and "freeze" in normalized
        assert "fine-grained PAT" in normalized
        assert "GitHub App installation token" in normalized


def test_actual_audit_semver_comparator_handles_huge_versions_and_dual_ledger() -> None:
    workflow = (ROOT / ".github/workflows/release-evidence.yml").read_text(encoding="utf-8")
    audit = workflow.split("\n  audit:\n", maxsplit=1)[1].split("\n  prepare:\n", maxsplit=1)[0]
    function_block = audit.split("          canonical_stable_semver() {\n", maxsplit=1)[1].split(
        "          test \"$(\n            jq -r '[.[] | select(.draft == true)] | length == 0'",
        maxsplit=1,
    )[0]
    functions = textwrap.dedent("canonical_stable_semver() {\n" + function_block)
    result = subprocess.run(
        [
            "bash",
            "-c",
            functions
            + "\nsemver_greater "
            + "v999999999999999999999999999999.0.0 v2.0.0"
            + "\n! semver_greater v2.0.0 v10.0.0",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert audit.index("published-noop=true") < audit.index(
        "[.[] | select(.draft == true)] | length == 0"
    )
    assert "a newer stable release already exists" in audit
    assert "a newer stable registry tag already exists" in audit
    assert "/tags/list?n=100" in audit
    assert "ambiguous registry pagination" in audit
    assert "unsafe registry next link" in audit


def test_release_checksum_and_scanner_snapshot_contracts_are_closed() -> None:
    workflow = (ROOT / ".github/workflows/release-evidence.yml").read_text(encoding="utf-8")

    assert workflow.count("verify_checksum_manifest_exact() {") == 5
    assert workflow.count('test "$(stat --format=\'%s\' "$checksum_file")" -le 4096') == 5
    assert workflow.count(r"^([0-9a-f]{64})\ \ ([A-Za-z0-9][A-Za-z0-9._-]*)$") >= 6
    assert "--vulnerability-service pypi" in workflow
    assert 'test "$pip_audit_version" = "2.10.1"' in workflow
    assert 'test "$trivy_version" = "0.69.3"' in workflow
    assert ".local/trivy-cache/db/metadata.json" in workflow
    assert ".local/trivy-cache/db/trivy.db" in workflow
    assert "database_sha256" in workflow
    assert "metadata_sha256" in workflow
    assert "scanner_evidence" in workflow


def test_static_gate_scans_candidate_secrets_artifacts_and_architecture() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    static_target = makefile.split("supply-chain-static:", maxsplit=1)[1].split("\n\n", maxsplit=1)[
        0
    ]

    assert "$(BIN)/python scripts/verify_supply_chain.py static" in static_target
    assert "$(BIN)/python scripts/release_audit.py" in static_target
    assert "--check-external" not in static_target


def test_runtime_base_image_requires_the_exact_reviewed_subject(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile.runtime").write_text(
        "FROM python:3.13.13-slim-bookworm@"
        "sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f\n"
        "COPY requirements/build.txt requirements/build.txt\n"
        "COPY requirements/runtime.txt requirements/runtime.txt\n"
        "RUN pip install --require-hashes --no-deps --no-build-isolation\n",
        encoding="utf-8",
    )

    assert "runtime_base_image_unreviewed" in {finding.code for finding in verify_images(tmp_path)}


def test_runtime_install_requires_the_verified_offline_wheelhouse(tmp_path: Path) -> None:
    source = (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    (requirements / "runtime-built.txt").write_text(
        (ROOT / "requirements" / "runtime-built.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile.runtime").write_text(
        source.replace("--no-index", "--index-url https://pypi.org/simple", 1),
        encoding="utf-8",
    )

    assert "runtime_install_not_offline" in {finding.code for finding in verify_images(tmp_path)}


@pytest.mark.parametrize(
    ("before", "after", "expected_code"),
    [
        (
            "SOURCE_DATE_EPOCH=1730470033",
            "SOURCE_DATE_EPOCH=0",
            "runtime_install_not_reproducible",
        ),
        (
            "RUN --mount=from=builder,source=/tmp/runtime-wheels,"
            "target=/tmp/runtime-wheels,ro \\\n",
            "COPY --from=builder /tmp/runtime-wheels /tmp/runtime-wheels\nRUN ",
            "runtime_wheelhouse_layer_retained",
        ),
    ],
)
def test_runtime_wheel_build_is_reproducible_and_does_not_retain_the_wheelhouse(
    tmp_path: Path,
    before: str,
    after: str,
    expected_code: str,
) -> None:
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    (requirements / "runtime-built.txt").write_text(
        (ROOT / "requirements" / "runtime-built.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    source = (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    assert before in source
    (tmp_path / "Dockerfile.runtime").write_text(
        source.replace(before, after, 1),
        encoding="utf-8",
    )

    assert expected_code in {finding.code for finding in verify_images(tmp_path)}


def test_runtime_built_watchdog_requirement_is_exactly_hash_bound(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile.runtime").write_text(
        (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    source = (ROOT / "requirements" / "runtime-built.txt").read_text(encoding="utf-8")
    (requirements / "runtime-built.txt").write_text(
        source.replace("4b510ffee66be0c7", "0b510ffee66be0c7", 1),
        encoding="utf-8",
    )

    assert "runtime_built_requirement_invalid" in {
        finding.code for finding in verify_images(tmp_path)
    }


def test_runtime_watchdog_build_backend_is_exactly_hash_bound(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile.runtime").write_text(
        (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    source = (ROOT / "requirements" / "watchdog-build.txt").read_text(encoding="utf-8")
    (requirements / "watchdog-build.txt").write_text(
        source.replace("29b23c360f22f414", "09b23c360f22f414", 1),
        encoding="utf-8",
    )

    assert "runtime_build_requirement_invalid" in {
        finding.code for finding in verify_images(tmp_path)
    }


def test_runtime_stage_rejects_an_additional_network_install(tmp_path: Path) -> None:
    source = (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    (tmp_path / "Dockerfile.runtime").write_text(
        source.replace(
            "USER 10001:10001\n",
            "RUN python -m pip install unreviewed-package\nUSER 10001:10001\n",
            1,
        ),
        encoding="utf-8",
    )

    assert "runtime_commands_unreviewed" in {finding.code for finding in verify_images(tmp_path)}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "USER 10001:10001\n",
            "run wget -qO /opt/schemabridge/payload https://example.invalid/payload\n"
            "USER 10001:10001\n",
        ),
        (
            "USER 10001:10001\n",
            "Run printf unreviewed >/opt/schemabridge/payload\nUSER 10001:10001\n",
        ),
        (
            "USER 10001:10001\n\nEXPOSE",
            "USER 10001:10001\nUSER 0\n\nEXPOSE",
        ),
        (
            "USER 10001:10001\n",
            "COPY --chmod=755 payload /usr/local/bin/schemabridge-api\nUSER 10001:10001\n",
        ),
        (
            "ARG SCHEMABRIDGE_RELEASE_REF=release-ref-not-supplied\n",
            "ADD https://example.invalid/payload /tmp/payload\n"
            "ARG SCHEMABRIDGE_RELEASE_REF=release-ref-not-supplied\n",
        ),
        (
            'CMD ["schemabridge-api"]',
            'ENTRYPOINT ["/opt/schemabridge/payload"]\nCMD ["schemabridge-api"]',
        ),
        (
            "USER 10001:10001\n",
            'SHELL ["/bin/sh", "-c"]\nONBUILD RUN echo unsafe\nUSER 10001:10001\n',
        ),
        (
            "COPY migrations ./migrations\n",
            "COPY migrations ./migrations\nRUN wget -qO /tmp/payload "
            "https://example.invalid/payload\n",
        ),
        (
            "\nFROM python:3.13.14-alpine3.24@sha256:"
            "399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0\n",
            "\nFROM python:3.13.14-alpine3.24@sha256:"
            "399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0 "
            "AS unreviewed\nRUN echo unsafe\n"
            "FROM python:3.13.14-alpine3.24@sha256:"
            "399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0\n",
        ),
    ],
)
def test_runtime_all_stages_reject_case_and_instruction_bypasses(
    tmp_path: Path,
    before: str,
    after: str,
) -> None:
    source = (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    assert before in source
    (tmp_path / "Dockerfile.runtime").write_text(
        source.replace(before, after, 1),
        encoding="utf-8",
    )

    assert "runtime_commands_unreviewed" in {finding.code for finding in verify_images(tmp_path)}


def test_runtime_postgres_client_apk_matrix_is_exact_for_both_architectures() -> None:
    assert POSTGRES_CLIENT_APK_MATRIX == {
        "amd64": (
            (
                "libpq",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libpq-18.4-r0.apk",
                "145d0d57ce40baaf2d7191e66dc18b8872822642939a0264fd4fc1c73d6599f1",
            ),
            (
                "lz4",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/lz4-libs-1.10.0-r1.apk",
                "3ad4912ab8ecf5f6236fdb751f88243006b3a8f152684e455b781e16284dd298",
            ),
            (
                "postgresql_common",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/"
                "postgresql-common-1.3-r0.apk",
                "749d8a88b56c84b415372655435600c749345113e21ce8fe9964f7e22345cbff",
            ),
            (
                "postgresql_client",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/community/x86_64/"
                "postgresql16-client-16.14-r0.apk",
                "21409f4ee297e6805a6688ba7fc91c20f66f972e79865db42b550348b20381d5",
            ),
            (
                "zstd",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/zstd-libs-1.5.7-r2.apk",
                "23c6065b0049b2406441564bcf0032515a43f78e80d76fcb85535a3803ef5d4e",
            ),
        ),
        "arm64": (
            (
                "libpq",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/libpq-18.4-r0.apk",
                "d6e4808216810535808523980a78608569be2318cf05e6034ffb03f519e91a56",
            ),
            (
                "lz4",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/lz4-libs-1.10.0-r1.apk",
                "af6cbe553adda941e04ac40cf0123455b714d65392472389113e7d6dcebbd8d8",
            ),
            (
                "postgresql_common",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/"
                "postgresql-common-1.3-r0.apk",
                "ecddbd6272f5034f30d78ff59634c07f862a13a2ad3d29078c0e7985b827fc73",
            ),
            (
                "postgresql_client",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/community/aarch64/"
                "postgresql16-client-16.14-r0.apk",
                "d1a0095b8a4a42bd46136bec4117a64fc1f5282a0c46a01ff5642ad14bada995",
            ),
            (
                "zstd",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/zstd-libs-1.5.7-r2.apk",
                "2bb5136c89f5b0bbe1554c8915a3b520d5aa63ae2a51d4d821eb81698db5a818",
            ),
        ),
    }


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libpq-18.4-r0.apk",
            "https://mirror.example.invalid/alpine/v3.24/main/x86_64/libpq-18.4-r0.apk",
        ),
        ("libpq-18.4-r0.apk", "libpq-18.4-r1.apk"),
        (
            "145d0d57ce40baaf2d7191e66dc18b8872822642939a0264fd4fc1c73d6599f1",
            "045d0d57ce40baaf2d7191e66dc18b8872822642939a0264fd4fc1c73d6599f1",
        ),
        ("amd64) \\", "x86_64) \\"),
        ("arm64) \\", "aarch64) \\"),
        (
            "target=/postgres-client-apks,ro \\",
            "target=/postgres-client-apks,rw \\",
        ),
        (
            "RUN --mount=from=postgres-client-apks,source=/postgres-client-apks,"
            "target=/postgres-client-apks,ro \\\n"
            "    apk add",
            "COPY --from=postgres-client-apks /postgres-client-apks /postgres-client-apks\n"
            "RUN apk add",
        ),
        ("apk add --no-cache --no-network \\", "apk add --no-cache \\"),
        (
            "apk add --no-cache --no-network \\",
            "apk add --allow-untrusted --no-cache --no-network \\",
        ),
        (
            'fetch_apk "$zstd_url" "$zstd_sha256"',
            'wget https://example.invalid/extra.apk; fetch_apk "$zstd_url" "$zstd_sha256"',
        ),
        (
            "RUN adduser -D -u 10001 schemabridge",
            "RUN apk add postgresql16-client=16.14-r0\nRUN adduser -D -u 10001 schemabridge",
        ),
    ],
)
def test_runtime_postgres_client_rejects_any_matrix_or_offline_install_change(
    tmp_path: Path,
    before: str,
    after: str,
) -> None:
    source = (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    assert before in source
    (tmp_path / "Dockerfile.runtime").write_text(
        source.replace(before, after, 1),
        encoding="utf-8",
    )

    assert "runtime_postgres_client_unreviewed" in {
        finding.code for finding in verify_images(tmp_path)
    }


def test_release_rejects_local_config_digest_as_oci_manifest_evidence(
    tmp_path: Path,
) -> None:
    _mutated_release_workflow(
        tmp_path,
        before="      - name: Generate the unpublished runtime-image CycloneDX SBOM\n",
        after=(
            "      - name: Record the local config digest\n"
            "        id: image\n"
            "        run: docker image inspect --format '{{.Id}}' \"$IMAGE_NAME\"\n"
            "      - name: Generate the complete runtime-image CycloneDX SBOM\n"
        ),
    )

    assert "release_workflow_not_exact" in {finding.code for finding in verify_workflows(tmp_path)}


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

    for spec_version in ("1.5", "1.6"):
        payload["specVersion"] = spec_version
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
        )
    for unsupported_version in ("1.7", []):
        payload["specVersion"] = unsupported_version
        with pytest.raises(SupplyChainViolation) as unsupported:
            verify_cyclonedx_sbom(
                payload,
                root=ROOT,
                artifact_digest=DIGEST,
                source_revision=REVISION,
            )
        assert unsupported.value.findings[0].code == "sbom_schema_invalid"

    payload["specVersion"] = "1.6"
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


def test_runtime_sbom_alpine_inventory_is_frozen_from_the_runtime_image() -> None:
    assert (
        frozenset(
            {
                ("alpine-baselayout", "3.7.2-r1"),
                ("alpine-baselayout-data", "3.7.2-r1"),
                ("alpine-keys", "2.6-r0"),
                ("alpine-release", "3.24.1-r0"),
                ("apk-tools", "3.0.6-r0"),
                ("busybox", "1.37.0-r31"),
                ("busybox-binsh", "1.37.0-r31"),
                ("ca-certificates", "20260611-r0"),
                ("ca-certificates-bundle", "20260611-r0"),
                ("gdbm", "1.26-r0"),
                ("libapk", "3.0.6-r0"),
                ("libbz2", "1.0.8-r6"),
                ("libcrypto3", "3.5.7-r0"),
                ("libffi", "3.5.2-r1"),
                ("libncursesw", "6.6_p20260516-r0"),
                ("libpanelw", "6.6_p20260516-r0"),
                ("libpq", "18.4-r0"),
                ("libssl3", "3.5.7-r0"),
                ("libuuid", "2.42-r0"),
                ("lz4-libs", "1.10.0-r1"),
                ("musl", "1.2.6-r2"),
                ("musl-utils", "1.2.6-r2"),
                ("ncurses-terminfo-base", "6.6_p20260516-r0"),
                ("postgresql-common", "1.3-r0"),
                ("postgresql16-client", "16.14-r0"),
                ("readline", "8.3.3-r1"),
                ("scanelf", "1.3.9-r1"),
                ("sqlite-libs", "3.53.2-r0"),
                ("ssl_client", "1.37.0-r31"),
                ("tzdata", "2026b-r0"),
                ("xz-libs", "5.8.3-r0"),
                ("zlib", "1.3.2-r0"),
                ("zstd-libs", "1.5.7-r2"),
            }
        )
        == RUNTIME_ALPINE_COMPONENTS
    )
    assert RUNTIME_ALPINE_PLATFORM_VIRTUAL_COMPONENT == {
        "aarch64": (".python-rundeps", "20260616.002547"),
        "x86_64": (".python-rundeps", "20260616.002554"),
    }
    assert (
        frozenset(RUNTIME_ALPINE_PLATFORM_VIRTUAL_COMPONENT.values())
        == RUNTIME_ALPINE_NOARCH_COMPONENTS
    )
    assert (
        frozenset(
            {
                ("libpq", "18.4-r0"),
                ("lz4-libs", "1.10.0-r1"),
                ("postgresql-common", "1.3-r0"),
                ("postgresql16-client", "16.14-r0"),
                ("zstd-libs", "1.5.7-r2"),
            }
        )
        == POSTGRES_CLIENT_ALPINE_COMPONENTS
    )


@pytest.mark.parametrize(
    ("architecture", "docker_architecture"),
    [("aarch64", "arm64"), ("x86_64", "amd64")],
)
def test_runtime_sbom_requires_exact_components_and_binds_reviewed_apks(
    architecture: str,
    docker_architecture: str,
) -> None:
    payload = _linux_runtime_sbom(architecture=architecture)

    verify_cyclonedx_sbom(
        payload,
        root=ROOT,
        artifact_digest=DIGEST,
        source_revision=REVISION,
        linux_runtime=True,
    )

    virtual_component = _sbom_component(payload, ".python-rundeps")
    assert (
        virtual_component["version"] == RUNTIME_ALPINE_PLATFORM_VIRTUAL_COMPONENT[architecture][1]
    )
    assert "arch=noarch" in str(virtual_component["purl"])

    names = {
        "libpq": "libpq",
        "lz4": "lz4-libs",
        "postgresql_common": "postgresql-common",
        "postgresql_client": "postgresql16-client",
        "zstd": "zstd-libs",
    }
    for variable, url, digest in POSTGRES_CLIENT_APK_MATRIX[docker_architecture]:
        properties = _sbom_component(payload, names[variable])["properties"]
        assert isinstance(properties, list)
        assert {"name": "schemabridge:apk-source-url", "value": url} in properties
        assert {
            "name": "schemabridge:apk-source-sha256",
            "value": digest,
        } in properties


@pytest.mark.parametrize(
    ("architecture", "other_architecture"),
    [("aarch64", "x86_64"), ("x86_64", "aarch64")],
)
def test_runtime_sbom_rejects_virtual_component_from_the_other_platform(
    architecture: str,
    other_architecture: str,
) -> None:
    payload = _linux_runtime_sbom(architecture=architecture)
    _replace_sbom_apk_identity(
        payload,
        ".python-rundeps",
        version=RUNTIME_ALPINE_PLATFORM_VIRTUAL_COMPONENT[other_architecture][1],
    )

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    assert "sbom_component_mismatch" in {finding.code for finding in error.value.findings}


def test_runtime_sbom_rejects_platform_virtual_component_with_non_noarch_purl() -> None:
    payload = _linux_runtime_sbom(architecture="x86_64")
    _replace_sbom_apk_identity(
        payload,
        ".python-rundeps",
        architecture="x86_64",
    )

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    assert "sbom_apk_architecture_invalid" in {finding.code for finding in error.value.findings}


def test_runtime_sbom_rejects_real_fetched_apk_version_drift() -> None:
    payload = _linux_runtime_sbom(architecture="x86_64")
    _replace_sbom_apk_identity(payload, "libpq", version="18.4-r1")

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    codes = {finding.code for finding in error.value.findings}
    assert "sbom_component_mismatch" in codes
    assert "sbom_apk_binding_mismatch" in codes


def test_runtime_sbom_rejects_truncated_apk_inventory() -> None:
    payload = _linux_runtime_sbom()
    components = payload["components"]
    assert isinstance(components, list)
    components.remove(_sbom_component(payload, "alpine-keys"))

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    assert "sbom_component_mismatch" in {finding.code for finding in error.value.findings}


def test_runtime_sbom_rejects_naked_component_without_package_purl() -> None:
    payload = _linux_runtime_sbom()
    component = _sbom_component(payload, "libpq")
    component.pop("purl")
    component.pop("bom-ref")

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    assert "sbom_component_mismatch" in {finding.code for finding in error.value.findings}


def test_runtime_sbom_rejects_extra_naked_library_component() -> None:
    payload = _linux_runtime_sbom()
    components = payload["components"]
    assert isinstance(components, list)
    components.append(
        {
            "type": "library",
            "name": "unreviewed",
            "version": "1.0.0",
        }
    )

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    assert "sbom_component_metadata_invalid" in {finding.code for finding in error.value.findings}


def test_runtime_sbom_rejects_apk_without_package_hash() -> None:
    payload = _linux_runtime_sbom()
    _sbom_component(payload, "musl").pop("hashes")

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    assert "sbom_component_hash_missing" in {finding.code for finding in error.value.findings}


def test_runtime_sbom_rejects_mixed_apk_architectures() -> None:
    payload = _linux_runtime_sbom()
    component = _sbom_component(payload, "musl")
    old_ref = component["purl"]
    assert isinstance(old_ref, str)
    new_ref = old_ref.replace("arch=aarch64", "arch=x86_64")
    component["purl"] = new_ref
    component["bom-ref"] = new_ref

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    assert "sbom_apk_architecture_invalid" in {finding.code for finding in error.value.findings}


def test_runtime_sbom_rejects_apk_not_reachable_from_root_graph() -> None:
    payload = _linux_runtime_sbom()
    component_ref = _sbom_component(payload, "libpq")["bom-ref"]
    metadata = payload["metadata"]
    dependencies = payload["dependencies"]
    assert isinstance(metadata, dict)
    assert isinstance(dependencies, list)
    root_ref = metadata["component"]["bom-ref"]
    root_dependency = next(
        item for item in dependencies if isinstance(item, dict) and item.get("ref") == root_ref
    )
    assert isinstance(root_dependency["dependsOn"], list)
    root_dependency["dependsOn"].remove(component_ref)

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    assert "sbom_dependency_graph_invalid" in {finding.code for finding in error.value.findings}


@pytest.mark.parametrize(
    "property_name",
    ["schemabridge:apk-source-url", "schemabridge:apk-source-sha256"],
)
def test_runtime_sbom_rejects_tampered_apk_source_binding(
    property_name: str,
) -> None:
    payload = _linux_runtime_sbom()
    properties = _sbom_component(payload, "postgresql16-client")["properties"]
    assert isinstance(properties, list)
    binding = next(
        item for item in properties if isinstance(item, dict) and item.get("name") == property_name
    )
    binding["value"] = "tampered"

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    assert "sbom_apk_binding_mismatch" in {finding.code for finding in error.value.findings}


def test_runtime_sbom_rejects_extra_python_component() -> None:
    payload = _linux_runtime_sbom()
    components = payload["components"]
    assert isinstance(components, list)
    purl = "pkg:pypi/unreviewed@1.0.0"
    components.append(
        {
            "type": "library",
            "name": "unreviewed",
            "version": "1.0.0",
            "purl": purl,
            "bom-ref": purl,
        }
    )

    with pytest.raises(SupplyChainViolation) as error:
        verify_cyclonedx_sbom(
            payload,
            root=ROOT,
            artifact_digest=DIGEST,
            source_revision=REVISION,
            linux_runtime=True,
        )

    assert "sbom_component_mismatch" in {finding.code for finding in error.value.findings}


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


def test_pip_audit_report_must_cover_runtime_and_build_inputs(tmp_path: Path) -> None:
    _write_runtime_requirements(tmp_path, ("runtime-example", "1.2.3"))
    requirement_template = (
        "{name}=={version} \\\n"
        "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    )
    requirements = tmp_path / "requirements"
    (requirements / "build.txt").write_text(
        requirement_template.format(name="build-example", version="2.0.0"),
        encoding="utf-8",
    )
    (requirements / "watchdog-build.txt").write_text(
        requirement_template.format(name="backend-example", version="3.0.0"),
        encoding="utf-8",
    )
    pip_report = tmp_path / "pip-audit.json"
    pip_report.write_text(
        json.dumps(_pip_audit_payload(("runtime-example", "1.2.3"))),
        encoding="utf-8",
    )
    trivy_report = tmp_path / "trivy.json"
    trivy_report.write_text(json.dumps(_trivy_payload()), encoding="utf-8")
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
