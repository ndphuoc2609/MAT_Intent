# Grounded persona generation progress

Branch: `codex/grounded-persona-20261030`. Existing dirty workspace state is preserved; no commit or reset was performed.

## Implemented

- Two strict Luna phases: `persona_analysis_v1` (semantic, needs, confidence and peer interpretation) and `persona_turn_v1` (reply, claims and optional fact proposals). Legacy semantic-only calls retain their contract.
- Persona for each turn comes from the post binding. Facts and generated-fact lifecycle are scoped to that persona; active facts are shared across its customers only.
- Fact selection uses exact requested keys when provided and opens a whole topic only when no exact key for it was requested. Persona-authored post fact IDs are included and contradictions are checked.
- Generated facts remain outside published release/checksum data, carry `generated`, can be revoked, expire while pending after 30 minutes, and activate only after FakeSender reports successful delivery.
- Sandbox auto-sends validated persona replies through `FakeSender`; the Meta outbound gate is independent and default-off. No human draft-review flow is exposed.
- Database personas get their own local release and do not inherit Nam’s utterances. Sandbox UI shows intent/confidence, supplied facts, proposal outcomes and reasons, generated status and revoke controls.
- Audit records persona ID, supplied fact IDs, proposals, validation result and rejection reason. Existing kill switch, opt-out, handoff, TTL, dedupe and dispatch prechecks remain active.

## Recent fixes

- Removed an obsolete sandbox path that retried `review_generated()` after dispatch failure.
- Kept only an applicable approved utterance fallback; persona without one sends nothing.
- Made generated-fact purge transactional and enabled revocation for any bound persona.
- Fixed authored-post fact-ID ordering, topic relevance checks, story proposal citation instructions and key-scoped fact supply.
- Added cleanup when post generation/audit/binding fails and tests for failed delivery, race, revocation, multiple personas, post consistency and wrong-topic replies.

## Final verification

- Unit: **607 tests OK**.
- Integration: **91 tests OK**.
- `node --check sandbox/static/app.js`, Python compilation of modified modules and `git diff --check`: pass.
- PostgreSQL test DSN was not configured; PostgreSQL migration was not run against a live test database. SQLite migrations are covered by tests.
- 12 synthetic conversation A/B, each 48 turns: legacy 13/48 sent, 48 attempts, p50 5.17s, p95 10.74s, $0.017774; two-phase 29/48 sent, 94 attempts, p50 16.45s, p95 23.10s, $0.060449. Provider cost was known for every turn; no HTTP retries. Run artifacts are in `/tmp/persona-legacy-current.json` and `/tmp/persona-generated-current.json`.
- Six live Luna smoke turns across Nam fixture and synthetic DB persona Linh: 6/6 sent, 12 attempts, p50 12.75s, p95 15.50s, $0.008224. Transcript: `/tmp/persona-live-six-conversations-final.json`.
- Human blind scoring was not run; the datasets are synthetic. Internal consistency checks do not establish generated stories are true. No Meta sends were performed.

See `docs/demo/grounded-generation-implementation-2026-10-30.md` for measured transcripts, detailed routing and limitations.

## Follow-up: post generation and database description

Imported category, role, perspective and sample_quote into the bound release context for posts and replies, with revision detection. Empty post needs now receives allowed facts; post prompt clarifies citation predicates and the structured-output repair is bounded to one attempt. Current verification: 608 unit and 92 integration tests pass; two live synthetic persona post checks pass, and restarted sandbox API returns HTTP 201 with validation OK. The earlier A/B measurements have not been repeated for this follow-up.
