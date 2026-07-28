# M24 implementation prompt

Implement `plans/M24_AUTHENTICATED_API_WORKERS.md` as one vertical slice.

Preserve every repository invariant. In particular:

- accept only an exact already-reviewed workflow execution approval;
- verify bearer JWT cryptographically before mapping claims;
- never persist or log tokens, raw claims, secrets, SQL, parameters, prompts, or preview rows;
- use a dedicated API role and worker role;
- treat PostgreSQL job delivery as at least once with lease fencing;
- never blindly retry an ambiguous external effect;
- recompile, guard, and execute only bounded read-only SQL from typed current context;
- do not call an LLM for this milestone;
- do not start migrations from API or worker startup;
- run the real socket, PostgreSQL, worker, and internal-browser acceptance before closing.

Write tests alongside behavior. Do not weaken existing assertions or claim production/release
readiness. Record all commands and exact results in the M24 handoff.
