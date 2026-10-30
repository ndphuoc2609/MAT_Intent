# Grounded persona generation implementation plan

Spec: ../specs/2026-10-30-grounded-persona-generation-design.md
Authorization: user requested full implementation, parallel Luna max agents, root review and repeated verification.

Constraints: retain all existing work; one Luna call for semantic+reply; default generated drafts; no real Meta sends; Jev shadow only; no invented scores, stories, consent or human evaluation labels. Public replies short; no public personal-contact collection. Preserve engine gates and dispatch/drain provenance checks.

Workspace ruling: use a new branch in the existing checkout because the working tree contains prerequisite uncommitted changes and local sandbox resources. Archived source before edits to /private/tmp/persona-before-20261030.tar.gz. No baseline changes committed or discarded.

- [x] Baseline: run full unittest suite, record current failures.
- [x] A: Add generated reply envelope/schema, single-call adapter, diagnostics; test old adapters remain compatible.
- [x] B: Implement fail-closed grounded validator/context filter and voice guide loading; tests for unsupported claims, question limits, customer/persona source separation.
- [x] C: Add release-scoped structured memories and voice examples with approval/privacy/consent; SQLite/Postgres migration; immutable snapshots, revocation and cooldown tests.
- [x] D: Integrate engine candidate planning, default draft review queue, reviewed send/edit/reject, outbox precheck/drain and successful-delivery memory tracking; semantic peer signals with evidence, old regex fallback only for legacy outputs.
- [x] E: Sandbox short public reply flow, operator draft UI, per-intent score/confidence with source and missing states; account for multi-actor and channel; explicit transparency.
- [x] F: Frozen evaluation fixtures/importer for real transcripts, blind A/B export and scoring workflow; do not fabricate human-approved samples or label synthetic cases real. Include 12 seed conversations and a 30–50 real-conversation collection target.
- [x] G: Root review integration and independent Luna max review. Fix verified findings, run full tests, PostgreSQL tests when available, contract and live Luna smoke, browser QA, rebuild sandbox and report measured limitations.

Interfaces: engine.process gains optional reply_candidate and generation_context, with legacy defaults. engine.persona_generation_context(event, postcard) provides filtered facts/memories/voice/messages context. Validator validate_generated_reply(candidate,snapshot,*,messages=(),allowed_memory_ids=(),allowed_question=None,revoked_ids=(),allow_fictional_fixture=False) returns status/text/fact_ids/memory_ids/reason/manifest. New app.persona_turn provides envelope schema + parse diagnostics. Generated replies never get added to approved utterance library implicitly.

Review focus: bad evidence from another actor; incorrect attribute with a permitted number; generated candidate under stop/complaint/identity query; source revocation after review but before delivery; delayed shadow result attached to wrong turn.
