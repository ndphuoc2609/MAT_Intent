"""PostgreSQL backend for the shared Engine state store (psycopg 3)."""
from __future__ import annotations

import glob
import os
import re
import threading
from pathlib import Path

from .state import Store

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "postgresql"
LATEST_VERSION = 5


def _adapt_sql(sql):
    """Translate the small SQLite SQL subset still used by existing callers."""
    if re.match(r"\s*BEGIN\s+IMMEDIATE\b", sql, re.I):
        sql = re.sub(r"BEGIN\s+IMMEDIATE\b", "BEGIN", sql, flags=re.I)
    sql = re.sub(
        r"json_extract\(\s*([\w.]+)\s*,\s*'\$\.([A-Za-z_]\w*)'\s*\)",
        r"(\1::jsonb ->> '\2')", sql, flags=re.I)
    sql = re.sub(r"\browid\b", "seq", sql, flags=re.I)
    ignored_insert = bool(re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\b", sql, re.I))
    if ignored_insert:
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, count=1, flags=re.I)
    sql = sql.replace("?", "%s")
    if ignored_insert:
        sql = sql.rstrip().removesuffix(";") + " ON CONFLICT DO NOTHING"
    return sql


class _ChangesCursor:
    rowcount = 1
    description = None

    def __init__(self, count):
        self.count = count

    def fetchone(self):
        return (self.count,)

    def fetchall(self):
        return [(self.count,)]


class _Connection:
    def __init__(self, connection):
        self.raw = connection
        self.last_rowcount = 0

    def execute(self, sql, params=()):
        if re.match(r"\s*SELECT\s+changes\(\s*\)\s*;?\s*$", sql, re.I):
            return _ChangesCursor(self.last_rowcount)
        pragma = re.fullmatch(r"\s*PRAGMA\s+table_info\((\w+)\)\s*;?\s*", sql, re.I)
        if pragma:
            cur = self.raw.execute(
                "SELECT ordinal_position-1, column_name, data_type, 0, column_default, 0 "
                "FROM information_schema.columns c WHERE c.table_schema=current_schema() "
                "AND c.table_name=%s ORDER BY ordinal_position", (pragma.group(1).lower(),))
        else:
            adapted = _adapt_sql(sql)
            # Inbox claims use row locking so concurrent service workers skip each other's row.
            if ("FROM meta_inbox" in adapted and "status='PENDING'" in adapted
                    and re.search(r"LIMIT\s+%s\s*$", adapted, re.I)):
                adapted += " FOR UPDATE SKIP LOCKED"
            cur = self.raw.execute(adapted, params)
        self.last_rowcount = cur.rowcount
        return cur

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


class PostgresStore(Store):
    """Store implementation reusing Engine's state operations on PostgreSQL."""

    def __init__(self, connection):
        self.lock = threading.RLock()
        self.conn = _Connection(connection)

    def lock_scope(self, event):
        key = "\x1f".join(str(event.get(k) or "") for k in
                          ("page_id", "post_id", "thread_root_id", "actor_id"))
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (key,))


def migrate(dsn):
    """Apply numbered migrations to the explicitly supplied database."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended('mat-intent-migrations', 0))")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                     "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)")
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for filename in sorted(glob.glob(str(MIGRATIONS / "[0-9][0-9][0-9]_*.sql"))):
            version = int(Path(filename).name[:3])
            if version in applied:
                continue
            conn.execute("BEGIN")
            try:
                for statement in Path(filename).read_text(encoding="utf-8").split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (%s, CURRENT_TIMESTAMP)",
                             (version,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        conn.execute("SELECT pg_advisory_unlock(hashtextextended('mat-intent-migrations', 0))")


def connect(dsn):
    """Open a PostgreSQL Store; schema changes remain an explicit migrate step."""
    import psycopg

    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("PostgreSQL database URL is required")
    connection = psycopg.connect(dsn, autocommit=True)
    try:
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        if versions != set(range(1, LATEST_VERSION + 1)):
            raise RuntimeError("PostgreSQL schema is not current; run python -m app.pg_adapter migrate first")
    except Exception:
        connection.close()
        raise
    return PostgresStore(connection)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2 or sys.argv[1] != "migrate":
        raise SystemExit("usage: python -m app.pg_adapter migrate (uses MAT_INTENT_DATABASE_URL)")
    url = os.environ.get("MAT_INTENT_DATABASE_URL", "")
    if not url:
        raise SystemExit("MAT_INTENT_DATABASE_URL is required")
    migrate(url)
