from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "src" / "schemabridge" / "bootstrap.py"
AUTH_ADAPTER = ROOT / "src" / "schemabridge" / "adapters" / "identity" / "streamlit_auth.py"
STREAMLIT_APP = ROOT / "src" / "schemabridge" / "entrypoints" / "streamlit" / "app.py"
LEGACY_ENTRYPOINT_VALIDATOR = (
    ROOT / "src" / "schemabridge" / "entrypoints" / "streamlit" / "auth_config.py"
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_streamlit_auth_preflight_preserves_inward_dependency_direction() -> None:
    adapter_imports = _imports(AUTH_ADAPTER)
    bootstrap_imports = _imports(BOOTSTRAP)
    entrypoint_imports = _imports(STREAMLIT_APP)

    assert not LEGACY_ENTRYPOINT_VALIDATOR.exists()
    assert not any(
        module == "schemabridge.bootstrap" or module.startswith("schemabridge.entrypoints")
        for module in adapter_imports
    )
    assert not any(
        module.startswith("schemabridge.entrypoints.streamlit") for module in bootstrap_imports
    )
    assert "schemabridge.adapters.identity.streamlit_auth" not in entrypoint_imports
