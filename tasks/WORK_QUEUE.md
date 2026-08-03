# Work queue

| Order | ID | Status | Dependency note |
|---:|---|---|---|
| 1 | M00 | ready | no dependencies |
| 2 | M01 | blocked | requires accepted M00 |
| 3 | M02 | blocked | requires M00/M01 |
| 4 | M03 | blocked | requires M02 |
| 5 | M04 | blocked | requires M00/M01 |
| 6 | M05 | blocked | requires M02/M04 |
| 7 | M06 | blocked | requires M02/M05 |
| 8 | M07 | blocked | requires M04/M05/M06 |
| 9 | M08 | blocked | requires M05/M06/M07 |
| 10 | M09 | blocked | requires M02/M07/M08 |
| 11 | M10 | blocked | requires M03/M07/M08/M09 |
| 12 | M11 | blocked | requires M09/M10 |
| 13 | M12 | blocked | requires M06–M11 |
| 14 | M13 | blocked | requires M07/M08/M10/M12 |
| 15 | M14 | blocked | requires M06–M13 |
| 16 | M15 | blocked | requires M14 and core evaluation inputs |
| 17 | M16 | blocked | requires M00–M15 |
| 18 | M17 | blocked | requires accepted M16 |
| 19 | M18 | blocked | requires M15–M17 |
| 20 | M19 | optional | only after accepted M18 |

## Productionization continuation

This track follows the locally verified development baseline without accepting the blocked public
M17/M18 release evidence.

| Order | ID | Status | Dependency note |
|---:|---|---|---|
| 21 | M20 | locally complete; accepted as productionization baseline | identity/RBAC/workflow isolation evidence recorded |
| 22 | M21 | complete; accepted as productionization baseline | atomic registry and diverse corpus gates pass |
| 23 | M22 | complete; accepted | live DataHub registry and browser gates pass |
| 24 | M23 | complete; accepted as productionization baseline | durable activation, migrations, rollback, reconciliation, recovery, and browser gates pass |
| 25 | M24 | complete; accepted locally | authenticated API, queues/workers, leases, cancellation, and idempotency |
| 26 | M25 | complete; accepted locally | dynamic inventory, scale, full gate, and browser evidence recorded |
| 27 | M26 | complete; accepted locally | all 30 criteria and final technical/browser gates recorded |
| 28 | M27 | complete; accepted locally | dynamic governed matching and final gates recorded |
| 29 | M28 | complete; accepted locally | governed connector routing, cost controls, full gate, and browser evidence recorded |
| 30 | M29 | complete; accepted locally | operations/supply-chain local gates pass; external operated evidence remains open |
| 31 | M32 | complete; accepted locally | bounded copy-first PostgreSQL capability; production gates remain open |
| 32 | M33 | complete; accepted locally | generic tenant onboarding to an immutable ready-for-publication proposal |
| 33 | M34 | complete; accepted locally | isolated writer/readback and activation-ready bridge pass local gates |
| 34 | M35 | complete; accepted locally | bounded registry-v2 join and one-model replacement/remediation lifecycle |
| 35 | M30 | Phase 0 + Phase 1a + schema-v2 qsp3 target binding implemented locally; final verification pending; campaign blocked | Phase-1b draft held back after P1 review; final `make check`, managed browser, protected exact candidate, 24 operated/independent/owner controls and corpus execution are absent |
| 36 | M31 | blocked | requires accepted M30; M31 produces the operated pilot evidence |
