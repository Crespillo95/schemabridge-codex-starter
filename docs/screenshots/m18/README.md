# M18 release captures

These browser-generated captures use only the synthetic SchemaBridge demo and the pinned local
DataHub Core v1.6.0 stack. They were captured on 2026-08-05 after source release
`c5817af6d01b8a98cd7f1950d57e1be667614696` passed the complete clean-room gate.

The six JPEG DataHub captures show the ingested synthetic catalog, approved logical Customer model,
immutable semantic registry, approval decision, and SQL-free query recipe. The ten PNG Streamlit
captures were made through Safari WebDriver against the final `linux/amd64` release image and show
the overview, query, explicit interpretation, governed plan, SQL policy evidence, exact result,
source rejections, completed publication, decisions, and relationships. No source database was
modified.

## Inventory

- `m18-overview.png`
- `m18-query-studio.png`
- `m18-query-interpretation.png`
- `m18-governed-plan.png`
- `m18-sql-safety.png`
- `m18-validated-result.png`
- `m18-rejections.png`
- `m18-publication-completed.png`
- `m18-decisions.png`
- `m18-relationships.png`
- `m18-datahub-*.jpg` (six live local DataHub views)

Privacy and rights review: the images contain no token, password, API key, local filesystem path,
email address, employer/proprietary data, third-party stock media, or personal record. Visible IDs,
dates, physical assets, values, and fingerprints belong to the deterministic synthetic fixture.
The DataHub and SchemaBridge product interfaces are included only as direct evidence of the working
integration. No image has been retouched or had functional values edited.

The discarded pre-freeze Streamlit captures are not release evidence. Only the files listed above
belong to the final M18 release evidence set.
