PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS personas (
  persona_id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL, stance TEXT NOT NULL DEFAULT 'peer_user' CHECK (stance='peer_user'),
  source_kind TEXT NOT NULL CHECK (source_kind IN ('fictional_fixture','user_provided','official_source','document','other')),
  status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','APPROVED','RETIRED')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS persona_identifiers (
  identifier_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(persona_id),
  identifier TEXT NOT NULL COLLATE NOCASE, kind TEXT NOT NULL CHECK (kind IN ('slug','display','alias','dealer_ref')),
  scope TEXT NOT NULL DEFAULT 'persona', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(identifier, kind, scope)
);
CREATE UNIQUE INDEX IF NOT EXISTS persona_identifiers_human_unique
  ON persona_identifiers(lower(identifier)) WHERE kind <> 'dealer_ref';
CREATE TABLE IF NOT EXISTS persona_key_registry (
  category TEXT NOT NULL, canonical_key TEXT NOT NULL,
  value_type TEXT NOT NULL CHECK (value_type IN ('text','integer','number','boolean','json')),
  unit TEXT, cardinality TEXT NOT NULL CHECK (cardinality IN ('singleton','multi')),
  entity_kind TEXT, temporal_mode TEXT NOT NULL DEFAULT 'current'
    CHECK (temporal_mode IN ('current','interval','event','static')),
  permitted_templates TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(category, canonical_key)
);
CREATE TABLE IF NOT EXISTS persona_facts (
  fact_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(persona_id),
  entity_id TEXT, category TEXT NOT NULL, canonical_key TEXT NOT NULL,
  value_type TEXT NOT NULL CHECK (value_type IN ('text','integer','number','boolean','json')),
  value_text TEXT, value_integer INTEGER, value_number REAL, value_boolean INTEGER CHECK (value_boolean IN (0,1)), value_json TEXT,
  unit TEXT, valid_from TEXT, valid_to TEXT, as_of TEXT, precision TEXT,
  tellable TEXT NOT NULL CHECK (tellable IN ('public','ask_only','never')),
  source_ref TEXT NOT NULL, source_kind TEXT NOT NULL CHECK (source_kind IN ('fictional_fixture','user_provided','official_source','document','other')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(persona_id, fact_id),
  FOREIGN KEY(category, canonical_key) REFERENCES persona_key_registry(category, canonical_key),
  CHECK ((valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to)),
  CHECK ((value_type='text' AND value_text IS NOT NULL AND value_integer IS NULL AND value_number IS NULL AND value_boolean IS NULL AND value_json IS NULL)
      OR (value_type='integer' AND value_text IS NULL AND value_integer IS NOT NULL AND value_number IS NULL AND value_boolean IS NULL AND value_json IS NULL)
      OR (value_type='number' AND value_text IS NULL AND value_integer IS NULL AND value_number IS NOT NULL AND value_boolean IS NULL AND value_json IS NULL)
      OR (value_type='boolean' AND value_text IS NULL AND value_integer IS NULL AND value_number IS NULL AND value_boolean IS NOT NULL AND value_json IS NULL)
      OR (value_type='json' AND value_text IS NULL AND value_integer IS NULL AND value_number IS NULL AND value_boolean IS NULL AND value_json IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS persona_fact_approvals (
  approval_id TEXT NOT NULL UNIQUE, fact_id TEXT NOT NULL REFERENCES persona_facts(fact_id),
  decision TEXT NOT NULL CHECK (decision IN ('approved','rejected','fixture')),
  approved_by TEXT NOT NULL, approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  seq INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE TABLE IF NOT EXISTS persona_releases (
  release_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(persona_id),
  status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','PUBLISHED','FIXTURE')),
  style TEXT NOT NULL DEFAULT '{}', checksum TEXT, approved_by TEXT, published_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(persona_id, release_id)
);
CREATE TABLE IF NOT EXISTS release_facts (
  release_id TEXT NOT NULL, persona_id TEXT NOT NULL, fact_id TEXT NOT NULL,
  approval_id TEXT NOT NULL REFERENCES persona_fact_approvals(approval_id),
  PRIMARY KEY(release_id, fact_id),
  FOREIGN KEY(persona_id, release_id) REFERENCES persona_releases(persona_id, release_id),
  FOREIGN KEY(persona_id, fact_id) REFERENCES persona_facts(persona_id, fact_id)
);
CREATE TABLE IF NOT EXISTS persona_hooks (
  hook_id TEXT PRIMARY KEY, release_id TEXT NOT NULL REFERENCES persona_releases(release_id),
  text TEXT NOT NULL CHECK(length(text)<=600), purpose TEXT NOT NULL,
  required_fact_ids TEXT NOT NULL DEFAULT '[]', claims TEXT NOT NULL DEFAULT '[]',
  max_per_conversation INTEGER NOT NULL DEFAULT 1 CHECK(max_per_conversation>=0),
  approval TEXT NOT NULL DEFAULT 'draft' CHECK(approval IN ('draft','approved','fixture','rejected'))
);
CREATE TABLE IF NOT EXISTS persona_utterances (
  utterance_id TEXT PRIMARY KEY, release_id TEXT NOT NULL REFERENCES persona_releases(release_id),
  body TEXT NOT NULL CHECK(length(body)<=600), purpose TEXT NOT NULL,
  topic TEXT, canonical_key TEXT, required_fact_ids TEXT NOT NULL DEFAULT '[]', claims TEXT NOT NULL DEFAULT '[]',
  style TEXT NOT NULL DEFAULT 'neutral', hook_flag INTEGER NOT NULL DEFAULT 0 CHECK(hook_flag IN (0,1)),
  approval TEXT NOT NULL DEFAULT 'draft' CHECK(approval IN ('draft','approved','fixture','rejected'))
);
CREATE TABLE IF NOT EXISTS post_persona_binding (
  page_id TEXT NOT NULL, post_id TEXT NOT NULL, persona_id TEXT NOT NULL, release_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(page_id, post_id),
  FOREIGN KEY(persona_id, release_id) REFERENCES persona_releases(persona_id, release_id)
);
CREATE TABLE IF NOT EXISTS persona_revocations (
  fact_id TEXT NOT NULL UNIQUE REFERENCES persona_facts(fact_id), reason TEXT NOT NULL,
  revoked_by TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  seq INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE TABLE IF NOT EXISTS persona_told (
  outbox_key TEXT NOT NULL, persona_id TEXT NOT NULL, release_id TEXT NOT NULL,
  page_id TEXT NOT NULL, post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL, actor_id TEXT NOT NULL,
  utterance_id TEXT NOT NULL, delivery_status TEXT NOT NULL,
  PRIMARY KEY(outbox_key, utterance_id)
);
CREATE TABLE IF NOT EXISTS persona_hook_usage (
  page_id TEXT NOT NULL, post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL,
  actor_id TEXT NOT NULL, hook_id TEXT NOT NULL, outbox_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('INTENDED','CLAIMED','UNKNOWN','SENT','FAILED')),
  PRIMARY KEY(page_id, post_id, thread_root_id, actor_id, hook_id, outbox_key)
);
CREATE TABLE IF NOT EXISTS persona_root_analyses (
  page_id TEXT NOT NULL, post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL,
  root_comment_id TEXT NOT NULL, root_actor_id TEXT NOT NULL, root_text TEXT NOT NULL,
  semantic TEXT NOT NULL, tags TEXT NOT NULL,
  persona_id TEXT NOT NULL, release_id TEXT NOT NULL, persona_snapshot TEXT NOT NULL,
  reply_text TEXT, model_version TEXT, schema_version TEXT, source_kind TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(page_id, post_id, thread_root_id),
  FOREIGN KEY(persona_id, release_id) REFERENCES persona_releases(persona_id, release_id)
);

CREATE TRIGGER IF NOT EXISTS persona_facts_no_update BEFORE UPDATE ON persona_facts
BEGIN SELECT RAISE(ABORT, 'persona facts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS persona_facts_no_delete BEFORE DELETE ON persona_facts
BEGIN SELECT RAISE(ABORT, 'persona facts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS persona_fact_approvals_no_update BEFORE UPDATE ON persona_fact_approvals
BEGIN SELECT RAISE(ABORT, 'persona approvals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS persona_fact_approvals_no_delete BEFORE DELETE ON persona_fact_approvals
BEGIN SELECT RAISE(ABORT, 'persona approvals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS persona_revocations_no_update BEFORE UPDATE ON persona_revocations
BEGIN SELECT RAISE(ABORT, 'persona revocations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS persona_revocations_no_delete BEFORE DELETE ON persona_revocations
BEGIN SELECT RAISE(ABORT, 'persona revocations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS release_facts_insert_draft BEFORE INSERT ON release_facts
WHEN (SELECT status FROM persona_releases WHERE release_id=NEW.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS release_facts_update_draft BEFORE UPDATE ON release_facts
WHEN (SELECT status FROM persona_releases WHERE release_id=OLD.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS release_facts_delete_draft BEFORE DELETE ON release_facts
WHEN (SELECT status FROM persona_releases WHERE release_id=OLD.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS releases_no_mutate_published BEFORE UPDATE ON persona_releases
WHEN OLD.status IN ('PUBLISHED','FIXTURE')
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS releases_no_delete_published BEFORE DELETE ON persona_releases
WHEN OLD.status IN ('PUBLISHED','FIXTURE')
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS utterances_insert_draft BEFORE INSERT ON persona_utterances
WHEN (SELECT status FROM persona_releases WHERE release_id=NEW.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS utterances_update_draft BEFORE UPDATE ON persona_utterances
WHEN (SELECT status FROM persona_releases WHERE release_id=OLD.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS utterances_delete_draft BEFORE DELETE ON persona_utterances
WHEN (SELECT status FROM persona_releases WHERE release_id=OLD.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS hooks_insert_draft BEFORE INSERT ON persona_hooks
WHEN (SELECT status FROM persona_releases WHERE release_id=NEW.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS hooks_update_draft BEFORE UPDATE ON persona_hooks
WHEN (SELECT status FROM persona_releases WHERE release_id=OLD.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS hooks_delete_draft BEFORE DELETE ON persona_hooks
WHEN (SELECT status FROM persona_releases WHERE release_id=OLD.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER IF NOT EXISTS binding_no_update BEFORE UPDATE ON post_persona_binding
BEGIN SELECT RAISE(ABORT, 'post persona binding is immutable'); END;
CREATE TRIGGER IF NOT EXISTS binding_no_delete BEFORE DELETE ON post_persona_binding
BEGIN SELECT RAISE(ABORT, 'post persona binding is immutable'); END;
CREATE TRIGGER IF NOT EXISTS root_analysis_no_update BEFORE UPDATE ON persona_root_analyses
BEGIN SELECT RAISE(ABORT, 'root analysis is immutable'); END;
CREATE TRIGGER IF NOT EXISTS root_analysis_no_delete BEFORE DELETE ON persona_root_analyses
BEGIN SELECT RAISE(ABORT, 'root analysis is immutable'); END;
