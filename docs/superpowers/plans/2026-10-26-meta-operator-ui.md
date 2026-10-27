# Meta API Docs and Operator Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (native) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local Swagger docs and an authenticated operator screen for the existing Meta service.

**Architecture:** Extend the current standard-library HTTP server with an OpenAPI document, a local Swagger UI page, and a small same-origin operator page. The operator page uses existing read/write routes and never stores the bearer key beyond page memory.

**Tech Stack:** Python standard library, OpenAPI 3.1, Swagger UI 5.33.1 static assets, vanilla HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-10-26-meta-operator-ui-design.md`

## Global Constraints

- Keep the service bound to loopback.
- Keep the API key in page memory only; do not persist it in browser storage or logs.
- Reuse existing routes for manual replies and Page posts.
- Require explicit confirmation for writes and keep write controls disabled while `META_OUTBOUND_ENABLED=false`.
- Serve Swagger UI assets locally so the API key is not exposed to third-party page scripts.
- Do not change the conversation engine or add a frontend framework.

## Review Focus

- Customer text containing HTML or script-like content must render as text, never execute; verify the rendering path uses `textContent` and no `innerHTML`.
- Missing or rejected operator keys must show an auth error without persisting or displaying the key; verify the notice is outside the hidden dashboard and protected API calls reject missing bearer auth.
- `outbound_enabled=false` must disable UI write actions and the server must return 423 without a Graph write; assert with `FakeGraph`.
- An uncertain write retry must reuse its idempotency key; verify with a Node VM regression test that simulates a lost response, a 423 retry, and success.
- Empty lists, pagination cursors, and Vietnamese text must render without crashing or corrupting content; inspect these paths through browser smoke when browser automation is available.

---

### Task 1: OpenAPI contract and local Swagger UI

**Files:**
- Create: `meta_services/openapi.py`
- Create: `meta_services/static/docs.html`
- Create: `meta_services/static/swagger-initializer.js`
- Vendor: `meta_services/static/swagger-ui-bundle.js`, `meta_services/static/swagger-ui.css`, and Swagger UI `LICENSE` from the official v5.33.1 release
- Modify: `meta_services/server.py`
- Test: `tests/integration/test_meta_service_http.py`

**Interfaces:**
- `meta_services.openapi.OPENAPI_DOCUMENT`: OpenAPI 3.1 document for `/health`, `/webhooks/meta`, and every existing `/api/*` route.
- `GET /openapi.json`: returns the document as JSON.
- `GET /docs`: serves local Swagger UI configured to load `/openapi.json` with `persistAuthorization` disabled.
- Static asset routes use a fixed filename allowlist; no user-controlled filesystem path is opened.

- [x] Add integration assertions for the OpenAPI version, required routes, bearer security on `/api/*`, and public health/webhook routes; assert `/docs` and local assets have the expected content types.
- [x] Implement the OpenAPI document from the current server request/response shapes, including 401, 423, idempotency headers, webhook signature header, and pagination parameters.
- [x] Serve only `/docs`, `/openapi.json`, and explicitly mapped static assets before the protected `/api/*` route check.
- [x] Run `python3 -m unittest tests.integration.test_meta_service_http -v`; expect all Meta HTTP integration tests to pass.

### Task 2: Operator screen

**Files:**
- Create: `meta_services/static/admin.html`
- Create: `meta_services/static/admin.css`
- Create: `meta_services/static/admin.js`
- Modify: `meta_services/server.py`
- Test: `tests/integration/test_meta_service_http.py`, `tests/test_meta_admin.js`

**Interfaces:**
- `GET /admin`: serves the local operator page.
- The page reads `/health`, `/api/posts`, `/api/posts/{post_id}/comments`, `/api/inbox`, and `/api/reviews` using the entered bearer key.
- Manual reply uses `POST /api/comments/{comment_id}/reply`; publishing uses `POST /api/posts`; both reuse the existing JSON bodies, bearer authorization, and `Idempotency-Key` contract.

- [x] Add integration assertions that `/admin` and its assets are served, while API data routes still reject missing bearer auth.
- [x] Build the screen with sections for service status, posts/comments, inbox, and reviews; use `textContent` for all Page-sourced text and keep the key only in a JavaScript variable.
- [x] Disable reply/publish controls while health reports outbound disabled. If enabled later, require confirmation and a unique safe idempotency key per new action; reuse that same key for a retry whose result is unknown.
- [x] Add a Node VM regression test confirming the same idempotency key survives an unknown result and a 423 retry, then clears after success.
- [x] Add a disabled-outbound integration case asserting reply and publish return 423 and `FakeGraph` records no writes.
- [x] Run the targeted Meta HTTP integration tests; expect all routes, auth, and outbound-gate assertions to pass.

### Task 3: Operator documentation and container smoke check

**Files:**
- Modify: `README.md`
- Verify: `compose.yaml`, `Dockerfile`, and routes added in Tasks 1-2

- [x] Document `http://127.0.0.1:8767/docs`, `http://127.0.0.1:8767/admin`, bearer-key entry, and the outbound-disabled behavior.
- [x] Run `python3 -m unittest discover -s tests`; expect the full suite to finish with `OK`, including the new HTTP cases.
- [x] Run `node --check meta_services/static/admin.js`, `node --test tests/test_meta_admin.js`, and `docker compose build meta-services`; expect all commands to exit 0.
- [x] Recreate `meta-services`, confirm `/health` is healthy, and HTTP-check `/docs`, `/admin`, and their local assets. Keep outbound disabled; browser visual smoke is unavailable in this environment.

## Execution notes

- Swagger UI assets are pinned to the official `swagger-api/swagger-ui` v5.33.1 release: <https://github.com/swagger-api/swagger-ui/releases/tag/v5.33.1>.
- The existing service does not expose a full reconstructed thread transcript. The operator page must show the inbox/review data the current API provides, not invent missing history.
- No commit is planned because the workspace currently contains untracked project files.
