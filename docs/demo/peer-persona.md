# Persona peer demo — 2026-10-29

Open http://127.0.0.1:8770/, select **Nam Creta · kịch bản hư cấu**, select Creta, then create a new conversation. New posts bind to the latest fixture release. Existing bindings remain immutable.

The fixture seeder self-approves only `fictional_fixture` content. The final fixture revision is 4: revision 3 was already seeded during local acceptance, so the final additions use a new immutable release rather than modifying the published release. Dealer and vehicle color remain unknown; no real person's facts or consent are asserted.

## Behavior

- Persona questions resolve against approved release facts and utterances.
- Unknown facts use approved conversational fallbacks with matching forms of address.
- Shallow purchase interest stays social. Lead questions follow interested_vehicle, purchase_timeline, finance_intent, budget, usage_need, current_vehicle.
- Lead values retain current-message evidence and source ID. Rejected fields become DECLINED. Questions stop after three uses for the same field in a scoped conversation.
- Open pending uses the existing TTL, ownership, scope and version checks. Answered requests close before the next request opens to avoid ambiguous topic references.
- Opt-out and handoff block further questions. Deposit messages keep internal HANDOFF_DRAFT_ONLY with persona wording in the draft.
- Lead drafts use purpose lead_draft and NEEDS_REVIEW; send_lead is not an automatic action.

## Hook decision

Reuse `persona_hooks` for questions. The repository converts approved, release-checksummed hooks into renderer candidates, so the existing utterance validator and dispatch checks validate them. No free text is appended in the engine. Hook IDs are selected deterministically and included in the rendered utterance IDs. The direct model hook_id interface remains rejected; it cannot bypass selection or validation. Scoped sent outbox records enforce the three-question limit.

## Changes in this task

app/engine.py, app/lead_flow.py, app/state.py, app/semantics.py, app/adapters/luna.py, app/persona_fixture.py, app/persona_store.py, app/pg_adapter.py, both semantic schema JSON files, migration 007 for SQLite and PostgreSQL, sandbox state/UI, tests/unit/test_peer_demo.py, test_policy.py, and the existing persona integration assertions.

## Verification and limits

- Unit: 397 passed. Existing ResourceWarnings remain.
- Network-free integration: 62 passed.
- JavaScript syntax and git diff whitespace check passed.
- SQLite additive migration tested twice against a database containing existing data.
- PostgreSQL migration is provided and schema version advanced to 7; PostgreSQL integration was not run for this change.
- Conversations and engine state in the sandbox remain in memory for the running session. The approved persona release is stored separately.
- The peer fixture path is for a local demo. Other product/factual request planners retain their existing templates and policy behavior; this is not a conversion of every sales/service planner into peer speech.

## Live acceptance

Six turns were run against the final container with the configured real model. Raw routing, response text, pending and lead profile are in [peer-live-results.json](peer-live-results.json). Dealer/color questions used peer fallbacks, shallow interest stayed social, Creta was recorded with evidence, tháng sau was recorded and advanced to finance, and deposit returned HANDOFF_DRAFT_ONLY with peer wording. The earlier live run exposed ambiguous topic:social references; the final run passed after closing completed requests.
