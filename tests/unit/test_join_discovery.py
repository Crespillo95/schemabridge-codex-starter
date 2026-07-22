"""Application-level catalog context and aggregate-evidence composition."""

from dataclasses import dataclass

from schemabridge.adapters.datahub.fake import CatalogRecord, FakeCatalogAdapter
from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.application.join_discovery import DiscoverJoinCandidates
from schemabridge.application.ports.catalog import CatalogAsset, CatalogField
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef
from schemabridge.domain.joins import (
    DeclaredRelationship,
    JoinProposal,
    RelationshipProfile,
)


@dataclass(frozen=True)
class FakeRelationshipEvidence:
    profiles: dict[str, RelationshipProfile]

    def profile(self, proposal: JoinProposal) -> RelationshipProfile:
        return self.profiles[proposal.id]


def test_discovery_composes_catalog_descriptions_and_explicit_missing_context() -> None:
    proposals = build_north_star_join_proposals()
    catalog = FakeCatalogAdapter(
        (
            _record(
                "crm.customers",
                "customer_id",
                "Customer identifier encoded as an 11-character numeric string.",
            ),
            _record(
                "bank.account_holders",
                "gf_customer_id",
                "Customer identifier stored as double precision.",
                extra_field=("account_number", "Account identifier on the holder relationship."),
            ),
            _record(
                "bank.accounts",
                "account_number",
                "Unique account identifier.",
            ),
        )
    )
    evidence = FakeRelationshipEvidence(
        {
            proposals[0].id: RelationshipProfile(
                left_row_count=7,
                right_row_count=9,
                left_null_count=0,
                right_null_count=1,
                left_invalid_count=0,
                right_invalid_count=2,
                left_distinct_valid=7,
                right_distinct_valid=5,
                matching_distinct_keys=5,
                left_max_multiplicity=1,
                right_max_multiplicity=2,
            ),
            proposals[1].id: RelationshipProfile(
                left_row_count=9,
                right_row_count=9,
                left_null_count=0,
                right_null_count=0,
                left_invalid_count=0,
                right_invalid_count=0,
                left_distinct_valid=9,
                right_distinct_valid=9,
                matching_distinct_keys=9,
                left_max_multiplicity=1,
                right_max_multiplicity=1,
                declared_relationship=DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT,
            ),
        }
    )

    report = DiscoverJoinCandidates(catalog, evidence).execute(proposals)

    assert report.catalog_source == "fake"
    assert [item.proposal.id for item in report.candidates] == [
        "customer_to_account_holder",
        "account_holder_to_account",
    ]
    assert all(
        "lineage_not_recorded" in " ".join(item.missing_evidence) for item in report.candidates
    )
    assert all(
        "query_context_not_recorded" in " ".join(item.missing_evidence)
        for item in report.candidates
    )


def _record(
    dataset_name: str,
    field_name: str,
    description: str,
    *,
    extra_field: tuple[str, str] | None = None,
) -> CatalogRecord:
    dataset = PhysicalDatasetRef(dataset_name)
    fields = [
        CatalogField(
            id=PhysicalFieldRef(f"{dataset_name}.{field_name}"),
            dataset=dataset,
            field_path=(field_name,),
            native_type="TEXT",
            description=description,
        )
    ]
    if extra_field is not None:
        name, extra_description = extra_field
        fields.append(
            CatalogField(
                id=PhysicalFieldRef(f"{dataset_name}.{name}"),
                dataset=dataset,
                field_path=(name,),
                native_type="TEXT",
                description=extra_description,
            )
        )
    return CatalogRecord(
        asset=CatalogAsset(
            dataset=dataset,
            urn=f"urn:li:dataset:{dataset_name}",
            name=dataset_name.rsplit(".", 1)[1],
            platform="postgres",
        ),
        fields=tuple(fields),
    )
