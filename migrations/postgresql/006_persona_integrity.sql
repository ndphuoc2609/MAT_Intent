ALTER TABLE persona_fact_approvals ADD CONSTRAINT persona_approval_fact_unique UNIQUE (approval_id, fact_id);
ALTER TABLE release_facts ADD CONSTRAINT release_fact_matching_approval FOREIGN KEY (approval_id, fact_id)
  REFERENCES persona_fact_approvals (approval_id, fact_id);
DROP RULE persona_release_facts_insert_guard ON release_facts;
DROP RULE persona_release_facts_update_guard ON release_facts;
DROP RULE persona_release_facts_delete_guard ON release_facts;
DROP RULE persona_utterances_update_guard ON persona_utterances;
DROP RULE persona_utterances_delete_guard ON persona_utterances;
DROP RULE persona_hooks_update_guard ON persona_hooks;
DROP RULE persona_hooks_delete_guard ON persona_hooks;
CREATE FUNCTION persona_guard_release_content() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' AND EXISTS (SELECT 1 FROM persona_releases WHERE release_id=OLD.release_id AND status<>'DRAFT') THEN
    RAISE EXCEPTION 'published release is immutable';
  END IF;
  IF TG_OP <> 'DELETE' AND EXISTS (SELECT 1 FROM persona_releases WHERE release_id=NEW.release_id AND status<>'DRAFT') THEN
    RAISE EXCEPTION 'published release is immutable';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER persona_release_facts_immutable BEFORE INSERT OR UPDATE OR DELETE ON release_facts FOR EACH ROW EXECUTE FUNCTION persona_guard_release_content();
CREATE TRIGGER persona_utterances_immutable BEFORE INSERT OR UPDATE OR DELETE ON persona_utterances FOR EACH ROW EXECUTE FUNCTION persona_guard_release_content();
CREATE TRIGGER persona_hooks_immutable BEFORE INSERT OR UPDATE OR DELETE ON persona_hooks FOR EACH ROW EXECUTE FUNCTION persona_guard_release_content();
CREATE FUNCTION persona_guard_root_analysis() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'root analysis is immutable'; END $$;
CREATE TRIGGER persona_root_analysis_immutable BEFORE UPDATE OR DELETE ON persona_root_analyses FOR EACH ROW EXECUTE FUNCTION persona_guard_root_analysis();
