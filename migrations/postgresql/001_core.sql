CREATE TABLE events (
  dedupe_key TEXT PRIMARY KEY, comment_id TEXT, actor_id TEXT, text TEXT,
  payload TEXT, retracted INTEGER DEFAULT 0, seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE requests (
  id TEXT PRIMARY KEY, page_id TEXT, actor_id TEXT, post_id TEXT,
  thread_root_id TEXT, topic TEXT, status TEXT, version INTEGER, slots TEXT,
  source_events TEXT, desired_outcomes TEXT, created_at TEXT, updated_at TEXT,
  seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE pending (
  id TEXT PRIMARY KEY, request_id TEXT, actor_id TEXT, qtype TEXT,
  expected_slot TEXT, options TEXT, proposition TEXT, issued_at TEXT,
  expires_at TEXT, version INTEGER, status TEXT, basis_version INTEGER,
  basis_slots TEXT, seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE outbox (
  dedupe_key TEXT PRIMARY KEY, request_id TEXT, purpose TEXT, version INTEGER,
  status TEXT, payload TEXT, created_at TEXT, claimed_at TEXT, claimed_by TEXT,
  seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE proposal_rejections (
  request_id TEXT, fingerprint TEXT, created_at TEXT,
  PRIMARY KEY(request_id, fingerprint), seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE audit (
  id TEXT PRIMARY KEY, comment_id TEXT, kind TEXT, payload TEXT, created_at TEXT,
  seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE contact_prefs (
  page_id TEXT, actor_id TEXT, scope TEXT, PRIMARY KEY(page_id, actor_id),
  seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE contact_blocks (
  page_id TEXT, actor_id TEXT, channel TEXT,
  PRIMARY KEY(page_id, actor_id, channel), seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE handoffs (
  request_id TEXT PRIMARY KEY, owner TEXT, reason TEXT, lock_version INTEGER,
  acquired_at TEXT, released_at TEXT, overdue INTEGER DEFAULT 0,
  seq BIGSERIAL UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL
);
