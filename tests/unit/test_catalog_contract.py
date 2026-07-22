"""Offline catalog adapters share one application-facing contract."""

from pathlib import Path

import pytest
from tests.contract.catalog_read_contract import (
    NORTH_STAR_FIELDS,
    assert_catalog_read_contract,
)

from schemabridge.adapters.datahub.fake import CatalogRecord, FakeCatalogAdapter
from schemabridge.adapters.datahub.recorded import RecordedCatalogAdapter
from schemabridge.application.ports.catalog import (
    CatalogAsset,
    CatalogDocument,
    CatalogField,
    CatalogInvalidResponseError,
    EvidenceStatus,
    PageRequest,
)
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef

ROOT = Path(__file__).parents[2]


def _fake_catalog() -> FakeCatalogAdapter:
    records: list[CatalogRecord] = []
    for dataset_text, field_names in NORTH_STAR_FIELDS.items():
        dataset = PhysicalDatasetRef(dataset_text)
        table = dataset_text.rpartition(".")[2]
        fields = tuple(
            CatalogField(
                id=PhysicalFieldRef(f"{dataset_text}.{field_name}"),
                dataset=dataset,
                field_path=(field_name,),
                native_type="TEXT",
                description=f"Synthetic {field_name} metadata.",
            )
            for field_name in sorted(field_names | {"extra_a", "extra_b"})
        )
        records.append(
            CatalogRecord(
                asset=CatalogAsset(
                    dataset=dataset,
                    urn=(
                        "urn:li:dataset:(urn:li:dataPlatform:postgres,"
                        f"schemabridge.{dataset_text},PROD)"
                    ),
                    name=table,
                    platform="postgres",
                    description=f"Synthetic {table} metadata.",
                ),
                fields=fields,
            )
        )
    return FakeCatalogAdapter(tuple(records))


@pytest.mark.parametrize(
    "catalog",
    [
        pytest.param(_fake_catalog(), id="fake"),
        pytest.param(
            RecordedCatalogAdapter(ROOT / "demo/datahub/catalog_snapshot.json"),
            id="recorded",
        ),
    ],
)
def test_offline_catalog_adapters_satisfy_shared_contract(
    catalog: FakeCatalogAdapter,
) -> None:
    assert_catalog_read_contract(catalog)


def test_fake_document_lookup_distinguishes_present_and_missing() -> None:
    catalog = FakeCatalogAdapter(
        (),
        documents=(
            CatalogDocument(
                urn="urn:li:document:approved-customer-key",
                title="Approved customer key mapping",
            ),
        ),
        documents_available=True,
    )

    found = catalog.search_documents("customer key", PageRequest(size=2))
    missing = catalog.search_documents("account status", PageRequest(size=2))

    assert found.status is EvidenceStatus.PRESENT
    assert found.page.items[0].title == "Approved customer key mapping"
    assert missing.status is EvidenceStatus.MISSING
    assert missing.reason_code == "documents_not_recorded"


def test_recording_rejects_secret_shaped_keys(tmp_path: Path) -> None:
    fixture = tmp_path / "catalog.json"
    fixture.write_text(
        '{"fixture_kind":"sanitized_catalog_recording","token":"unsafe","assets":[]}',
        encoding="utf-8",
    )

    with pytest.raises(CatalogInvalidResponseError, match="forbidden"):
        RecordedCatalogAdapter(fixture)


def test_application_and_domain_have_no_datahub_or_mcp_imports() -> None:
    forbidden = ("from mcp", "import mcp", "from datahub", "import datahub")

    for package in (ROOT / "src/schemabridge/application", ROOT / "src/schemabridge/domain"):
        for source_file in package.rglob("*.py"):
            source = source_file.read_text(encoding="utf-8")
            assert not any(marker in source for marker in forbidden), source_file
