# Inactive observability contracts

These files are retained as bounded design contracts, not as active monitoring.
They are excluded from the alert, SLO, and dashboard bundle because the deployed
SchemaBridge composition does not yet produce their complete metric inputs.

`siem-export.yaml` remains schema-validated as an inactive integration contract.
It must not be used for delivery or telemetry-loss paging until a lifecycle-owned
exporter is composed and operated against the target SIEM.
