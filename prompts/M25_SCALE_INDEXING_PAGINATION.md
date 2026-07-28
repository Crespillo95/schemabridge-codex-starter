# M25 implementation prompt

Implement `plans/M25_SCALE_INDEXING_PAGINATION.md` as one vertical slice.

Preserve every repository invariant. In particular:

- make connection/table/field inventory tenant-scoped and dynamically sized;
- prove 10 and 5,434 tables without materializing the complete large fixture in Python;
- keep `PhysicalDatasetRef` and the three-table/two-join query limit unchanged;
- persist no DSN, token, credential, raw claim, source row, or sample value;
- use signed snapshot-bound keyset cursors, never interactive deep `OFFSET`;
- stage refresh generations invisibly and promote only exact complete state atomically;
- separate API, execution worker, catalog indexer, and migrator capabilities;
- make quota/rate admission transactional across replicas and worker claim tenant-fair;
- lifecycle-manage explicitly bounded PostgreSQL pools;
- use DataHub stable scroll pagination for deep discovery;
- do not call OpenAI or implement M27 semantic matching;
- label load thresholds as local regression budgets, not production SLOs;
- run real PostgreSQL/DataHub, process, load, package, and internal-browser acceptance before close.

Write tests alongside behavior. Do not weaken existing assertions, edit migrations 0001–0003, or
claim production/release readiness. Record all commands and exact results in the M25 handoff.
