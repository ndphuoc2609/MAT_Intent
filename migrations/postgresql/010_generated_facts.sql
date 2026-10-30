CREATE TABLE IF NOT EXISTS persona_generated_facts (
 gen_fact_id TEXT PRIMARY KEY,
 persona_id TEXT NOT NULL REFERENCES personas(persona_id),
 fact_key TEXT NOT NULL,
 value_text TEXT NOT NULL CHECK(length(value_text) BETWEEN 1 AND 160),
 precision TEXT NOT NULL,
 source_ref TEXT NOT NULL,
 created_comment_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PENDING','ACTIVE','DISCARDED','REVOKED')),
 revoke_reason TEXT,
 revoked_by TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS persona_generated_facts_one_live_value
 ON persona_generated_facts(persona_id, fact_key) WHERE status IN ('PENDING','ACTIVE');
CREATE INDEX IF NOT EXISTS persona_generated_facts_status
 ON persona_generated_facts(persona_id, status, created_at);
