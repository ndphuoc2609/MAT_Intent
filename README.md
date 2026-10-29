# Facebook Page conversation assistant

For the independent intent-engine API (no Meta dependency or outbound writes),
see [docs/intent-api.md](docs/intent-api.md).

The Compose stack contains two isolated services:

- `sandbox` on `127.0.0.1:8766`: the interactive demo with a read-only dealer directory and simulated sends.
- `meta-services` on `127.0.0.1:8767`: the Page Graph API connector, signed comment webhook, OpenRouter/Luna processing, persistent conversation state, and authenticated operator API.

To test a Luna conversation, open `http://127.0.0.1:8766/`, choose a persona and
vehicle, create the sample post, then enter comments. Configure `DB_Host`,
`DB_Port`, `DB_Username`, and `DB_Password` for read-only access to
`content_ai_fac.public.personas`. Posts are generated with OpenRouter using the
selected persona; posts and comments incur model usage. Replies remain inside the sandbox. Nothing
is published externally. The independent PostgreSQL intent API is documented
in [docs/intent-api.md](docs/intent-api.md).

`Nam Creta · kịch bản hư cấu` is a sandbox fixture, not approved production
persona data. It can answer only from its checked fixture facts and utterances;
unknown details remain unknown.

The sandbox reads `fact_db.sqlite3` as a read-only dealer directory. Compose
mounts this file read-only; local Python may set `SANDBOX_DEALER_DB` to another
SQLite file with `dealers(name, mailing_address)`. The directory has no
verification date, business hours, or coordinates, so results are identified as
an imported list rather than a verified nearest dealer. If a dealer cannot be
matched, the reply links to the official Hyundai dealer locator; automatic
online lookup is not configured. Other factual replies require real facts and
do not fall back to sample data.

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

The local sandbox leaves budget enforcement off by default so unknown-cost ledger entries do not block manual testing. Provider attempts and reported costs are still appended to the shared ledger, and the UI warns that spend is uncapped. Set `SANDBOX_ENFORCE_BUDGET=true` to restore the sandbox cap and fail-closed unknown-cost check. The production intent API keeps its separate required budget limits. Facts are empty until verified facts are connected, so price, stock, warranty, and other factual requests are handed to staff rather than answered from sample values.

## Verify

```bash
python3 -m unittest discover -s tests
node --check meta_services/static/admin.js
node --test tests/test_meta_admin.js
```

Graph API calls are not made by the offline tests. Meta app review, Page permissions, token validity, webhook delivery over public HTTPS, and live reply/post behavior still need a live check on the deployed host.
