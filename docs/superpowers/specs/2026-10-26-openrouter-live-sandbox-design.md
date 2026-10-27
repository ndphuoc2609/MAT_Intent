# OpenRouter Live Conversation Sandbox

## Goal

Replace the fixture-driven `demo/` with a local, interactive conversation sandbox that sends free-form Vietnamese turns to OpenRouter `openai/gpt-6-luna`, then runs validated semantics through the existing state engine.

## User outcome

The operator can select a sample Page post or paste a real Page ID/post, type multiple natural-language customer turns, and inspect each model interpretation, remembered request/pending state, factual response, and routing decision. The engine must preserve separation by Page, thread, and actor. Sample business facts are clearly marked as samples.

## Authority and safety

- Luna proposes semantic operations only. `validate_semantic`, `Engine`, policy, facts, response checks, and send permission remain authoritative.
- The local app uses `FakeSender`; it must not call Facebook or create real appointments, leads, deposits, or customer contact.
- No secret is committed. The adapter reads `OPENROUTER_API_KEY` from the process environment and sends it only as the OpenRouter Bearer credential.
- The UI states that each turn, the post text, and the scoped recent context are sent to OpenRouter; sandbox conversation state stays in memory.
- The server binds to `127.0.0.1` by default. Docker exposes the app only on the host loopback interface.
- Model failures, invalid semantics, unknown scope, or exhausted budget must not apply customer request operations or send a reply. Preserve the Engine's existing ambiguity behavior: an unresolved new need may remain as an OPEN placeholder for a clarification question, while an existing request must be restored unchanged; an explicit protective contact block may still be applied.
- The current session has 24 model attempts remaining against its 300 attempt / $2 budget. A live smoke test must use the remaining budget guard and be skipped if no key or network is available.
- This work does not implement Meta login, Page webhooks, durable production tenancy, or real sending. Those require credentials and operational setup not present here.

## Core behaviors

1. Context for each model call comes from only the same Page and actor, plus a same-Page parent/root post.
2. Each user turn is parsed with the configured OpenRouter Luna model and validated against the existing schema/evidence rules before Engine processing.
3. Invalid semantic output may receive at most one repair call, with the same budget guard; unresolved invalid output is review-only.
4. The UI exposes the semantic proposal, active requests and slots, pending question/proposition, response, routing, and fake-send ledger; it lets the operator select a seeded post or enter Page ID and post text.
5. Users can start a separate Page/actor/thread conversation without seeing or changing another conversation's state.
6. Engine ingestion rejects out-of-scope/unknown operations, prevents extra slots from overriding the validated primary slot, canonicalizes extra slots, and handles edit/delete dedupe and retraction safely.

## Files and compatibility

- `app/adapters/luna.py`: replace only connector-specific credential loading with environment-key authentication; retain adapter result contract and retries.
- `app/engine.py`, `app/semantics.py`: fix shared validation, slot application, and event handling; existing callers retain `Engine.process(event, postcard, semantic)`.
- `scripts/budget_ledger.py`: use its existing append-only ledger and fail-closed accounting from the sandbox.
- `sandbox/server.py`, `sandbox/static/index.html`, `sandbox/static/app.js`, `sandbox/static/style.css`: new stdlib local UI and session handling; no extra runtime dependency.
- `tests/`: regression coverage for changed engine behavior and sandbox endpoints.
- `.env.example`, `README.md`: key setup, run command, sandbox boundary, and removal of fixture-demo instructions.
- Remove `demo/` after the replacement is runnable; remove or update every reference to the deleted UI.

## Acceptance checks

- Existing offline suite passes, plus focused regressions for each engine fix and sandbox isolation/failure paths.
- Without a key, health/UI work and model submission fails closed with a clear setup error; no state or fake send is created.
- With a key, at least one live multi-turn conversation can be exercised within remaining budget, showing model output and state changes. If network/key is unavailable, report the exact blocker and keep the sandbox in safe offline mode.
- An HTTP-level smoke check confirms the UI endpoints start and return correctly; `FakeSender` remains the only sender.

## Non-goals

Automatic Facebook ingestion/sending, production deployment, permanent database tenancy, and tuning automation thresholds. Actual comments can be entered manually for evaluation.
