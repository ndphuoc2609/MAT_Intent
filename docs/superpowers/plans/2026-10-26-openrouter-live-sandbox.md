# OpenRouter Live Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the scripted demo with a live OpenRouter Luna conversation sandbox, fix confirmed engine defects, and preserve the no-real-send boundary.

**Architecture:** Reuse `call_luna`, `validate_semantic`, `Engine.context/process`, Facts, and FakeSender. Add only a small stdlib HTTP app and browser UI; Python owns state, policy, facts, and simulated sending.

**Tech Stack:** Python stdlib HTTP server, existing SQLite engine, OpenRouter chat completions API, browser JavaScript/CSS.

**Spec:** `docs/superpowers/specs/2026-10-26-openrouter-live-sandbox-design.md`

## Global Constraints

- "Luna proposes semantic operations only. `validate_semantic`, `Engine`, policy, facts, response checks, and send permission remain authoritative."
- "The local app uses `FakeSender`; it must not call Facebook or create real appointments, leads, deposits, or customer contact."
- "No secret is committed. The adapter reads `OPENROUTER_API_KEY` from the process environment and sends it only as the OpenRouter Bearer credential."
- "Model failures, invalid semantics, unknown scope, or exhausted budget must not apply customer request operations or send a reply. Preserve the Engine's ambiguity handling: a new unresolved need may remain as a clarification placeholder, existing requests are restored unchanged, and explicit protective contact blocks may apply."
- The UI tells operators that post text, turns, and scoped conversation context are sent to OpenRouter; conversation state remains in memory.
- Bind to `127.0.0.1` by default; Docker publishes only `127.0.0.1:8766` on the host.
- Render customer/model content as text, never executable HTML.
- Use only Python stdlib/new existing dependencies; retain `openai/gpt-6-luna`.
- Preserve the existing `Engine.process(event, postcard, semantic)` contract and cap new live attempts at the known remaining budget.

## Review Focus

1. `scope=unknown` with a state-changing operation: rejected before state changes or send.
2. Primary slot plus duplicate/invalid `slot.extra`: cannot override identity and every accepted value is canonicalized.
3. Delete/edit webhook with missing version or comment IDs sharing prefixes: not incorrectly deduplicated or cross-matched.
4. Same actor across different Pages and different actors on one Page: no cross-context leakage in the interactive app.
5. Missing key, invalid model JSON, and budget rejection: no engine mutation and no outbound effect.

---

### Task 1: Engine and semantic regressions

**Files:**
- Modify: `app/engine.py`, `app/semantics.py`
- Test: `tests/unit/test_review_fixes.py` or a focused new `tests/unit/test_live_sandbox_regressions.py`

**Interfaces:**
- Keep `validate_semantic(semantic, text, context_texts=())` and `Engine.process(event, postcard, semantic)` unchanged.

- [x] Add failing tests for the five engine defects: unknown-scope state mutation, duplicate/invalid extra slot values, unversioned delete/edit dedupe, retracted context leakage, and exact comment-ID matching.
- [x] Run focused tests and verify they fail for the expected current behavior.
- [x] Make the smallest shared-code fixes.
- [x] Run focused tests and the existing unit suite.

### Task 2: OpenRouter credentials and guarded calls

**Files:**
- Modify: `app/adapters/luna.py`, `scripts/budget_ledger.py`, `.env.example`, `.dockerignore`
- Create: `.gitignore` (ignore `.env` and local runtime state)
- Test: `tests/unit/test_review_infra2.py`, `tests/unit/test_review_infra.py`

**Interfaces:**
- Preserve `call_luna(...)` parameters/results and `MODEL = "openai/gpt-6-luna"`.
- Preserve per-HTTP-attempt `budget_guard`; do not expose key values in errors/logs.
- Keep the 300-attempt / $2 session cap and account for the known prior usage of 276 attempts / $0.0854 when a caller supplies those prior values.

- [x] Add failing tests proving missing key fails before network, configured key becomes an Authorization Bearer header, prior budget usage is included in guard decisions, and negative/non-finite budget values fail closed.
- [x] Run the focused adapter test and observe the expected failure.
- [x] Use `OPENROUTER_API_KEY` from environment, parse response with stdlib JSON, account for optional prior usage in budget guard, treat malformed token counts as unknown cost, and update `.env.example` with an empty key only.
- [x] Run adapter tests and the full suite.

### Task 3: Interactive live-model sandbox

**Files:**
- Create: `sandbox/server.py`, `sandbox/static/index.html`, `sandbox/static/app.js`, `sandbox/static/style.css`
- Test: `tests/integration/test_sandbox_server.py`

**Interfaces:**
- Reuse `make_engine`, `Engine.context`, `validate_semantic`, `call_luna`, and `scripts.budget_ledger`.
- HTTP routes: `GET /health`, `GET /api/posts`, `POST /api/conversations`, `POST /api/conversations/<id>/turn`, `GET /api/conversations/<id>`.
- Conversation input accepts a seeded post ID or manual Page ID/post text plus an actor ID; each conversation owns one Page, actor, thread, Engine, and FakeSender.
- Turn input is free-form text; context given to Luna is limited to this conversation's post, recent turns, requests, and pending questions.

- [x] Add failing HTTP tests for seeded/manual post creation, turn/state isolation, missing-key/invalid-semantic fail-closed behavior, and one-repair maximum; keep ambiguity handling delegated to the Engine.
- [x] Run the focused tests and observe failure.
- [x] Implement a minimal stdlib server and usable responsive chat UI; display interpretation, requests/slots, pending question, route, response, and fake-send status.
- [x] Run focused tests and HTTP handler/static-asset smoke checks. A real listener launch was attempted but the environment denied loopback socket binding.

### Task 4: Remove old fixture demo and document live sandbox

**Files:**
- Delete: `demo/`
- Modify: `README.md`, `Dockerfile`, `compose.yaml`, `docker-compose.yml`

- [x] Search active references to `demo/` and update run/documentation links, Docker command, health check, and environment variable names.
- [x] Remove fixture-only UI and instructions after the replacement passed its focused tests.
- [x] Run full test suite and verify the new launch instructions and Compose configuration.

### Task 5: End-to-end verification

- [x] Run `python3 -m unittest discover -s tests`: 142 tests pass.
- [x] Verify health/posts/conversation handlers and static assets in-process; the sandbox denies loopback listener binding, so no browser-level HTTP session was possible.
- [x] Check OpenRouter availability: `OPENROUTER_API_KEY` is absent, so no live Luna call was made. Budget guards remain enabled.
- [x] Inspect active files and runtime paths: no Facebook send path; Compose mounts `./runs` so container recreation retains the cumulative budget ledger.

**Execution notes:** No `.git` exists, so isolate tasks by files rather than worktrees. The user explicitly requested multiple agents; dispatch implementers sequentially per the subagent-driven-development workflow and review each result before moving on.
