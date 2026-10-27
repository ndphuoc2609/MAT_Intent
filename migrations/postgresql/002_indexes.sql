CREATE INDEX requests_scope_actor_status
  ON requests(page_id, post_id, thread_root_id, actor_id, status);
CREATE INDEX pending_actor_status
  ON pending(actor_id, status, request_id);
CREATE INDEX events_comment_order
  ON events(comment_id, seq DESC);
CREATE INDEX outbox_status_created
  ON outbox(status, created_at DESC);
CREATE INDEX handoffs_scope_active
  ON handoffs(request_id, released_at);
