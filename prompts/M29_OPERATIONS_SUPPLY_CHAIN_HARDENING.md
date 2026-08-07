# M29 implementation prompt

Implement `plans/M29_OPERATIONS_SUPPLY_CHAIN_HARDENING.md` as one governed vertical milestone.

Preserve every repository and M28 invariant. In particular:

- never modify a source database;
- never put a secret, token, DSN, endpoint, private binding/path, SQL, parameter, prompt, source
  value, result row, raw plan, or claim in logs, metrics, traces, state, fixtures, screenshots,
  reports, or commits;
- never let managed/production composition fall back to owner-only files, global credentials,
  environment bearer tokens, mutable secret versions, default service accounts, mutable images,
  unpinned actions, or an unlocked dependency resolution;
- use exact workload/capability identities, verified TLS, default-deny networking, and explicit
  egress;
- keep telemetry labels closed and low-cardinality;
- keep backup/restore separate from active runtime and require a distinct fresh target;
- do not claim a provider, cluster, signature, restore, browser, test, or release proof that was
  not actually executed.

Write tests before or alongside behavior. Work phase by phase and do not mark any acceptance item
complete from static inspection when the plan requires an operated proof. Run focused gates, then
the complete M29 matrix, then the internal-browser desktop/mobile validation on final bytes.

Update the authoritative state and handoff with exact commands, results, limitations, and any
provider-only NO-GO item. M30 may start only after M29 is accepted.
