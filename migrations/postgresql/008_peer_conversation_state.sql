CREATE TABLE IF NOT EXISTS peer_conversation_state (
 page_id TEXT NOT NULL, post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 payload TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
 PRIMARY KEY(page_id,post_id,thread_root_id,actor_id)
);
