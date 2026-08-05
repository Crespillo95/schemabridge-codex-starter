from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from schemabridge import bootstrap
from schemabridge.adapters.connectors.remote_secrets import (
    ConnectorSecretResolutionError,
)
from schemabridge.adapters.identity.streamlit_secrets import (
    ProjectedStreamlitSecrets,
    StreamlitSecretsFileError,
)
from schemabridge.bootstrap import build_web_process_runtime
from schemabridge.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def _base64url(value: dict[str, object]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _jwt(*, audience: str = "schemabridge-secret-manager") -> str:
    now = int(time.time())
    return ".".join(
        (
            _base64url({"alg": "RS256", "typ": "JWT"}),
            _base64url(
                {
                    "aud": audience,
                    "exp": now + 600,
                    "iat": now,
                    "iss": "https://kubernetes.default.svc.cluster.local",
                    "nbf": now - 1,
                    "sub": "system:serviceaccount:schemabridge-system:schemabridge-web",
                }
            ),
            _base64url({"synthetic": "signature"}),
        )
    )


def _projected_file(
    root: Path,
    *,
    name: str,
    content: str,
    mode: int,
) -> tuple[Path, Path]:
    root.mkdir()
    version = root / "..2026_07_30"
    version.mkdir()
    target = version / name
    target.write_text(content, encoding="utf-8")
    target.chmod(mode)
    (root / "..data").symlink_to(version.name)
    projected = root / name
    projected.symlink_to(f"..data/{name}")
    return projected, target


def _auth_toml(*, client_id: str = "schemabridge-web") -> str:
    return f"""
[auth]
redirect_uri = "https://app.example.test/oauth2callback"
cookie_secret = "6KzTEe9Wt7JfM2xP8vAc4nQ1sRg5yUhB" # gitleaks:allow — synthetic fixture

[auth.corporate]
client_id = "{client_id}"
client_secret = "G7nYw4rQ9tVz6mXp2sKa8cHd" # gitleaks:allow — synthetic fixture
server_metadata_url = "https://identity.example.test/.well-known/openid-configuration"
"""


def _managed_settings(identity_root: Path, token_file: Path) -> Settings:
    return Settings(_env_file=None).model_copy(
        update={
            "environment": "production",
            "runtime_component": "web",
            "auth_mode": "oidc",
            "catalog_kind": "live",
            "registry_kind": "live",
            "publication_kind": "disabled",
            "judge_execution_kind": "disabled",
            "oidc_provider": "corporate",
            "oidc_audience": "schemabridge-web",
            "oidc_issuer": "https://identity.example.test",
            "workload_identity_token_file": token_file,
            "workload_identity_root": identity_root,
            "workload_identity_audience": "schemabridge-secret-manager",
        }
    )


def _managed_projection(
    tmp_path: Path,
    *,
    auth_toml: str | None = None,
    token: str | None = None,
) -> tuple[Path, Settings]:
    auth_root = (tmp_path / "streamlit-auth").resolve()
    _projected_file(
        auth_root,
        name="secrets.toml",
        content=auth_toml or _auth_toml(),
        mode=0o440,
    )
    identity_root = (tmp_path / "identity").resolve()
    token_file, _target = _projected_file(
        identity_root,
        name="token",
        content=token or _jwt(),
        mode=0o400,
    )
    return auth_root, _managed_settings(identity_root, token_file)


def _compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth_toml: str | None = None,
    token: str | None = None,
) -> tuple[Any, list[dict[str, object]]]:
    auth_root, settings = _managed_projection(
        tmp_path,
        auth_toml=auth_toml,
        token=token,
    )
    schema_checks: list[dict[str, object]] = []
    monkeypatch.setattr(bootstrap, "_MANAGED_STREAMLIT_SECRETS_ROOT", auth_root)
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        bootstrap,
        "require_current_control_plane_schema",
        lambda **kwargs: schema_checks.append(kwargs),
    )
    runtime = build_web_process_runtime(repository_root=ROOT, settings=settings)
    return runtime, schema_checks


def test_managed_web_readiness_repeats_oidc_identity_and_exact_schema_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, schema_checks = _compose(tmp_path, monkeypatch)

    runtime.require_ready()
    runtime.require_ready()

    assert len(schema_checks) == 2
    assert all(item["credential_kind"] == "runtime" for item in schema_checks)
    assert all(item["repository_root"] == ROOT for item in schema_checks)
    assert runtime.streamlit_argv[:3] == ("streamlit", "run", "streamlit_app.py")
    assert "--client.toolbarMode=minimal" in runtime.streamlit_argv
    assert "--client.showErrorDetails=none" in runtime.streamlit_argv
    assert "--client.showErrorLinks=false" in runtime.streamlit_argv
    assert "secret" not in repr(runtime).casefold()


def test_managed_web_rejects_oidc_projection_before_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_marker = "private-client-value-that-must-not-be-echoed"
    runtime, schema_checks = _compose(
        tmp_path,
        monkeypatch,
        auth_toml=_auth_toml(client_id=private_marker),
    )

    with pytest.raises(RuntimeError) as failure:
        runtime.require_ready()

    assert schema_checks == []
    assert private_marker not in str(failure.value)


def test_managed_web_rejects_invalid_workload_identity_before_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, schema_checks = _compose(
        tmp_path,
        monkeypatch,
        token=_jwt(audience="unapproved-audience"),
    )

    with pytest.raises(ConnectorSecretResolutionError) as failure:
        runtime.require_ready()

    assert schema_checks == []
    assert "unapproved-audience" not in str(failure.value)


def test_local_web_requires_no_managed_projection_or_control_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "require_current_control_plane_schema",
        lambda **_kwargs: pytest.fail("local readiness must not inspect PostgreSQL"),
    )

    runtime = build_web_process_runtime(settings=Settings(_env_file=None))

    runtime.require_ready()
    assert runtime.streamlit_argv[0] == "streamlit"


def test_projected_streamlit_reader_accepts_kubernetes_symlinks_and_rejects_writes(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "auth").resolve()
    projected, target = _projected_file(
        root,
        name="secrets.toml",
        content=_auth_toml(),
        mode=0o440,
    )
    reader = ProjectedStreamlitSecrets(
        secrets_file=projected,
        mount_root=root,
    )

    assert "auth" in reader.read()
    target.chmod(0o660)
    with pytest.raises(StreamlitSecretsFileError) as failure:
        reader.read()
    assert str(failure.value) == "Streamlit authentication configuration is unavailable"
    assert str(root) not in str(failure.value)
