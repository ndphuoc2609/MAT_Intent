CREATE TABLE IF NOT EXISTS persona_customer_facts (
 page_id TEXT NOT NULL, post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL,
 actor_id TEXT NOT NULL, comment_id TEXT NOT NULL, predicate TEXT NOT NULL,
 value TEXT NOT NULL, evidence TEXT NOT NULL, certainty TEXT NOT NULL
 CHECK(certainty IN ('explicit','inferred','unknown')), created_at TEXT NOT NULL,
 PRIMARY KEY(page_id,post_id,thread_root_id,actor_id,comment_id,predicate)
);
CREATE INDEX IF NOT EXISTS persona_customer_facts_scope ON persona_customer_facts(page_id,post_id,thread_root_id,actor_id,created_at);
