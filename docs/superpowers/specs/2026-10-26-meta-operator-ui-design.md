# Meta API docs and operator screen

## Intent

Make the existing `meta-services` endpoints easier to explore and use during Page comment testing, without changing how intent handling or delivery works.

## Approaches

- Swagger only: quickest for API developers, but awkward for reviewing conversations.
- Operator screen only: better for staff workflows, but leaves endpoint exploration undocumented.
- **Both (selected):** OpenAPI + Swagger UI for endpoint testing, plus a small local operator screen for posts, comments, inbox, and reviews.

## Scope

- Add `GET /openapi.json` and `GET /docs` for the existing authenticated API and webhook contract.
- Add `GET /admin` as a lightweight browser screen showing service health, posts/comments, inbox events, and review drafts.
- Let the operator enter the service API key in memory; do not persist it in browser storage or log it.
- Reuse existing routes for manual replies and Page posts. Show outbound status, require explicit confirmation, and keep write controls disabled while `META_OUTBOUND_ENABLED=false`.
- Keep the service bound to loopback and avoid adding a frontend framework or changing the conversation engine.
- Serve Swagger UI assets locally so the API key is not exposed to third-party page scripts.

## Constraints and risks

- The existing API supplies current Page data and review drafts; it does not expose a full reconstructed thread transcript. The screen will only show data the API already returns.
- Enabling outbound later makes confirmed write actions public Page actions; the UI must make that state visible.
- The operator key is a bearer credential. It stays in page memory and is sent only to the local service origin.

## Acceptance

- `/docs` renders the OpenAPI API and supports authorized read calls.
- `/admin` can list posts/comments/inbox/reviews and clearly indicates service and outbound state.
- Reply and publish actions cannot be submitted while outbound is disabled; no change enables outbound automatically.
