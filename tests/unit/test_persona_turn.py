import io
import json
import os
import unittest
from unittest.mock import patch

from app.adapters import luna
from app.persona_turn import parse_persona_turn, persona_turn_model_schema


def semantic_v2(text="Bạn đang ở đâu?"):
    return {
        "schema_version": "2", "sentiment": "NEU", "sarcasm": "absent",
        "scope": "in_scope", "requests": [{"topic": "social", "desired_outcome": "question",
                                               "polarity": "positive"}],
        "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}],
        "uncertainties": [], "intent_labels": ["social"], "category": "social", "tags": [],
    }


def reply():
    return {
        "reply_text": "Mình đang ở Đà Nẵng.", "fact_ids": ["f1"], "memory_ids": [],
        "claims": [{"text": "Mình đang ở Đà Nẵng", "subject": "persona",
                    "predicate": "location.base.province", "fact_ids": ["f1"],
                    "memory_ids": [], "message_ids": []}],
        "asks_question": False, "question": None,
    }


def peer_interpretation():
    return {
        "speaker_intent": "social", "address_term": "bạn", "persona_topic": "residence",
        "asks_persona_question": True, "evidence_excerpt": "Bạn đang ở đâu?",
        "claims": [{"predicate": "purchase_intent", "value": "exploring",
                    "evidence_excerpt": "Mình cũng đang tính mua xe"}],
    }


def envelope(**overrides):
    value = {
        "schema_version": "persona_turn_v1", "semantic": semantic_v2(),
        "reply": reply(),
        "diagnostics": {"intent_confidence": [{"topic": "social", "confidence": 0.73,
                                                "evidence_excerpt": "Bạn đang ở đâu?"}]},
        "peer_interpretation": peer_interpretation(),
    }
    value.update(overrides)
    return value


class PersonaTurnEnvelopeTests(unittest.TestCase):
    def test_schema_is_a_strict_envelope_around_semantic_v2(self):
        schema = persona_turn_model_schema()
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["semantic"]["properties"]["schema_version"]["enum"], ["2"])
        self.assertEqual(schema["properties"]["reply"]["anyOf"][0]["additionalProperties"], False)
        self.assertEqual(schema["properties"]["diagnostics"]["anyOf"][0]["additionalProperties"], False)
        self.assertEqual(schema["properties"]["peer_interpretation"]["anyOf"][0]["additionalProperties"], False)

    def test_parses_all_four_outputs_without_mixing_them(self):
        result = parse_persona_turn(envelope(), current_text="Bạn đang ở đâu? Mình cũng đang tính mua xe")
        self.assertEqual(result["semantic"]["schema_version"], "2")
        self.assertEqual(result["reply_candidate"], reply())
        self.assertEqual(result["diagnostics"]["intent_confidence"][0]["confidence"], 0.73)
        self.assertEqual(result["peer_signals"]["speaker_intent"], "social")
        self.assertEqual(result["peer_signals"]["claims"][0]["predicate"], "purchase_intent")

    def test_malformed_reply_and_diagnostics_do_not_discard_valid_semantic(self):
        bad_reply = reply()
        bad_reply["fact_ids"] = ["unreferenced"]
        result = parse_persona_turn(envelope(reply=bad_reply,
            diagnostics={"intent_confidence": [{"topic": "social", "confidence": 1.2,
                                                "evidence_excerpt": "Bạn đang ở đâu?"}]}),
            current_text="Bạn đang ở đâu?")
        self.assertEqual(result["semantic"], semantic_v2())
        self.assertIsNone(result["reply_candidate"])
        self.assertIsNone(result["diagnostics"])

    def test_invalid_peer_interpretation_is_omitted_independently(self):
        peer = peer_interpretation()
        peer["claims"][0]["predicate"] = "persona_secret_field"
        result = parse_persona_turn(envelope(peer_interpretation=peer),
                                    current_text="Bạn đang ở đâu? Mình cũng đang tính mua xe")
        self.assertIsNone(result["peer_signals"])
        self.assertEqual(result["semantic"]["schema_version"], "2")

    def test_malformed_nested_optional_values_are_dropped_without_crashing(self):
        bad_reply = reply()
        bad_reply["claims"] = [{"text": "Mình đang ở Đà Nẵng", "subject": "persona",
            "predicate": [], "fact_ids": ["f1"], "memory_ids": [], "message_ids": []}]
        bad_diagnostics = {"intent_confidence": [{"topic": [], "confidence": 0.5,
                                                   "evidence_excerpt": "Bạn đang ở đâu?"}]}
        bad_peer = peer_interpretation()
        bad_peer["claims"][0]["predicate"] = []
        result = parse_persona_turn(envelope(reply=bad_reply, diagnostics=bad_diagnostics,
            peer_interpretation=bad_peer), current_text="Bạn đang ở đâu? Mình cũng đang tính mua xe")
        self.assertEqual(result["semantic"]["schema_version"], "2")
        self.assertIsNone(result["reply_candidate"])
        self.assertIsNone(result["diagnostics"])
        self.assertIsNone(result["peer_signals"])

    def test_extreme_confidence_integer_drops_diagnostics_without_losing_semantic(self):
        diagnostics = {"intent_confidence": [{"topic": "social", "confidence": 10 ** 1000,
                                               "evidence_excerpt": "Bạn đang ở đâu?"}]}
        result = parse_persona_turn(envelope(diagnostics=diagnostics), current_text="Bạn đang ở đâu?")
        self.assertEqual(result["semantic"]["schema_version"], "2")
        self.assertIsNone(result["diagnostics"])

    def test_rejects_bad_outer_envelope_or_missing_semantic(self):
        with self.assertRaises(ValueError):
            parse_persona_turn({"schema_version": "semantic_v2", "semantic": semantic_v2()})
        with self.assertRaises(ValueError):
            parse_persona_turn({"schema_version": "persona_turn_v1", "semantic": None})


class LunaPersonaTurnAdapterTests(unittest.TestCase):
    def test_persona_prompt_is_short_truthful_and_does_not_fake_waiting(self):
        prompt = luna.PERSONA_TURN_SYSTEM.lower().replace("\n", " ")
        self.assertIn("persona_turn_v1", prompt)
        self.assertIn("tối đa hai câu", prompt)
        self.assertIn("không nhận mình là người thật", prompt)
        self.assertIn("không thêm độ trễ", prompt)
        self.assertIn("không tự bịa cảm xúc", prompt)
        self.assertIn("trả lời đúng nhu cầu hiện tại", prompt)
        self.assertIn("không nhắc lại xe đang đi hoặc dữ kiện persona khác nếu không liên quan", prompt)
        self.assertIn("không nói với khách về \"hồ sơ\", \"fact\", hay việc hệ thống thiếu trường dữ liệu", prompt)
        self.assertIn("hỏi làm rõ có ý nghĩa", prompt)
        self.assertIn("nếu không có fallback phù hợp thì không gửi", prompt)
        self.assertNotIn("bản nháp", prompt)
        self.assertIn("phải dẫn cả memory_id tương ứng", prompt)
        self.assertIn("không tự kể lại dữ kiện trong memory nếu khách không hỏi", prompt)
        self.assertNotIn("chỉ trả json đúng schema v2", prompt)

    def test_generated_reply_prompt_uses_proposals_for_missing_story_facts(self):
        prompt = luna.PERSONA_GENERATED_REPLY_SYSTEM.lower().replace("\n", " ")
        self.assertIn("nếu khách hỏi về trải nghiệm hoặc kỷ niệm", prompt)
        self.assertIn("gen.trip", prompt)
        self.assertIn("gen.memory", prompt)
        self.assertIn("reply.fact_ids và ít nhất một claims[].fact_ids đều phải chứa đúng proposal_id", prompt)
        self.assertIn("proposal.value phải xuất hiện nguyên văn trong câu claim và reply_text", prompt)
        self.assertIn("claim cho từng fact trong câu, cùng text và fact_id tương ứng", prompt)
        self.assertIn("không lấy chi tiết mới từ lời khách", prompt)
        self.assertIn("nếu không thể tạo proposal nhất quán thì nói chưa có thông tin", prompt)

    def test_semantic_v2_prompt_states_ordered_topics_and_social_ack_shape(self):
        prompt = luna.SYSTEM_V2
        self.assertIn("The ordered unique topics in requests MUST exactly equal intent_labels", prompt)
        self.assertIn("When requests is empty, intent_labels must be [] and category must be null", prompt)
        self.assertIn("Every SOCIAL_ACK must include a social request", prompt)
        self.assertIn('"desired_outcome":"conversation"', prompt)

    def test_persona_context_uses_one_strict_envelope_call_and_keeps_legacy_fields(self):
        body = json.dumps(envelope())
        comment = "Bạn đang ở đâu? Mình cũng đang tính mua xe"
        context = json.dumps({"format": "mat_intent_context_v1", "channel": "private",
            "postcard": {"id": "fixture-post", "text": "post"},
            "messages": [{"role": "current", "message_id": "m1", "actor_id": "c1", "text": comment}],
            "recent": [], "active_requests": [], "pending": [], "contact_blocks": [],
            "prior_customer_turns": [], "recent_bot_replies": []})
        response = {"model": "fixture/luna", "usage": {"prompt_tokens": 700,
            "completion_tokens": 300, "cost": 0.002}, "choices": [{"finish_reason": "stop",
            "message": {"content": body}}]}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(luna.urllib.request, "build_opener") as opener, \
             patch.object(luna.time, "sleep"):
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = luna.call_luna("post", comment, context=context, max_tokens=1200,
                                    persona_context={"facts": [{"fact_id": "f1"}],
                                                     "memories": [], "voice": {"tone": "warm"},
                                                     "voice_guide": {"few_shots": []},
                                                     "private_notes": ["must not reach model"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["semantic"]["schema_version"], "2")
        self.assertEqual(result["reply_candidate"]["reply_text"], "Mình đang ở Đà Nẵng.")
        self.assertEqual(result["diagnostics"]["intent_confidence"][0]["confidence"], 0.73)
        self.assertEqual(result["peer_signals"]["speaker_intent"], "social")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["cost_usd_est"], 0.002)
        self.assertIn("raw_text", result)
        request = opener.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertGreaterEqual(payload["max_tokens"], 2400)
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "persona_turn_v1")
        user = json.loads(payload["messages"][1]["content"])
        self.assertEqual(user["persona_context"], {"facts": [{"fact_id": "f1"}],
            "memories": [], "voice": {"tone": "warm"}, "voice_guide": {"few_shots": []}})
        self.assertEqual(user["channel"], "private")
        self.assertNotIn("private_notes", payload["messages"][1]["content"])

    def test_bad_reply_or_diagnostics_keeps_valid_semantic_in_the_same_call(self):
        bad_reply = reply()
        bad_reply["fact_ids"] = ["not-in-claims"]
        bad = envelope(reply=bad_reply, diagnostics={"intent_confidence": [
            {"topic": "social", "confidence": 1.2, "evidence_excerpt": "Bạn đang ở đâu?"}]})
        response = {"usage": {"cost": 0.001}, "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(bad)}}]}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(luna.urllib.request, "build_opener") as opener, \
             patch.object(luna.time, "sleep"):
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = luna.call_luna("post", "Bạn đang ở đâu? Mình cũng đang tính mua xe",
                context="{}", persona_context={"facts": [], "memories": [], "voice_guide": {}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["semantic"]["schema_version"], "2")
        self.assertIsNone(result["reply_candidate"])
        self.assertIsNone(result["diagnostics"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(opener.return_value.open.call_count, 1)

    def test_legacy_call_keeps_semantic_only_schema_and_token_budget(self):
        semantic = semantic_v2()
        response = {"usage": {"cost": 0.001}, "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(semantic)}}]}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(luna.urllib.request, "build_opener") as opener, \
             patch.object(luna.time, "sleep"):
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = luna.call_luna("post", "comment", max_tokens=1200, schema_version="2")
        self.assertTrue(result["ok"])
        self.assertNotIn("reply_candidate", result)
        payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(payload["max_tokens"], 1200)
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "mat_intent_semantic_v2")

    def test_two_phase_adapter_uses_separate_strict_contracts(self):
        comment = "Bạn đang ở đâu?"
        analysis = {"schema_version": "persona_analysis_v1", "semantic": semantic_v2(comment),
            "needs": {"topics": ["residence"], "keys": ["location.base.province"]},
            "diagnostics": {"intent_confidence": [{"topic": "social", "confidence": 0.8,
                "evidence_excerpt": comment}]}, "peer_interpretation": peer_interpretation()}
        analysis["peer_interpretation"]["evidence_excerpt"] = comment
        analysis["peer_interpretation"]["claims"] = []
        response = {"usage": {"cost": 0.001}, "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(analysis)}}]}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(luna.urllib.request, "build_opener") as opener, \
             patch.object(luna.time, "sleep"):
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = luna.call_luna("post", comment, context="{}", persona_phase="analysis")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["needs"]["keys"], ["location.base.province"])
        self.assertNotIn("reply_candidate", result)
        payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "persona_analysis_v1")
        self.assertNotIn("persona_context", payload["messages"][1]["content"])

    def test_reply_phase_allows_only_declared_proposal_references(self):
        comment = "Bạn kể chuyện cuối tuần đi"
        candidate = dict(reply_text="Cuối tuần mình hay đi Đà Lạt.", fact_ids=["new1"], memory_ids=[],
            claims=[dict(text="Cuối tuần mình hay đi Đà Lạt.", subject="persona", predicate="gen.trip",
                fact_ids=["new1"], memory_ids=[], message_ids=[])], asks_question=False, question=None,
            fact_proposals=[dict(proposal_id="new1", namespace="trip", slug="weekend_getaway",
                value="Cuối tuần mình hay đi Đà Lạt", precision="approximate")])
        value = {"schema_version": "persona_turn_v1", "semantic": semantic_v2(comment),
            "reply": candidate, "diagnostics": None, "peer_interpretation": None}
        response = {"usage": {"cost": 0.002}, "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(value, ensure_ascii=False)}}]}
        context = {"facts": [{"fact_id": "f1", "key": "identity.name", "value": "Nam",
                              "source_ref": "content_ai_fac.public.personas:private-row-id:identity.name"}],
                   "memories": [], "voice_guide": {},
                   "persona_authored_post": "Cuối tuần mình về Đà Lạt.",
                   "persona_authored_post_fact_ids": ["post-fact-1"],
                   "phase_one_analysis": {"semantic": semantic_v2(comment)}}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(luna.urllib.request, "build_opener") as opener, \
             patch.object(luna.time, "sleep"):
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = luna.call_luna("post", comment, context="{}", persona_context=context,
                                    persona_phase="reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["fact_proposals"][0]["proposal_id"], "new1")
        payload = json.loads(opener.return_value.open.call_args.args[0].data)
        reply_schema = payload["response_format"]["json_schema"]["schema"]["properties"]["reply"]["anyOf"][0]
        self.assertIn("fact_proposals", reply_schema["properties"])
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "persona_turn_v1")
        supplied = json.loads(payload["messages"][1]["content"])["persona_context"]
        self.assertNotIn("private-row-id", json.dumps(supplied))
        self.assertNotIn("source_ref", supplied["facts"][0])
        self.assertEqual(supplied["persona_authored_post"], context["persona_authored_post"])
        self.assertEqual(supplied["persona_authored_post_fact_ids"], ["post-fact-1"])
        prompt = payload["messages"][0]["content"].lower()
        self.assertIn("persona đã đăng trước đây", prompt)
        self.assertIn("không phải bằng chứng về persona", prompt)

    def test_post_contract_requires_claims_and_declared_proposals(self):
        from app import persona_turn
        schema_fn = getattr(persona_turn, "persona_post_model_schema", None)
        parser = getattr(persona_turn, "parse_persona_post", None)
        self.assertTrue(callable(schema_fn), "persona-authored posts need their own strict schema")
        self.assertTrue(callable(parser), "persona-authored posts need a strict parser")
        value = {"schema_version": "persona_post_v1",
            "post_text": "Cuối tuần mình thường đi Đà Lạt.",
            "fact_ids": ["new1"],
            "claims": [{"text": "Cuối tuần mình thường đi Đà Lạt.", "subject": "persona",
                "predicate": "gen.trip", "fact_ids": ["new1"], "memory_ids": [], "message_ids": []}],
            "fact_proposals": [{"proposal_id": "new1", "namespace": "trip",
                "slug": "weekend_getaway", "value": "Cuối tuần mình thường đi Đà Lạt",
                "precision": "approximate"}]}
        result = parser(value, persona_predicates={"identity.name"})
        self.assertEqual(result["post_text"], value["post_text"])
        self.assertEqual(result["fact_proposals"], value["fact_proposals"])
        self.assertEqual(schema_fn()["properties"]["post_text"]["maxLength"], 3000)
        too_long = dict(value, post_text=value["post_text"] + ("x" * 3000))
        self.assertIsNone(parser(too_long, persona_predicates={"identity.name"}))
        wrong_predicate = json.loads(json.dumps(value))
        wrong_predicate["claims"][0]["predicate"] = "gen.car"
        self.assertIsNone(parser(wrong_predicate, persona_predicates={"identity.name"}))
        value["fact_ids"] = ["new2"]
        self.assertIsNone(parser(value, persona_predicates={"identity.name"}))


class LunaPersonaPostAdapterTests(unittest.TestCase):
    def test_post_phase_uses_strict_persona_post_schema_and_grounding_prompt(self):
        from app import persona_turn
        parser = getattr(persona_turn, "parse_persona_post", None)
        self.assertTrue(callable(parser), "post parser must exist before provider call")
        fact = {"fact_id": "f1", "key": "identity.name", "value": "Nam"}
        post = {"schema_version": "persona_post_v1", "post_text": "Mình là Nam.",
            "fact_ids": ["f1"], "claims": [{"text": "Mình là Nam.", "subject": "persona",
                "predicate": "identity.name", "fact_ids": ["f1"], "memory_ids": [], "message_ids": []}],
            "fact_proposals": []}
        response = {"usage": {"cost": 0.001}, "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(post, ensure_ascii=False)}}]}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(luna.urllib.request, "build_opener") as opener, \
             patch.object(luna.time, "sleep"):
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = luna.call_luna("post brief", "Viết về chuyến đi cuối tuần", context="{}",
                persona_context={"facts": [fact], "memories": [], "voice_guide": {}},
                persona_phase="post")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["post_candidate"]["post_text"], "Mình là Nam.")
        payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "persona_post_v1")
        schema = payload["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["required"], ["schema_version", "post_text", "fact_ids", "claims", "fact_proposals"])
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("fact_proposals", schema["properties"])
        prompt = payload["messages"][0]["content"].lower()
        self.assertIn("fact_proposals", prompt)
        self.assertIn("được cấp", prompt)
        self.assertEqual(payload["max_tokens"], 3200)


if __name__ == "__main__":
    unittest.main()

class PersonaAnalysisContractTests(unittest.TestCase):
    def test_two_phase_schemas_split_needs_from_reply_and_proposals(self):
        from app.persona_turn import persona_analysis_model_schema
        analysis = persona_analysis_model_schema()
        reply = persona_turn_model_schema(proposals=True)
        self.assertNotIn('reply', analysis['properties'])
        self.assertIn('needs', analysis['properties'])
        self.assertIn('fact_proposals', reply['properties']['reply']['anyOf'][0]['properties'])
        self.assertIn('gen.trip', reply['properties']['reply']['anyOf'][0]['properties']['claims']['items']['properties']['predicate']['enum'])
        self.assertFalse(reply['properties']['reply']['anyOf'][0]['properties']['fact_proposals']['items']['additionalProperties'])

    def test_parses_needs_and_phase_two_proposal_without_mixing_outputs(self):
        from app.persona_turn import parse_persona_analysis
        value = {'schema_version': 'persona_analysis_v1', 'semantic': semantic_v2(),
            'needs': {'topics': ['vehicle_current', 'trip'], 'keys': ['vehicle.current.model']},
            'diagnostics': None, 'peer_interpretation': None}
        self.assertEqual(parse_persona_analysis(value, current_text='Bạn đi xe gì?')['needs']['topics'],
                         ['vehicle_current', 'trip'])

        candidate = reply()
        candidate['fact_proposals'] = [{'proposal_id': 'new1', 'namespace': 'trip',
            'slug': 'weekend_route', 'value': 'Cuối tuần mình thường về quê', 'precision': 'approximate'}]
        envelope_value = {'schema_version': 'persona_turn_v1', 'semantic': semantic_v2(),
            'reply': candidate, 'diagnostics': envelope()['diagnostics'],
            'peer_interpretation': peer_interpretation()}
        parsed = parse_persona_turn(envelope_value, current_text='Bạn kể chuyện đi chơi? ', proposals=True)
        self.assertEqual(parsed['fact_proposals'][0]['proposal_id'], 'new1')

    def test_phase_one_bad_needs_is_rejected_as_a_phase_failure(self):
        from app.persona_turn import parse_persona_analysis
        value = {'schema_version': 'persona_analysis_v1', 'semantic': semantic_v2(),
            'needs': {'topics': ['price'], 'keys': ['contact.phone']}, 'diagnostics': None,
            'peer_interpretation': None}
        with self.assertRaises(ValueError):
            parse_persona_analysis(value, current_text='Bạn kể chuyện đi chơi?')
