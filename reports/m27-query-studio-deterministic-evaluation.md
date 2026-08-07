# M27 deterministic Query Studio retrieval

- Status: **PASS**
- Matcher: `m27-deterministic-v8`
- Corpus SHA-256: `6bb72e6efd4801bd5042e15a29616d634118d62e5f8a83b2f19d4691b1502ece`
- Catalog SHA-256: `3057afebc4e67e674995db5b45df0ec1293bad422e687f5d30a71c3cf93de671`
- Registry fingerprint: `0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966`
- Scope: synthetic recorded governed retrieval only; no provider or source request was made.

| Metric | Result | Gate |
|---|---:|---:|
| Top-1 accuracy | 56/62 (0.903226) | >= 0.85 |
| Top-3 recall | 62/62 (1.000000) | 1.00 |
| Recall@20 | 62/62 (1.000000) | 1.00 |
| Mean reciprocal rank | 0.946237 | >= 0.90 |
| No-match specificity | 31/31 (1.000000) | 1.00 |
| Critical ambiguity recall | 6/6 (1.000000) | 1.00 |

## Boundedness and safety

- Governed mappings traversed: 31; page sizes: [1, 17, 50]; counts: [31, 31, 31].
- Stable keyset and replay order: True.
- Ungoverned executable results: 0.
- False positives: 0.
- False negatives at 20: 0.
- Ambiguity misses: 0.
- Provider calls/tokens/cost: 0 / 0 / EUR 0.00.

> Synthetic multilingual Query Studio labels. They contain no employer, customer, source-row, credential, or proprietary data and are not evidence of production accuracy.
