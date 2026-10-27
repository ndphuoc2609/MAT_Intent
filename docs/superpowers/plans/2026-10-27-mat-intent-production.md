# MAT_Intent Production Upgrade Implementation Plan

> **For agentic workers:** Execute task by task with TDD; retain the current branch state and do not enable production routing.

**Goal:** Make the existing intent engine safer and operationally measurable across inference, scoped state, validation, persistence, caching, and evaluation.

**Architecture:** Extend current `Engine`, `Store`, and adapter seams. Pydantic validates the application contract; versioned JSON Schema drives Luna structured output. PostgreSQL is an additional store backend, while policy and state transitions remain shared.

**Tech Stack:** Python stdlib, Pydantic v2, psycopg 3, PostgreSQL, unittest.

**Spec:** `docs/superpowers/specs/2026-10-27-mat-intent-production-design.md`

## Global Constraints

- Preserve `Engine.process(event, postcard, semantic)` compatibility.
- No production enablement, external channel integration, real model calls, or user-data deletion.
- Do not alter existing budget totals or limits; fail closed on unknown spend.
- Keep synthetic fixtures separate from human gold.
- Claim PostgreSQL verified only after tests against a disposable PostgreSQL server pass.

## Review Focus

- A slot whose word occurs only in an unrelated excerpt must not be committed.
- A negated/retracted slot value must not survive as the selected value.
- The same actor in a different post/thread must not update earlier requests or see their pending prompts.
- A different actor in the same thread must not confirm another actor's pending prompt.
- A result inferred before state changes must not commit against the new state.

### Task 1: Evidence-grounded semantic contract

**Files:** `app/semantics.py`, `app/normalize.py`, `config/schema_versions/semantic_v1.json`, new `config/schema_versions/semantic_v2.json`, `app/adapters/luna.py`, `tests/unit/test_production_regressions.py`.

**Produces:** strict v1/v2 validation, full contracts, slot evidence checks, and Luna JSON Schema request mode.

- [x] Add regression tests for false vehicle evidence, alias canonicalization, negation/change-of-mind, wrong types, unknown fields, and v2 labels.
- [x] Implement the validator and schema changes while retaining v1 fixtures.
- [x] Run the focused tests and full unittest suite.

### Task 2: Scoped context and safe commit

**Files:** `app/state.py`, `app/engine.py`, `meta_services/runtime.py`, `meta_services/webhook.py`, `sandbox/server.py`, `tests/unit/test_production_regressions.py`.

**Produces:** structured post/root/parent/current context with authors, scoped requests/pending, and stale-inference rejection.

- [x] Add regression tests for same actor across threads, two actors in one thread, and stale context tokens.
- [x] Scope SQLite/PostgreSQL request and pending access and update resolution callers.
- [x] Add structured context limits that drop lower-priority whole entries and preserve source IDs/authors.
- [x] Run focused and full unittest suites.

### Task 3: Cache and resume identity

**Files:** `scripts/cache_key.py`, `scripts/resume.py`, `scripts/run_multiturn_eval.py`, `tests/unit/test_review_infra.py`.

**Produces:** deterministic fingerprints for all slot values, outcomes, pending propositions/status/options, parent/context, schema/prompt/model, and policy inputs; unsafe remaps become misses.

- [x] Add cache-miss tests for location, color, datetime, desired outcome, and pending proposition.
- [x] Update logical mapping and cache call sites; failed ID remaps become misses.
- [x] Run focused and full unittest suites.

### Task 4: PostgreSQL store and migrations

**Files:** `app/pg_adapter.py`, `app/state.py`, `meta_services/runtime.py`, `meta_services/server.py`, `migrations/postgresql/`, `docs/postgresql.md`, `tests/postgres/`.

**Produces:** psycopg-backed store with versioned migrations, transaction-scoped dedupe, optimistic updates, and a disposable-DB integration command.

- [x] Add adapter tests for scope, duplicate event, optimistic versions, and idempotent migrations.
- [x] Implement the PostgreSQL store using the shared state interface and migrations.
- [x] Run integration tests against a disposable local PostgreSQL server.

### Task 5: Model adapters, telemetry, and spend control

**Files:** `app/adapters/jev.py`, `app/adapters/luna.py`, `meta_services/runtime.py`, `scripts/budget_ledger.py`, `config/policy.yaml`, `tests/unit/test_production_regressions.py`.

**Produces:** pinned Jev 1.14 Decisions API adapter, default-off shadow/fast-path flags, model-independent traces, usage-based/UNKNOWN cost, and atomic reservations for retries/repair.

- [x] Add offline tests for Jev request/response parsing, fallback, routing metadata, budget reservation, and unknown-cost behavior.
- [x] Integrate optional non-blocking shadow collection without changing the policy/state machine.
- [x] Keep fast path disabled absent explicit evaluation thresholds.
- [x] Run focused and full unittest suites without external model calls.

### Task 6: Evaluation and contract migration

**Files:** `scripts/make_labeling_pack.py`, `scripts/scoring_v3.py`, `datasets/human_gold/README.md`, `reports/contract-migration.md`, `tests/`.

**Produces:** thread/actor/time-safe annotation packs, requested metrics and risk slices, explicit analysis of 9 contract discrepancies, and offline regression/mutation/review-probe results.

- [x] Add tests for leakage-safe splits and refusal of non-human labels.
- [x] Add the human-gold scorer and keep generated annotation cases marked UNLABELED.
- [x] Re-run unit/integration, contract, mutation, and reconstructed review probes; document all nine contract changes.
- [x] Record unavailable human-gold/online Jev evaluation and actual test results below.

## Execution Record

- Final unittest discovery: 257 passed, 1 skipped (PostgreSQL-only suite is run separately); Python 3.11 emitted existing unclosed-file `ResourceWarning`s although the project requires Python 3.12+.
- Contract suite: 199/199; mutation suite: 17/17 detected; locally reconstructed review probes: 41/41 fixed. The original reviewer probe bundle was not present locally.
- PostgreSQL 16 disposable local integration: 4/4 passed. No shared or production database was changed.
- Human gold is still empty. No Luna or Jev network calls were made, so model comparison, live endpoint support, and online latency/cost remain unmeasured; incremental model cost for this work was $0.
