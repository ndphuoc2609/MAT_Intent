CREATE TABLE IF NOT EXISTS persona_memory_reservations (
 dedupe_key TEXT NOT NULL PRIMARY KEY,
 page_id TEXT NOT NULL,
 post_id TEXT NOT NULL,
 thread_root_id TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 memory_id TEXT NOT NULL REFERENCES persona_memories(memory_id),
 turn_ordinal INTEGER NOT NULL,
 reserved_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('RESERVED','SENDING')),
 UNIQUE(page_id, post_id, thread_root_id, actor_id)
);
CREATE INDEX IF NOT EXISTS persona_memory_reservations_status
 ON persona_memory_reservations(status, reserved_at);
