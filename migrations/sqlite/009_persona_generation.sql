CREATE TABLE IF NOT EXISTS persona_memories (
 memory_id TEXT PRIMARY KEY,
 persona_id TEXT NOT NULL,
 release_id TEXT NOT NULL,
 topic TEXT NOT NULL,
 occurred_at TEXT,
 precision TEXT NOT NULL,
 source_ref TEXT NOT NULL,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('fictional_fixture','user_provided','official_source','document','other')),
 approval TEXT NOT NULL CHECK(approval IN ('draft','approved','fixture','rejected')),
 privacy TEXT NOT NULL CHECK(privacy IN ('public','private','sensitive')),
 tellable TEXT NOT NULL CHECK(tellable IN ('public','ask_only','never')),
 fact_ids TEXT NOT NULL,
 approved_summary TEXT NOT NULL,
 consent_ref TEXT,
 checksum TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(persona_id, release_id) REFERENCES persona_releases(persona_id, release_id)
);
CREATE INDEX IF NOT EXISTS persona_memories_release ON persona_memories(release_id, memory_id);
CREATE TRIGGER persona_memories_insert_draft BEFORE INSERT ON persona_memories
WHEN (SELECT status FROM persona_releases WHERE release_id=NEW.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER persona_memories_update_draft BEFORE UPDATE ON persona_memories
WHEN (SELECT status FROM persona_releases WHERE release_id=OLD.release_id) <> 'DRAFT'
 OR (SELECT status FROM persona_releases WHERE release_id=NEW.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;
CREATE TRIGGER persona_memories_delete_draft BEFORE DELETE ON persona_memories
WHEN (SELECT status FROM persona_releases WHERE release_id=OLD.release_id) <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'published release is immutable'); END;

CREATE TABLE IF NOT EXISTS persona_memory_revocations (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 memory_id TEXT NOT NULL REFERENCES persona_memories(memory_id),
 reason TEXT NOT NULL,
 revoked_by TEXT NOT NULL,
 revoked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS persona_memory_revocations_epoch ON persona_memory_revocations(seq);
CREATE TRIGGER persona_memory_revocations_no_update BEFORE UPDATE ON persona_memory_revocations
BEGIN SELECT RAISE(ABORT, 'revocations are append only'); END;
CREATE TRIGGER persona_memory_revocations_no_delete BEFORE DELETE ON persona_memory_revocations
BEGIN SELECT RAISE(ABORT, 'revocations are append only'); END;

CREATE TABLE IF NOT EXISTS generated_reply_drafts (
 draft_id TEXT PRIMARY KEY,
 page_id TEXT NOT NULL,
 post_id TEXT NOT NULL,
 thread_root_id TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 comment_id TEXT NOT NULL,
 payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING',
 reviewer TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(page_id,comment_id)
);
CREATE TABLE IF NOT EXISTS persona_generation_turns (
 page_id TEXT NOT NULL,
 post_id TEXT NOT NULL,
 thread_root_id TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 comment_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL,
 PRIMARY KEY(page_id,post_id,thread_root_id,actor_id,comment_id)
);
CREATE TABLE IF NOT EXISTS persona_memory_deliveries (
 dedupe_key TEXT PRIMARY KEY,
 page_id TEXT NOT NULL,
 post_id TEXT NOT NULL,
 thread_root_id TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 turn_ordinal INTEGER NOT NULL,
 memory_ids TEXT NOT NULL,
 created_at TEXT NOT NULL
);
