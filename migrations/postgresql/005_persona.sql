CREATE TABLE personas (
  persona_id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL, stance TEXT NOT NULL DEFAULT 'peer_user' CHECK (stance='peer_user'),
  source_kind TEXT NOT NULL CHECK (source_kind IN ('fictional_fixture','user_provided','official_source','document','other')),
  status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','APPROVED','RETIRED')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE persona_identifiers (
  identifier_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(persona_id),
  identifier TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('slug','display','alias','dealer_ref')),
  scope TEXT NOT NULL DEFAULT 'persona', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(identifier, kind, scope)
);
CREATE UNIQUE INDEX persona_identifiers_human_unique
  ON persona_identifiers(lower(identifier)) WHERE kind <> 'dealer_ref';
CREATE TABLE persona_key_registry (
  category TEXT NOT NULL, canonical_key TEXT NOT NULL,
  value_type TEXT NOT NULL CHECK (value_type IN ('text','integer','number','boolean','json')),
  unit TEXT, cardinality TEXT NOT NULL CHECK (cardinality IN ('singleton','multi')),
  entity_kind TEXT, temporal_mode TEXT NOT NULL DEFAULT 'current'
    CHECK (temporal_mode IN ('current','interval','event','static')),
  permitted_templates TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(category, canonical_key)
);
CREATE TABLE persona_facts (
  fact_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(persona_id),
  entity_id TEXT, category TEXT NOT NULL, canonical_key TEXT NOT NULL,
  value_type TEXT NOT NULL CHECK (value_type IN ('text','integer','number','boolean','json')),
  value_text TEXT, value_integer INTEGER, value_number DOUBLE PRECISION, value_boolean SMALLINT CHECK (value_boolean IN (0,1)), value_json TEXT,
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
CREATE TABLE persona_fact_approvals (
  approval_id TEXT UNIQUE NOT NULL, fact_id TEXT NOT NULL REFERENCES persona_facts(fact_id),
  decision TEXT NOT NULL CHECK (decision IN ('approved','rejected','fixture')),
  approved_by TEXT NOT NULL, approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  seq BIGSERIAL PRIMARY KEY
);
CREATE TABLE persona_releases (
  release_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(persona_id),
  status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','PUBLISHED','FIXTURE')),
  style TEXT NOT NULL DEFAULT '{}', checksum TEXT, approved_by TEXT, published_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(persona_id, release_id)
);
CREATE TABLE release_facts (
  release_id TEXT NOT NULL, persona_id TEXT NOT NULL, fact_id TEXT NOT NULL,
  approval_id TEXT NOT NULL REFERENCES persona_fact_approvals(approval_id),
  PRIMARY KEY(release_id, fact_id),
  FOREIGN KEY(persona_id, release_id) REFERENCES persona_releases(persona_id, release_id),
  FOREIGN KEY(persona_id, fact_id) REFERENCES persona_facts(persona_id, fact_id)
);
CREATE TABLE persona_hooks (
  hook_id TEXT PRIMARY KEY, release_id TEXT NOT NULL REFERENCES persona_releases(release_id),
  text TEXT NOT NULL CHECK(length(text)<=600), purpose TEXT NOT NULL,
  required_fact_ids TEXT NOT NULL DEFAULT '[]', claims TEXT NOT NULL DEFAULT '[]',
  max_per_conversation INTEGER NOT NULL DEFAULT 1 CHECK(max_per_conversation>=0),
  approval TEXT NOT NULL DEFAULT 'draft' CHECK(approval IN ('draft','approved','fixture','rejected'))
);
CREATE TABLE persona_utterances (
  utterance_id TEXT PRIMARY KEY, release_id TEXT NOT NULL REFERENCES persona_releases(release_id),
  body TEXT NOT NULL CHECK(length(body)<=600), purpose TEXT NOT NULL,
  topic TEXT, canonical_key TEXT, required_fact_ids TEXT NOT NULL DEFAULT '[]', claims TEXT NOT NULL DEFAULT '[]',
  style TEXT NOT NULL DEFAULT 'neutral', hook_flag SMALLINT NOT NULL DEFAULT 0 CHECK(hook_flag IN (0,1)),
  approval TEXT NOT NULL DEFAULT 'draft' CHECK(approval IN ('draft','approved','fixture','rejected'))
);
CREATE TABLE post_persona_binding (
  page_id TEXT NOT NULL, post_id TEXT NOT NULL, persona_id TEXT NOT NULL, release_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(page_id, post_id),
  FOREIGN KEY(persona_id, release_id) REFERENCES persona_releases(persona_id, release_id)
);
CREATE TABLE persona_revocations (
  fact_id TEXT UNIQUE NOT NULL REFERENCES persona_facts(fact_id), reason TEXT NOT NULL,
  revoked_by TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  seq BIGSERIAL PRIMARY KEY
);
CREATE TABLE persona_told (
  outbox_key TEXT NOT NULL, persona_id TEXT NOT NULL, release_id TEXT NOT NULL,
  page_id TEXT NOT NULL, post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL, actor_id TEXT NOT NULL,
  utterance_id TEXT NOT NULL, delivery_status TEXT NOT NULL,
  PRIMARY KEY(outbox_key, utterance_id)
);
CREATE TABLE persona_hook_usage (
  page_id TEXT NOT NULL, post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL,
  actor_id TEXT NOT NULL, hook_id TEXT NOT NULL, outbox_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('INTENDED','CLAIMED','UNKNOWN','SENT','FAILED')),
  PRIMARY KEY(page_id, post_id, thread_root_id, actor_id, hook_id, outbox_key)
);
CREATE TABLE persona_root_analyses (
  page_id TEXT NOT NULL, post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL,
  root_comment_id TEXT NOT NULL, root_actor_id TEXT NOT NULL, root_text TEXT NOT NULL,
  semantic TEXT NOT NULL, tags TEXT NOT NULL, persona_id TEXT NOT NULL, release_id TEXT NOT NULL,
  persona_snapshot TEXT NOT NULL, reply_text TEXT, model_version TEXT, schema_version TEXT,
  source_kind TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(page_id, post_id, thread_root_id),
  FOREIGN KEY(persona_id, release_id) REFERENCES persona_releases(persona_id, release_id)
);
CREATE INDEX persona_facts_lookup ON persona_facts(persona_id,category,canonical_key);
CREATE INDEX persona_fact_approvals_latest ON persona_fact_approvals(fact_id,seq DESC);
CREATE INDEX release_facts_fact ON release_facts(persona_id,release_id,fact_id);
CREATE INDEX persona_revocations_epoch ON persona_revocations(seq);

CREATE RULE persona_facts_no_update AS ON UPDATE TO persona_facts DO INSTEAD NOTHING;
CREATE RULE persona_facts_no_delete AS ON DELETE TO persona_facts DO INSTEAD NOTHING;
CREATE RULE persona_approvals_no_update AS ON UPDATE TO persona_fact_approvals DO INSTEAD NOTHING;
CREATE RULE persona_approvals_no_delete AS ON DELETE TO persona_fact_approvals DO INSTEAD NOTHING;
CREATE RULE persona_revocations_no_update AS ON UPDATE TO persona_revocations DO INSTEAD NOTHING;
CREATE RULE persona_revocations_no_delete AS ON DELETE TO persona_revocations DO INSTEAD NOTHING;
CREATE RULE persona_release_facts_insert_guard AS ON INSERT TO release_facts
  WHERE EXISTS (SELECT 1 FROM persona_releases r WHERE r.release_id=NEW.release_id AND r.status='PUBLISHED')
  DO INSTEAD NOTHING;
CREATE RULE persona_release_facts_update_guard AS ON UPDATE TO release_facts
  WHERE EXISTS (SELECT 1 FROM persona_releases r WHERE r.release_id=OLD.release_id AND r.status='PUBLISHED')
  DO INSTEAD NOTHING;
CREATE RULE persona_release_facts_delete_guard AS ON DELETE TO release_facts
  WHERE EXISTS (SELECT 1 FROM persona_releases r WHERE r.release_id=OLD.release_id AND r.status='PUBLISHED')
  DO INSTEAD NOTHING;
CREATE RULE persona_utterances_update_guard AS ON UPDATE TO persona_utterances
  WHERE EXISTS (SELECT 1 FROM persona_releases r WHERE r.release_id=OLD.release_id AND r.status IN ('PUBLISHED','FIXTURE'))
  DO INSTEAD NOTHING;
CREATE RULE persona_utterances_delete_guard AS ON DELETE TO persona_utterances
  WHERE EXISTS (SELECT 1 FROM persona_releases r WHERE r.release_id=OLD.release_id AND r.status IN ('PUBLISHED','FIXTURE'))
  DO INSTEAD NOTHING;
CREATE RULE persona_hooks_update_guard AS ON UPDATE TO persona_hooks
  WHERE EXISTS (SELECT 1 FROM persona_releases r WHERE r.release_id=OLD.release_id AND r.status IN ('PUBLISHED','FIXTURE'))
  DO INSTEAD NOTHING;
CREATE RULE persona_hooks_delete_guard AS ON DELETE TO persona_hooks
  WHERE EXISTS (SELECT 1 FROM persona_releases r WHERE r.release_id=OLD.release_id AND r.status IN ('PUBLISHED','FIXTURE'))
  DO INSTEAD NOTHING;
CREATE RULE persona_releases_no_delete_published AS ON DELETE TO persona_releases
  WHERE OLD.status IN ('PUBLISHED','FIXTURE') DO INSTEAD NOTHING;
CREATE RULE persona_releases_no_update_published AS ON UPDATE TO persona_releases
  WHERE OLD.status IN ('PUBLISHED','FIXTURE') DO INSTEAD NOTHING;
CREATE RULE persona_bindings_no_update AS ON UPDATE TO post_persona_binding DO INSTEAD NOTHING;
CREATE RULE persona_bindings_no_delete AS ON DELETE TO post_persona_binding DO INSTEAD NOTHING;
