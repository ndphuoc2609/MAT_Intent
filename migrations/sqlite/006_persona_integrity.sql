CREATE UNIQUE INDEX persona_approvals_pair ON persona_fact_approvals(approval_id, fact_id);
CREATE TABLE release_facts_v6 (
  release_id TEXT NOT NULL, persona_id TEXT NOT NULL, fact_id TEXT NOT NULL, approval_id TEXT NOT NULL,
  PRIMARY KEY(release_id, fact_id),
  FOREIGN KEY(persona_id, release_id) REFERENCES persona_releases(persona_id, release_id),
  FOREIGN KEY(persona_id, fact_id) REFERENCES persona_facts(persona_id, fact_id),
  FOREIGN KEY(approval_id, fact_id) REFERENCES persona_fact_approvals(approval_id, fact_id)
);
INSERT INTO release_facts_v6 SELECT release_id, persona_id, fact_id, approval_id FROM release_facts;
DROP TABLE release_facts;
ALTER TABLE release_facts_v6 RENAME TO release_facts;
CREATE TRIGGER release_facts_insert_draft BEFORE INSERT ON release_facts
WHEN (SELECT status FROM persona_releases WHERE release_id=NEW.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER release_facts_update_draft BEFORE UPDATE ON release_facts
WHEN (SELECT status FROM persona_releases WHERE release_id=OLD.release_id) <> 'DRAFT'
 OR (SELECT status FROM persona_releases WHERE release_id=NEW.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER release_facts_delete_draft BEFORE DELETE ON release_facts
WHEN (SELECT status FROM persona_releases WHERE release_id=OLD.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
