import unittest
from unittest.mock import patch

from app.harness import make_engine
from app.persona_reply import utterance_checksum


class _PersonaStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_binding_snapshot(self, page_id, post_id):
        return self.snapshot

    def revoked_fact_ids(self, persona_uuid, release_id):
        return []


def _snapshot():
    utterance = {
        "utterance_id": "u-residence", "persona_uuid": "p1", "release_id": "r1",
        "text": "Mình đang sống ở Đà Nẵng.", "purpose": "answer_persona",
        "topic": "residence", "required_fact_ids": ["f1"],
        "claims": [{"text": "sống ở Đà Nẵng", "required_fact_ids": ["f1"],
                    "evidence": [{"fact_id": "f1", "source_ref": "fixture:nam_creta:residence"}]}],
        "approval": "approved",
    }
    utterance["checksum"] = utterance_checksum(utterance)
    return {
        "persona_uuid": "p1", "release_id": "r1", "approved": True,
        "facts": [{"fact_id": "f1", "persona_uuid": "p1", "release_id": "r1",
                   "key": "location.base.province", "value": "Đà Nẵng", "tellable": True, "approval": "approved",
                   "source_ref": "fixture:nam_creta:residence"}],
        "utterances": [utterance],
    }


class PersonaEngineTests(unittest.TestCase):
    def test_district_question_does_not_answer_with_province(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot())
        event, postcard, semantic = self._input()
        event["text"] = "Bạn ở quận nào?"
        semantic["operations"][0]["evidence_excerpt"] = event["text"]
        result = engine.process(event, postcard, semantic)
        self.assertNotEqual(result.get("response_text"), "Mình đang sống ở Đà Nẵng.")
        self.assertFalse(result["sends"])

    def test_unrecognized_personal_question_requires_review(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot())
        event, postcard, semantic = self._input()
        event["text"] = "Quê bạn ở nơi nào?"
        semantic["operations"][0]["evidence_excerpt"] = event["text"]
        result = engine.process(event, postcard, semantic)
        self.assertIn("REVIEW_PERSONA_UNRESOLVED_QUERY", result["routing"])
        self.assertEqual(result["sends"], [])

    def _input(self):
        text = "Bạn ở đâu vậy?"
        semantic = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
                    "requests": [{"topic": "social", "desired_outcome": "ack", "polarity": "positive"}],
                    "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}], "uncertainties": []}
        event = {"page_id": "page-1", "post_id": "post-1", "thread_root_id": "root-1", "parent_id": "root-1",
                 "comment_id": "comment-1", "actor_id": "actor-1", "actor_role": "user", "event_type": "create",
                 "content_version": 1, "text": text, "event_dedupe_key": "comment-1:1"}
        return event, {"text": "Bài viết", "persona_binding": {"release_id": "r1"}}, semantic

    def test_persona_kill_switch_and_opt_out_before_dispatch(self):
        for kind in ("kill", "all", "public_reply"):
            with self.subTest(kind=kind):
                engine = make_engine()
                engine.persona_store = _PersonaStore(_snapshot())
                original = engine._dispatch
                def dispatch(*args, **kwargs):
                    if kind == "kill":
                        engine.kill_switch = True
                    else:
                        engine.store.block("page-1", "actor-1", kind)
                        engine.store.conn.commit()
                    return original(*args, **kwargs)
                with patch.object(engine, "_dispatch", side_effect=dispatch):
                    result = engine.process(*self._input())
                self.assertIn("BLOCKED_AT_DISPATCH", result["routing"])
                self.assertEqual(result["sends"], [])

    def test_persona_recovery_blocks_revoked_or_opted_out(self):
        for kind in ("revoked", "public_reply", "kill", "missing_scope", "missing_persona_metadata", "wrong_actor"):
            with self.subTest(kind=kind):
                engine = make_engine()
                engine.persona_store = _PersonaStore(_snapshot())
                with patch.object(engine, "_dispatch", side_effect=RuntimeError("crash")):
                    engine.process(*self._input())
                if kind == "revoked":
                    engine.persona_store.revoked_fact_ids = lambda *args: ["f1"]
                elif kind == "kill":
                    engine.kill_switch = True
                elif kind in ("missing_scope", "missing_persona_metadata", "wrong_actor"):
                    import json
                    row = engine.store.get_outbox("comment-1:reply")
                    payload = dict(row["payload"])
                    if kind == "wrong_actor":
                        payload["persona_scope"]["actor_id"] = "another-actor"
                    else:
                        payload.pop("persona_scope")
                    if kind == "missing_persona_metadata":
                        for key in ("persona_uuid", "release_id", "utterance_ids", "fact_ids"):
                            payload.pop(key)
                    engine.store.conn.execute("UPDATE outbox SET payload=? WHERE dedupe_key=?",
                                              (json.dumps(payload), "comment-1:reply"))
                    engine.store.conn.commit()
                else:
                    engine.store.block("page-1", "actor-1", kind)
                    engine.store.conn.commit()
                result = engine.drain_intended()
                self.assertEqual(result[0][1], "BLOCKED")
                self.assertFalse(engine.sender.receiver.has_effect("comment-1:reply"))

    def test_persona_dispatch_blocks_new_handoff_in_same_scope(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot())
        engine.store.insert_request({"id": "request-1", "page_id": "page-1", "post_id": "post-1",
            "thread_root_id": "root-1", "actor_id": "actor-1", "topic": "price", "status": "OPEN",
            "version": 1, "slots": {}, "source_events": [], "desired_outcomes": []})
        engine.store.conn.commit()
        original = engine._dispatch
        def dispatch(*args, **kwargs):
            engine.store.add_handoff("request-1", "staff", "review", 1, "2026-10-29T00:00:00+00:00")
            engine.store.conn.commit()
            return original(*args, **kwargs)
        with patch.object(engine, "_dispatch", side_effect=dispatch):
            result = engine.process(*self._input())
        self.assertIn("BLOCKED_AT_DISPATCH", result["routing"])
        self.assertEqual(result["sends"], [])

    def test_social_persona_question_uses_only_bound_approved_utterance(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot())
        text = "Bạn ở đâu vậy?"
        semantic = {
            "schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
            "scope": "in_scope",
            "requests": [{"topic": "social", "desired_outcome": "ack", "polarity": "positive"}],
            "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}],
            "uncertainties": [],
        }
        event = {"page_id": "page-1", "post_id": "post-1", "thread_root_id": "root-1",
                 "parent_id": "root-1", "comment_id": "comment-1", "actor_id": "actor-1",
                 "actor_role": "user", "event_type": "create", "content_version": 1,
                 "text": text, "event_dedupe_key": "comment-1:1"}
        result = engine.process(event, {"text": "Bài viết", "persona_binding": {"release_id": "r1"}}, semantic)
        self.assertEqual(result["response_text"], "Mình đang sống ở Đà Nẵng.")
        self.assertIn("answer_persona", result["routing"])
        self.assertTrue(result["outbound_permission"])
        self.assertEqual(result["response_plan"]["persona_query"]["evidence_message_id"], "comment-1")
        self.assertEqual(result["response_plan"]["persona_query"]["evidence_actor_id"], "actor-1")


if __name__ == "__main__":
    unittest.main()
