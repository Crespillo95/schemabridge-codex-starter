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
