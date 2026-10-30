# MAT_Intent sandbox and API

The repository provides two local entry points:

- `sandbox` on `127.0.0.1:8766`: a persona conversation demo. Replies use `FakeSender` and stay local.
- `intent-api` on `127.0.0.1:8780`: an authenticated API that returns a validated draft and never sends a reply.

Both use the shared engine and can run the bounded model-led tool loop. The model plans across multiple intents, requests scoped facts or memories, records customer evidence, and writes a final reply. The engine checks source, scope, freshness, and delivery rules. Model-led processing is disabled by default; tests use fake providers and do not call OpenRouter.

## Start the sandbox

Configure `DB_NEON` for PostgreSQL and `OPENROUTER_API_KEY` in the ignored `.env`. For persona lookup, configure `DB_Host`, `DB_Port`, `DB_Username`, and `DB_Password` for read-only access to `content_ai_fac.public.personas`.

Apply migrations and start the sandbox:

```sh
docker compose run --rm --no-deps --entrypoint python3 sandbox -m app.pg_adapter migrate
SANDBOX_MODEL_LED_ENABLED=true SANDBOX_ENFORCE_BUDGET=true \
  docker compose up --build -d sandbox
```

Open `http://127.0.0.1:8766/`. Replies are simulated; nothing is published or sent externally. Product facts are not taken from sample data. A product answer requires a current non-sample source; otherwise the model must report that the source is unavailable.

`Nam Creta · kịch bản hư cấu` is a sandbox fixture. Generated persona facts are labeled `generated`, stored outside published releases, and activated only after a successful simulated send. Passing validation establishes internal consistency, not truth.

## Start the Intent API

Set `INTENT_API_KEY`, the PostgreSQL URL, `OPENROUTER_API_KEY`, and explicit call/cost limits. Model-led mode also requires an authorized page allowlist.

```sh
docker compose --profile intent up -d intent-postgres
docker compose run --rm --no-deps --entrypoint python3 intent-api -m app.pg_adapter migrate
MAT_INTENT_MODEL_LED_ENABLED=true \
  MAT_INTENT_MODEL_LED_ALLOWED_PAGES=brand-01 \
  docker compose --profile intent up --build -d intent-api
```

See [docs/intent-api.md](docs/intent-api.md) for the request contract. The API returns drafts and has no outbound reply effect.

## Offline checks

```sh
env -u OPENROUTER_API_KEY python3 -m unittest discover -s tests/unit -q
env -u OPENROUTER_API_KEY python3 -m unittest discover -s tests/integration -q
node --check sandbox/static/app.js
```

PostgreSQL-specific tests use a disposable database ending in `_test`; see [docs/postgresql.md](docs/postgresql.md). Live persona evaluation is opt-in and invokes OpenRouter; see [datasets/persona_conversations/README.md](datasets/persona_conversations/README.md).
