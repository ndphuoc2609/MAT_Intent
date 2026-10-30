CREATE TABLE IF NOT EXISTS lead_draft_fields (
 page_id TEXT NOT NULL, actor_id TEXT NOT NULL, post_id TEXT NOT NULL,
 field TEXT NOT NULL, value TEXT, evidence TEXT, source_message_id TEXT,
 updated_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ASKED','ANSWERED','DECLINED')),
 PRIMARY KEY(page_id,actor_id,post_id,field)
);
