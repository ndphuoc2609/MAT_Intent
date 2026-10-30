ALTER TABLE persona_stories ADD COLUMN vehicle_use TEXT NOT NULL DEFAULT 'unknown';
CREATE TABLE IF NOT EXISTS persona_story_revisions (
 revision_id TEXT PRIMARY KEY,
 memory_id TEXT NOT NULL REFERENCES persona_stories(memory_id),
 version INTEGER NOT NULL,
 base_version INTEGER NOT NULL,
 revision_type TEXT NOT NULL CHECK(revision_type IN ('ADDENDUM','CORRECTION')),
 details_json TEXT NOT NULL,
 provenance_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(memory_id, version)
);
CREATE INDEX IF NOT EXISTS persona_story_revisions_memory ON persona_story_revisions(memory_id, version);
CREATE TRIGGER IF NOT EXISTS persona_story_vehicle_use_immutable BEFORE UPDATE ON persona_stories
WHEN NEW.vehicle_use IS NOT OLD.vehicle_use
BEGIN SELECT RAISE(ABORT, 'story is immutable'); END;
