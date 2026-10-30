# Facebook Page conversation assistant

For the independent intent-engine API (no Meta dependency or outbound writes),
see [docs/intent-api.md](docs/intent-api.md).

The Compose stack contains two isolated services:

- `sandbox` on `127.0.0.1:8766`: the interactive demo with simulated sends.
- `meta-services` on `127.0.0.1:8767`: the Page Graph API connector, signed comment webhook, OpenRouter/Luna processing, persistent conversation state, and authenticated operator API.

To test a Luna conversation, open `http://127.0.0.1:8766/`, choose a persona,
generate its post, then enter comments. Configure `DB_Host`,
`DB_Port`, `DB_Username`, and `DB_Password` for read-only access to
`content_ai_fac.public.personas`. Posts are generated with OpenRouter using the
selected persona's bound release facts; there is no vehicle selector. The post's vehicle
comes from `vehicle.current.model`, and stays empty when no such fact exists.
Posts and comments incur model usage. Replies remain inside the sandbox. Nothing
is published externally. The independent PostgreSQL intent API is documented
in [docs/intent-api.md](docs/intent-api.md).

`Nam Creta · kịch bản hư cấu` is a sandbox fixture, not approved production
persona data. It can answer only from its checked fixture facts and utterances;
unknown details remain unknown.

For the persona simulation, select **Nam Creta · kịch bản hư cấu**,
then select Người 1 or Người 2 before each comment. Both people
share the post while their request state remains separate. Nam's post is generated
from his fictional fixture facts. Sandbox conversations do not load sample product prices.
The generated persona path self-sends only validated replies through `FakeSender`;
it does not create a human review queue. Missing prices are not quoted. The display
shows per-turn routing, intent confidence, supplied facts, generated proposals and
their statuses. Comments still use Luna and can incur provider charges. The fixture's
mileage has no source date and is shown as a past, approximate record.

The sandbox does not load local SQLite product or dealer data. Product facts are
returned only when a supported, current source is configured; the Neon persona
database does not currently contain product pricing or inventory.

The Meta service uses the existing engine with **no sample facts**. It sends a public comment reply only when the engine allows it. Missing or conflicting facts produce a handoff draft at `GET /api/reviews`; they are never answered from demo data.

## Configure and start

The local ignored `.env` contains the configured OpenRouter and Meta credentials. Do not commit it or copy it into an image. `.env.example` contains names only.

```bash
docker compose run --rm --no-deps sandbox python3 -m app.pg_adapter migrate
SANDBOX_PORT=8770 SANDBOX_MODEL_LED_ENABLED=true SANDBOX_ENFORCE_BUDGET=true \
  docker compose up --build -d --no-deps sandbox
```

The migration command applies the PostgreSQL schema to `DB_NEON` from `.env`;
the sandbox then stores persona snapshots, conversations, memories, and sends
in that database. Runtime services require PostgreSQL; SQLite is limited to
isolated tests. `SANDBOX_MODEL_LED_ENABLED=true` selects the bounded
model/tool loop. The sandbox uses FakeSender and does not send to Meta. The
model-led path enforces the shared budget ledger even if legacy sandbox budget
enforcement is disabled. Without the port override, the sandbox listens on 8766.

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

The service calls the existing `openai/gpt-6-luna` adapter for verified new comments, including while outbound writes are disabled. It sends the post text, comment, parent text, and recent Page/actor-scoped conversation state to OpenRouter. PostgreSQL stores application state; the separate model budget ledger lives in the ignored `runs/` directory.

Legacy sandbox calls leave budget enforcement off by default, so they are not suitable for live evaluation. Model-led calls reserve and log each provider attempt in the shared ledger. The production intent API requires its own configured call and cost limits and a server-side page allowlist before model-led processing can be enabled. Facts are empty until verified facts are connected, so price, stock, warranty, and other factual requests are handed to staff rather than answered from sample values.

## Verify

```bash
python3 -m unittest discover -s tests
node --check meta_services/static/admin.js
node --test tests/test_meta_admin.js
```

Graph API calls are not made by the offline tests. Meta app review, Page permissions, token validity, webhook delivery over public HTTPS, and live reply/post behavior still need a live check on the deployed host.

## Synthetic persona evaluation

The frozen set contains twelve synthetic conversations. It has no real transcripts
or completed human scores. The legacy variant uses one semantic Luna call per turn;
the generated variant uses analysis and reply calls, with retries counted in
`attempts`. Both variants use the same seeded Nam fixture snapshot and shared Engine.
Validated generated replies are sent through the in-memory `FakeSender`; the runner
does not approve drafts or send anything to Meta. `response` is populated only for
an actual simulated send; an unsent suggestion is kept separately as `candidate_text`.

Set `OPENROUTER_API_KEY`, then run both variants and create the blinded review pack:

```bash
python3 scripts/run_persona_conversations.py --variant legacy --output runs/persona-legacy.json
python3 scripts/run_persona_conversations.py --variant generated --output runs/persona-generated.json
python3 scripts/evaluate_persona_conversations.py export-ab runs/persona-legacy.json runs/persona-generated.json --output runs/persona-blind.json --key-output runs/persona-key.json --seed 42
```

The unblinding key is separate from the reviewer pack. Keep it private until two
independent reviewers finish scoring. To debug one or two cases, add `--case
syn-01-multi-intent` (repeat the option for more cases); partial runs cannot be
used to export the complete twelve-case blinded pack. Unknown provider cost stays
unknown in the metrics: the report shows a known-cost subtotal, unknown-cost
count, and a total only when every turn has a known cost. Provider failures retain
`response: null`; the evaluation summary counts missing responses and leaves total
attempts unknown if any adapter failure did not report its attempt count.


Persona generation in the sandbox uses two Luna phases: semantic analysis with requested
persona facts, then a grounded reply with claims and optional story-fact proposals. The
engine validates and self-sends passing replies through `FakeSender`, without human review.
New generated facts are labeled `generated`, stay outside published releases/checksums, and
become active only after a successful simulated send. If validation fails, the engine uses
an approved persona utterance when one applies or sends nothing. Public replies cannot ask
for private contact details. Consistency checks do not establish that a generated story is
true. Persona content is sent to OpenRouter; no Meta send is performed.

`META_OUTBOUND_ENABLED=false` is a separate Meta connector gate. Enabling sandbox
FakeSender auto-send never enables real Meta writes.

Optional `SANDBOX_JEV_SHADOW_ENABLED=true` displays independent per-intent Jev scores.
It adds a shadow provider call and does not affect engine permissions. Missing scores are shown
as unavailable; Luna confidence is a self-assessment, not calibrated accuracy.

For the frozen synthetic A/B runner, collection status and human-label workflow, see
[datasets/persona_conversations/README.md](datasets/persona_conversations/README.md).
