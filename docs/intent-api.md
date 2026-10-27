# Standalone intent API

`app.intent_api` serves an authenticated intent endpoint without importing or
calling Meta/Facebook services. It accepts the source post, root, direct parent,
current message and their authors. PostgreSQL stores input events, semantic
results and the engine's scoped request/pending state. The API has no outbound
comment effect.

## Local setup

Use Python 3.12+, Docker, and an isolated local PostgreSQL database. Do not point
the test instructions at a shared database.

```sh
cp -n .env.example .env
```

Set `INTENT_API_KEY` and `OPENROUTER_API_KEY` in the ignored `.env`. Set the
`INTENT_MODEL_MAX_CALLS`, `INTENT_MODEL_MAX_COST_USD`, and
`INTENT_MODEL_RESERVE_USD` from the approved budget for this environment. These
caps intentionally have no default. The service checks the append-only budget
ledger before every live model call and reserves each attempt (including Luna
retries) atomically. Unknown or unreadable ledger entries stop model calls.
Never clear or reset the ledger to make the service ready.

The existing sandbox usage before the ledger is represented once by
`SHARED_PRIOR_CALLS` / `SHARED_PRIOR_COST_USD` in `scripts/budget_ledger.py`;
it is not copied into the ledger. The API and sandbox apply that same baseline
when checking their cap. Keep their ledger path and approved cap aligned when
both are used, or calls from one path will not be visible to the other.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[postgres]'
docker compose --profile intent up -d intent-postgres
```

Load local settings into the shell, apply the versioned migration explicitly,
then start the API:

```sh
set -a
. ./.env
set +a
python -m app.pg_adapter migrate
docker compose --profile intent up --build -d intent-api
```

The host migration uses `MAT_INTENT_DATABASE_URL`; the container uses
`INTENT_SERVICE_DATABASE_URL`. The service never migrates a database at startup.
The example PostgreSQL password is for a disposable local database only.

Check process health and readiness:

```sh
curl -fsS http://127.0.0.1:8780/health
curl -i http://127.0.0.1:8780/ready
```

`/health` reports only that the HTTP process responds. `/ready` checks API auth,
PostgreSQL schema/connectivity, provider key and budget configuration/ledger.
When those are missing it returns `503` with reason strings and the request
endpoint does not call a model.

## Request and response

`POST /v1/intent` accepts `api_version: "1"` (optional; defaults to `1`), a
unique `event_id`, `page_id`, a post `{id,text,vehicles?}`, `thread_id`, root and
current messages `{id,actor_id,text}`, optional direct parent of the same
shape, and optional ISO `event_time`. The root ID must equal `thread_id`.
Maximum body size is 128 KiB, each text is limited to 20,000 characters, and
combined post/message text to 50,000 characters. Unknown fields are rejected.

Example root event:

```sh
curl -sS http://127.0.0.1:8780/v1/intent \
  -H "Authorization: Bearer $INTENT_API_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary '{
    "api_version":"1",
    "event_id":"event-001",
    "page_id":"brand-01",
    "post":{"id":"post-01","text":"Thông tin về Hyundai Creta","vehicles":["Creta"]},
    "thread_id":"comment-01",
    "root":{"id":"comment-01","actor_id":"user-01","text":"Giá lăn bánh Creta bao nhiêu?"},
    "parent":null,
    "current":{"id":"comment-01","actor_id":"user-01","text":"Giá lăn bánh Creta bao nhiêu?"}
  }'
```

For a reply, retain `page_id`, `post.id`, and `thread_id`; send the original
root, the reply's direct parent and a new current message/event ID. For example,
`current` can be `{"id":"comment-02","actor_id":"user-01","text":"Bản cao nhé"}`
with the root comment as `parent`. A different actor may read root/parent text
as thread context, but their state, pending answers and contact preferences
remain isolated. Reusing the same event ID and payload returns the stored
result; reusing it with a different payload returns `409 EVENT_ID_CONFLICT`.

The response includes `api_version`, `semantic_version`, v2 semantic
(`intent_labels`, nullable `category`, `tags`, operations, evidence and
uncertainties), policy routing/reason, state changes, scoped state after
processing, and a trace with model/schema/prompt versions, state snapshot hash,
latency, reported usage and known cost. Unknown taxonomy is `null`/empty rather
than inferred. `outbound_effects_enabled` is always `false`.

Luna uses the pinned `openai/gpt-6-luna-20260922` model ID and strict JSON
Schema output. The adapter maps the application contract to the provider's
supported schema subset; the application validator remains authoritative.
OpenRouter support is endpoint-specific, so requests require provider parameter
support and are still checked in the engine ([OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs),
[OpenAI supported schema subset](https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas)).

HTTP outcomes include `200` processed or reviewed, `202` another worker is
processing the same event, `401` bad API key, `409` payload/context conflict,
`422` invalid input, `503` unavailable database/model/budget, and `413` oversized
body. A provider failure stays retryable as a durable event; an ambiguous stale
inference lease becomes a durable review rather than an automatic duplicate.

Read an event result or an actor-scoped thread history (both require the same
Bearer key):

```sh
curl -sS -H "Authorization: Bearer $INTENT_API_KEY" \
  http://127.0.0.1:8780/v1/events/event-001
curl -sS -H "Authorization: Bearer $INTENT_API_KEY" \
  'http://127.0.0.1:8780/v1/threads/comment-01?page_id=brand-01&post_id=post-01&actor_id=user-01'
```

## Server configuration and limits

The API container binds to the Compose network and is published on loopback by
default. For a server, supply secrets through the deployment secret manager,
use HTTPS at the ingress, restrict network access, configure a non-default
database credential and explicit model budget caps, and persist `runs/` on a
filesystem that supports atomic `flock` so worker reservations share one ledger.
Do not expose the example local password, API key, or local-only port settings.

`INTENT_JEV_SHADOW_ENABLED=false` by default. Enabling it requires its own
positive `INTENT_JEV_MODEL_MAX_CALLS`, `INTENT_JEV_MODEL_MAX_COST_USD`, and
`INTENT_JEV_MODEL_RESERVE_USD`; Jev never changes the Luna/policy result. The
current code has no evaluated threshold, so the fast path remains off.
The adapter uses the documented OpenRouter Decisions endpoint and pinned
`typesafe/jev-1.14` model ([Jev API documentation](https://openrouter.ai/docs/guides/community/jev)).

This repository currently has no approved Vietnamese human-gold holdout and
the local environment has no configured provider key/budget for a live request.
Offline tests therefore do not establish model quality, live cost/latency, or
production capacity. Run the documented PostgreSQL-only suite on a disposable
`*_test` database before deployment.
