# PostgreSQL state backend

The Meta runtime can use the shared engine state operations on PostgreSQL via
`META_DATABASE_URL`. SQLite remains the default for existing callers and the
local sandbox. PostgreSQL changes are applied only by the explicit migration
command; starting the service never migrates a database.

Install the extra with `pip install '.[postgres]'`. In a disposable local
environment, start PostgreSQL 16 on a loopback-only port:

```sh
docker run --rm --name mat-intent-pg \
  -e POSTGRES_USER=mat_intent -e POSTGRES_PASSWORD=local-only \
  -e POSTGRES_DB=mat_intent_test -p 127.0.0.1:55432:5432 postgres:16
```

Run the versioned migrations and the PostgreSQL-only integration tests against
that database:

```sh
export MAT_INTENT_DATABASE_URL='postgresql://mat_intent:local-only@127.0.0.1:55432/mat_intent_test'
python3 -m app.pg_adapter migrate
MAT_INTENT_TEST_DATABASE_URL="$MAT_INTENT_DATABASE_URL" \
  python3 -m unittest discover -s tests/postgres -p 'test*.py' -v
```

The integration suite refuses database names without the `_test` suffix. It
creates a uniquely named schema, applies migrations there, and drops only that
schema during cleanup. For the service, set `META_DATABASE_URL` to a database
that was migrated explicitly. An empty setting leaves the existing SQLite
path active. Do not point the test variable at a shared or production database.

Migrations live in `migrations/postgresql/` and are recorded in
`schema_migrations`. Event processing uses a transaction-scoped advisory lock
for page/post/thread/actor, event dedupe uses a unique key, and request/pending
updates keep optimistic version predicates. The PostgreSQL connection runs in
autocommit mode outside explicit state transactions, so context reads do not
hold a transaction while a model request is in flight.
