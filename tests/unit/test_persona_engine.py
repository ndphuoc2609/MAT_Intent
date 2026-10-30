import unittest
from unittest.mock import patch
import hashlib
import json

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


def _snapshot_with_district():
    snapshot = _snapshot()
    fact = {"fact_id": "f2", "persona_uuid": "p1", "release_id": "r1",
            "key": "location.base.district", "value": "Hải Châu", "tellable": True, "approval": "approved",
            "source_ref": "fixture:nam_creta:district"}
    utterance = {
        "utterance_id": "u-district", "persona_uuid": "p1", "release_id": "r1",
        "text": "Mình ở quận Hải Châu.", "purpose": "answer_persona",
        "topic": "residence", "required_fact_ids": ["f2"],
        "claims": [{"text": "quận Hải Châu", "required_fact_ids": ["f2"],
                    "evidence": [{"fact_id": "f2", "source_ref": fact["source_ref"]}]}],
        "approval": "approved",
    }
    utterance["checksum"] = utterance_checksum(utterance)
    snapshot["facts"].append(fact)
    snapshot["utterances"].append(utterance)
    return snapshot


def _snapshot_with_legacy_purchase_keys():
    snapshot = _snapshot()
    for fact_id, key, value, text, claim_text in (
        ("f-year", "purchase.year", 2020, "Mình mua xe năm 2020.", "năm 2020"),
        ("f-finance", "purchase.finance_method", "trả thẳng", "Mình mua xe trả thẳng.", "trả thẳng"),
    ):
        source_ref = f"fixture:nam_creta:{key}"
        snapshot["facts"].append({"fact_id": fact_id, "persona_uuid": "p1", "release_id": "r1",
            "key": key, "value": value, "tellable": True, "approval": "approved", "source_ref": source_ref})
        utterance = {"utterance_id": f"u-{fact_id}", "persona_uuid": "p1", "release_id": "r1",
            "text": text, "purpose": "answer_persona", "topic": "purchase_finance",
            "required_fact_ids": [fact_id],
            "claims": [{"text": claim_text, "required_fact_ids": [fact_id],
                "evidence": [{"fact_id": fact_id, "source_ref": source_ref}]}],
            "approval": "approved"}
        utterance["checksum"] = utterance_checksum(utterance)
        snapshot["utterances"].append(utterance)
    return snapshot


def _social_semantic(text):
    return {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
            "requests": [{"topic": "social", "desired_outcome": "ack", "polarity": "positive"}],
            "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}], "uncertainties": []}


class PersonaEngineTests(unittest.TestCase):
    def test_valid_edit_replaces_unclaimed_persona_reply_and_drain_cannot_resend_old_text(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot_with_district())
        event, postcard, semantic = self._input()
        with patch.object(engine, "_dispatch", side_effect=RuntimeError("crash before dispatch")):
            queued = engine.process(event, postcard, semantic)
        self.assertIn("ERROR_ROLLBACK", queued["routing"])

        edited = dict(event, event_type="edit", content_version=2,
                      event_dedupe_key="comment-1:edit:2", text="Bạn ở quận nào?")
        result = engine.process(edited, postcard, _social_semantic(edited["text"]))

        self.assertEqual(result["response_text"], "Mình ở quận Hải Châu.")
        self.assertTrue(result["outbound_permission"])
        payload = engine.store.get_outbox("comment-1:reply")["payload"]
        token = hashlib.sha256(json.dumps(edited, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        self.assertEqual(payload["persona_event_token"], token)
        self.assertEqual(payload["text"], "Mình ở quận Hải Châu.")
        self.assertEqual(len(engine.sender.sent), 1)
        engine.drain_intended()
        self.assertEqual(len(engine.sender.sent), 1)

    def test_legacy_persona_intent_is_upgraded_only_with_one_active_create_event(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot())
        with patch.object(engine, "_dispatch", side_effect=RuntimeError("crash before dispatch")):
            queued = engine.process(*self._input())
        self.assertIn("ERROR_ROLLBACK", queued["routing"])
        row = engine.store.get_outbox("comment-1:reply")
        row["payload"].pop("persona_event_token")
        engine.store.conn.execute("UPDATE outbox SET payload=? WHERE dedupe_key=?",
                                  (json.dumps(row["payload"]), "comment-1:reply"))
        engine.store.conn.commit()

        self.assertEqual(engine.drain_intended(), [("comment-1:reply", "SENT")])
        self.assertTrue(engine.store.get_outbox("comment-1:reply")["payload"].get("persona_event_token"))
        self.assertTrue(engine.sender.receiver.has_effect("comment-1:reply"))

    def test_legacy_persona_intent_with_edit_event_is_sent_to_review(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot())
        event, _, _ = self._input()
        with patch.object(engine, "_dispatch", side_effect=RuntimeError("crash before dispatch")):
            queued = engine.process(*self._input())
        self.assertIn("ERROR_ROLLBACK", queued["routing"])
        row = engine.store.get_outbox("comment-1:reply")
        row["payload"].pop("persona_event_token")
        engine.store.conn.execute("UPDATE outbox SET payload=? WHERE dedupe_key=?",
                                  (json.dumps(row["payload"]), "comment-1:reply"))
        edit = dict(event, event_type="edit", content_version=2,
                    event_dedupe_key="comment-1:edit:2", text="Bạn ở quận nào?")
        engine.store.insert_event(edit["event_dedupe_key"], edit, edit["actor_id"])
        engine.store.conn.commit()

        self.assertEqual(engine.drain_intended(), [("comment-1:reply", "NEEDS_REVIEW")])
        self.assertEqual(engine.store.get_outbox("comment-1:reply")["status"], "NEEDS_REVIEW")
        self.assertFalse(engine.sender.receiver.has_effect("comment-1:reply"))

    def test_legacy_persona_intent_after_retraction_and_late_create_needs_review(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot())
        event, _, _ = self._input()
        with patch.object(engine, "_dispatch", side_effect=RuntimeError("crash before dispatch")):
            queued = engine.process(*self._input())
        self.assertIn("ERROR_ROLLBACK", queued["routing"])
        row = engine.store.get_outbox("comment-1:reply")
        row["payload"].pop("persona_event_token")
        engine.store.conn.execute("UPDATE outbox SET payload=? WHERE dedupe_key=?",
                                  (json.dumps(row["payload"]), "comment-1:reply"))
        deleted = dict(event, event_type="delete", content_version=2,
                       event_dedupe_key="comment-1:delete:2")
        engine.store.insert_event(deleted["event_dedupe_key"], deleted, deleted["actor_id"])
        engine.store.conn.execute("UPDATE events SET retracted=1 WHERE comment_id=?", (event["comment_id"],))
        late_create = dict(event, content_version=3, event_dedupe_key="comment-1:3")
        engine.store.insert_event(late_create["event_dedupe_key"], late_create, late_create["actor_id"])
        engine.store.conn.commit()

        self.assertEqual(engine.drain_intended(), [("comment-1:reply", "NEEDS_REVIEW")])
        self.assertFalse(engine.sender.receiver.has_effect("comment-1:reply"))

    def test_existing_persona_purchase_year_and_finance_method_facts_still_answer(self):
        for index, (text, answer) in enumerate((
            ("Bạn mua xe năm nào?", "năm 2020"),
            ("Bạn mua xe khi nào?", "năm 2020"),
            ("Bạn mua xe trả thẳng không?", "trả thẳng"),
        )):
            with self.subTest(text=text):
                engine = make_engine()
                engine.persona_store = _PersonaStore(_snapshot_with_legacy_purchase_keys())
                event, postcard, _ = self._input()
                event.update(comment_id=f"legacy-{index}", event_dedupe_key=f"legacy-{index}:1", text=text)
                result = engine.process(event, postcard, _social_semantic(text))
                self.assertIn(answer, result.get("response_text") or "", result)
                self.assertTrue(result["outbound_permission"], result)

    def test_month_question_does_not_fall_back_to_year_only_fact(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot_with_legacy_purchase_keys())
        event, postcard, _ = self._input()
        event.update(comment_id="month-question", event_dedupe_key="month-question:1",
                     text="Bạn mua xe tháng mấy?")
        result = engine.process(event, postcard, _social_semantic(event["text"]))
        self.assertFalse(result["outbound_permission"])
        self.assertNotIn("năm 2020", result.get("response_text") or "")

    def test_persona_recovery_blocks_after_comment_edit_or_delete(self):
        for kind in ("edit", "delete", "delete_edit"):
            with self.subTest(kind=kind):
                engine = make_engine()
                engine.persona_store = _PersonaStore(_snapshot())
                event, postcard, semantic = self._input()
                with patch.object(engine, "_dispatch", side_effect=RuntimeError("crash")):
                    engine.process(event, postcard, semantic)
                event_type = "delete" if kind == "delete_edit" else kind
                changed = dict(event, event_type=event_type, content_version=2,
                               event_dedupe_key="comment-1:" + event_type, text="Tôi không hỏi nữa")
                engine.process(changed, postcard, None)
                if kind == "delete_edit":
                    engine.process(dict(changed, event_type="edit", event_dedupe_key="comment-1:edit"), postcard, None)
                engine.drain_intended()
                self.assertFalse(engine.sender.receiver.has_effect("comment-1:reply"))

    def test_persona_answer_obeys_auto_actions(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot())
        with patch("app.engine.AUTO_ACTIONS", set()):
            result = engine.process(*self._input())
        self.assertEqual(result["sends"], [])
        self.assertFalse(result["outbound_permission"])

    def test_persona_recovery_rechecks_auto_actions(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot())
        with patch.object(engine, "_dispatch", side_effect=RuntimeError("crash")):
            engine.process(*self._input())
        with patch("app.engine.AUTO_ACTIONS", set()):
            self.assertEqual(engine.drain_intended()[0][1], "BLOCKED")
        self.assertFalse(engine.sender.receiver.has_effect("comment-1:reply"))

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
