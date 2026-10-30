ALTER TABLE persona_stories ADD COLUMN IF NOT EXISTS vehicle_use TEXT NOT NULL DEFAULT 'unknown';
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
CREATE OR REPLACE FUNCTION persona_story_vehicle_use_immutable() RETURNS trigger AS $$
BEGIN
 IF NEW.vehicle_use IS DISTINCT FROM OLD.vehicle_use THEN
   RAISE EXCEPTION 'story is immutable';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER persona_story_vehicle_use_immutable BEFORE UPDATE OF vehicle_use ON persona_stories
FOR EACH ROW EXECUTE FUNCTION persona_story_vehicle_use_immutable();
