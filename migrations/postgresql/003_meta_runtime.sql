CREATE TABLE meta_inbox (
  comment_id TEXT PRIMARY KEY, event TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, last_error TEXT, result TEXT,
  next_attempt_at TEXT, seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE meta_manual_writes (
  idempotency_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
  status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE meta_delivery_attempts (
  dedupe_key TEXT PRIMARY KEY, status TEXT NOT NULL, remote_id TEXT,
  updated_at TEXT NOT NULL, seq BIGSERIAL UNIQUE NOT NULL
);
