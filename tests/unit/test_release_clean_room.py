"""Release clean-room orchestration preserves the governed DataHub prerequisite."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/release_clean_room.sh"


def test_release_clean_room_requires_explicit_datahub_confirmation_before_work() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    confirmation_check = source.index("SCHEMABRIDGE_RELEASE_DATAHUB_CONFIRMATION:-}")
    bootstrap = source.index("[1/9] Clean Python environment")

    assert confirmation_check < bootstrap
    assert "publish-approved-registry-version" in source


def test_release_clean_room_publishes_exact_prepared_registry_before_integration() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    writer = source.index("make datahub-provision-writer")
    prepare = source.index("registry-prepare --json")
    no_write_validation = source.index('payload.get("writes_performed") is False')
    fingerprint_validation = source.index('re.fullmatch(r"[0-9a-f]{64}", fingerprint)')
    publish = source.index("registry-publish", prepare)
    read_back = source.index("make datahub-registry-check")
    integration = source.index("make test-integration")

    assert writer < prepare < no_write_validation < fingerprint_validation < publish
    assert publish < read_back < integration
    assert '--fingerprint "$SCHEMABRIDGE_RELEASE_REGISTRY_FINGERPRINT"' in source
    assert '--confirm "$SCHEMABRIDGE_RELEASE_DATAHUB_CONFIRMATION"' in source
