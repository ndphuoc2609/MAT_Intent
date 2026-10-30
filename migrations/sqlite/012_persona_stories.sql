CREATE TABLE IF NOT EXISTS persona_story_versions (
 persona_id TEXT PRIMARY KEY REFERENCES personas(persona_id), version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS persona_stories (
 memory_id TEXT PRIMARY KEY,
 persona_id TEXT NOT NULL REFERENCES personas(persona_id),
 release_id TEXT NOT NULL REFERENCES persona_releases(release_id),
 topic TEXT NOT NULL,
 event_text TEXT NOT NULL,
 time_start TEXT,
 time_end TEXT,
 entities_json TEXT NOT NULL,
 base_fact_ids_json TEXT NOT NULL,
 new_details_json TEXT NOT NULL,
 relation_json TEXT,
 label TEXT NOT NULL CHECK(label='fictional_generated'),
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),
 created_at TEXT NOT NULL,
 revoked_at TEXT,
 revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS persona_stories_scope ON persona_stories(persona_id, status, topic);
CREATE TABLE IF NOT EXISTS persona_story_requests (
 persona_id TEXT NOT NULL REFERENCES personas(persona_id),
 idempotency_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 result_json TEXT NOT NULL,
 PRIMARY KEY(persona_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS persona_story_deliveries (
 dedupe_key TEXT NOT NULL,
 memory_id TEXT NOT NULL REFERENCES persona_stories(memory_id),
 page_id TEXT NOT NULL,
 post_id TEXT NOT NULL,
 thread_root_id TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 delivered_at TEXT NOT NULL,
 PRIMARY KEY(dedupe_key,memory_id)
);
CREATE INDEX IF NOT EXISTS persona_story_delivery_scope ON persona_story_deliveries(page_id,post_id,thread_root_id,actor_id);
CREATE TABLE IF NOT EXISTS persona_story_delivery_receipts (
 dedupe_key TEXT PRIMARY KEY,
 persona_id TEXT NOT NULL REFERENCES personas(persona_id),
 scope_json TEXT NOT NULL,
 memory_ids_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS persona_story_reservations (
 dedupe_key TEXT PRIMARY KEY,
 persona_id TEXT NOT NULL REFERENCES personas(persona_id),
 page_id TEXT NOT NULL, post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 memory_id TEXT NOT NULL REFERENCES persona_stories(memory_id),
 status TEXT NOT NULL CHECK(status IN ('RESERVED','SENDING')),
 reserved_at TEXT NOT NULL,
 UNIQUE(page_id,post_id,thread_root_id,actor_id)
);
CREATE TRIGGER IF NOT EXISTS persona_stories_immutable BEFORE UPDATE ON persona_stories
WHEN NEW.memory_id IS NOT OLD.memory_id OR NEW.persona_id IS NOT OLD.persona_id
 OR NEW.release_id IS NOT OLD.release_id OR NEW.topic IS NOT OLD.topic
 OR NEW.event_text IS NOT OLD.event_text OR NEW.time_start IS NOT OLD.time_start
 OR NEW.time_end IS NOT OLD.time_end OR NEW.entities_json IS NOT OLD.entities_json
 OR NEW.base_fact_ids_json IS NOT OLD.base_fact_ids_json OR NEW.new_details_json IS NOT OLD.new_details_json
 OR NEW.relation_json IS NOT OLD.relation_json OR NEW.label IS NOT OLD.label
 OR NEW.created_at IS NOT OLD.created_at OR OLD.status <> 'ACTIVE' OR NEW.status <> 'REVOKED'
 OR NEW.revoked_at IS NULL OR NEW.revoke_reason IS NULL
BEGIN SELECT RAISE(ABORT, 'story is immutable'); END;
CREATE TRIGGER IF NOT EXISTS persona_stories_no_delete BEFORE DELETE ON persona_stories
BEGIN SELECT RAISE(ABORT, 'story is immutable'); END;
