# Facebook Page conversation assistant

The Compose stack contains two isolated services:

- `sandbox` on `127.0.0.1:8766`: the existing interactive demo with sample facts and simulated sends.
- `meta-services` on `127.0.0.1:8767`: the Page Graph API connector, signed comment webhook, OpenRouter/Luna processing, persistent conversation state, and authenticated operator API.

The Meta service uses the existing engine with **no sample facts**. It sends a public comment reply only when the engine allows it. Missing or conflicting facts produce a handoff draft at `GET /api/reviews`; they are never answered from demo data.

## Configure and start

The local ignored `.env` contains the configured OpenRouter and Meta credentials. Do not commit it or copy it into an image. `.env.example` contains names only.

```bash
docker compose up --build -d
```

The service API listens only on loopback. Use `http://127.0.0.1:8767/docs` for Swagger and `http://127.0.0.1:8767/admin` for the operator screen. Enter `META_SERVICE_API_KEY` in the screen to load posts, comments, inbox events, and review drafts; the key stays in page memory and is cleared when the page is closed or the key is rejected. `http://127.0.0.1:8767/health` shows service configuration and outbound status. Read routes require `Authorization: Bearer $META_SERVICE_API_KEY`; manual reply and post routes also require a unique `Idempotency-Key` header.

`META_OUTBOUND_ENABLED=false` is the default. Webhook events are queued and processed through OpenRouter so you can inspect routing and reviews, but automatic replies, manual replies, and Page publishing stay disabled until you deliberately set it to `true` in `.env` and restart `meta-services`.

The operator screen disables public reply and publish actions while outbound is off, and asks for confirmation before sending either action. If a write returns an uncertain result, retry the same content from the same open page so it reuses the original idempotency key.

## Meta API

- `GET /api/posts?limit=25&after=...`
- `GET /api/posts/{post_id}/comments?limit=25&after=...`
- `GET /api/comments/{comment_id}`
- `GET /api/inbox?limit=50`
- `GET /api/reviews?limit=50`
- `POST /api/comments/{comment_id}/reply` with `{"message":"..."}`
- `POST /api/posts` with `{"message":"...","link":"https://..."}`
- Meta callback: `GET|POST /webhooks/meta`

For a manual write, reuse the same idempotency key after a timeout. A confirmed write returns its saved result; an uncertain write is not repeated. Use a new key only for a new, deliberate action.

To receive webhooks, configure the Meta Page `feed` subscription to a publicly reachable HTTPS callback ending in `/webhooks/meta`. The current Compose port is loopback-only, so webhook delivery requires an HTTPS reverse proxy or a deployed server. The Page token also needs the Meta permissions/tasks for reading Page posts/comments and managing Page engagement/posts.

## OpenRouter and privacy

The service calls the existing `openai/gpt-6-luna` adapter for verified new comments, including while outbound writes are disabled. It sends the post text, comment, parent text, and recent Page/actor-scoped conversation state to OpenRouter. SQLite state and the separate model budget ledger live in the ignored `runs/` directory.

The Meta service uses a separate budget ledger capped at 300 provider attempts and $2.00. Unknown model cost blocks further model calls. Facts are empty until verified facts are connected, so price, stock, warranty, and other factual requests are handed to staff rather than answered from sample values.

## Verify

```bash
python3 -m unittest discover -s tests
node --check meta_services/static/admin.js
node --test tests/test_meta_admin.js
```

Graph API calls are not made by the offline tests. Meta app review, Page permissions, token validity, webhook delivery over public HTTPS, and live reply/post behavior still need a live check on the deployed host.
