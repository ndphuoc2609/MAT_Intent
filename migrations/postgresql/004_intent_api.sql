ALTER TABLE meta_inbox ADD COLUMN IF NOT EXISTS lease_owner TEXT;
ALTER TABLE meta_inbox ADD COLUMN IF NOT EXISTS lease_until TEXT;
CREATE INDEX IF NOT EXISTS meta_inbox_claimable
  ON meta_inbox(status, next_attempt_at, lease_until, seq);

CREATE TABLE IF NOT EXISTS intent_api_events (
  event_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
  page_id TEXT NOT NULL, post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL,
  actor_id TEXT NOT NULL, comment_id TEXT NOT NULL, payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT, lease_until TEXT, result TEXT, last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, seq BIGSERIAL UNIQUE NOT NULL
);
CREATE INDEX IF NOT EXISTS intent_api_events_thread
  ON intent_api_events(page_id, post_id, thread_root_id, seq);
CREATE INDEX IF NOT EXISTS intent_api_events_claimable
  ON intent_api_events(status, lease_until, seq);
