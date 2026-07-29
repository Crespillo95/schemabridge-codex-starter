# Queue backlog and unsafe transitions

## Detection

Identify the closed queue name, depth, oldest age, and stable transition category. Correlate only
public job fingerprints already allowed by the job contract; never retrieve a raw request or plan.
Missing queue or reconciliation-age series are incidents and block production GO.

## Containment

Pause new admission for the affected capability when bounded capacity is exhausted. Do not bypass
leases, fencing, stale authorization, retry limits, or dead-letter transitions.

## Recovery

Restore the required worker identity and dependency, reclaim only expired leases through the
governed store, and let normal bounded claims drain the queue. Reconcile dead letters explicitly.

## Evidence

Record queue category, safe counts, UTC timeline, worker revision, recovery decision, and the
complete healthy-age window. Do not copy job payloads into incident systems.
