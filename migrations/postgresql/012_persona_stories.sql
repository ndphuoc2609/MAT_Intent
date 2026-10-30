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
CREATE OR REPLACE FUNCTION persona_stories_enforce_immutable() RETURNS trigger AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'story is immutable'; END IF;
 IF ROW(NEW.memory_id,NEW.persona_id,NEW.release_id,NEW.topic,NEW.event_text,NEW.time_start,
        NEW.time_end,NEW.entities_json,NEW.base_fact_ids_json,NEW.new_details_json,
        NEW.relation_json,NEW.label,NEW.created_at)
    IS DISTINCT FROM
    ROW(OLD.memory_id,OLD.persona_id,OLD.release_id,OLD.topic,OLD.event_text,OLD.time_start,
        OLD.time_end,OLD.entities_json,OLD.base_fact_ids_json,OLD.new_details_json,
        OLD.relation_json,OLD.label,OLD.created_at)
    OR OLD.status <> 'ACTIVE' OR NEW.status <> 'REVOKED'
    OR NEW.revoked_at IS NULL OR NEW.revoke_reason IS NULL THEN
   RAISE EXCEPTION 'story is immutable';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER persona_stories_immutable BEFORE UPDATE OR DELETE ON persona_stories
FOR EACH ROW EXECUTE FUNCTION persona_stories_enforce_immutable();
