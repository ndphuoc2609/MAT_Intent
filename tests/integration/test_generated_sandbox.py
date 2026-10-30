import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def persona_semantic(text):
    return {"schema_version": "2", "sentiment": "NEU", "sarcasm": "absent",
        "scope": "in_scope", "requests": [{"topic": "social", "desired_outcome": "ack",
                                               "polarity": "positive"}],
        "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}],
        "uncertainties": [], "intent_labels": ["social"], "category": "social", "tags": []}


def generated_turn(text, persona_phase=None, persona_context=None):
    common = {"ok": True, "semantic": persona_semantic(text),
        "diagnostics": {"intent_confidence": [{"topic": "social", "confidence": 0.7,
                                                "evidence_excerpt": text}]},
        "peer_signals": {"speaker_intent": "social", "address_term": None,
            "persona_topic": None, "asks_persona_question": False, "claims": [],
            "evidence_excerpt": text},
        "raw_text": "{}", "attempts": 1, "usage": {"prompt_tokens": 400,
            "completion_tokens": 120, "total_tokens": 520}, "cost_usd_est": 0.001,
        "cost_unknown": False, "model_effective": "fixture/luna"}
    if persona_phase == "analysis":
        common["needs"] = {"topics": ["vehicle_current"], "keys": ["vehicle.current.model"]} \
            if "xe gì" in text.lower() else {"topics": [], "keys": []}
        common.pop("reply_candidate", None)
        return common
    fact = next((row for row in (persona_context or {}).get("facts", [])
                 if row.get("key") == "vehicle.current.model"), None)
    if "xe gì" in text.lower() and fact:
        reply_text = "Mình đang đi Hyundai Creta."
        candidate = {"reply_text": reply_text, "fact_ids": [fact["fact_id"]], "memory_ids": [],
            "claims": [{"text": reply_text, "subject": "persona", "predicate": "vehicle.current.model",
                "fact_ids": [fact["fact_id"]], "memory_ids": [], "message_ids": []}],
            "asks_question": False, "question": None, "fact_proposals": []}
    else:
        candidate = {"reply_text": "Chào bạn!", "fact_ids": [], "memory_ids": [],
            "claims": [], "asks_question": False, "question": None, "fact_proposals": []}
    common["reply_candidate"] = candidate
    return common


class GeneratedSandboxTests(unittest.TestCase):
    def setUp(self):
        from sandbox.server import Sandbox
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = str(Path(self.tmp.name) / "budget.jsonl")
        with patch.dict(os.environ, {"SANDBOX_JEV_SHADOW_ENABLED": "false"}):
            self.app = Sandbox(ledger_path=self.ledger,
                persona_db_path=str(Path(self.tmp.name) / "personas.sqlite3"))
        persona = next(p for p in self.app.personas() if p.get("slug") == "nam_creta")
        with patch.object(self.app, "generate_post", return_value="Bài viết cho hội thoại thử nghiệm."):
            self.state = self.app.create({"actor_id": "customer-1", "persona_id": persona["id"],
                                          "channel": "public"})
        self.conversation = self.app.conversations[self.state["id"]]

    def tearDown(self):
        for conversation in self.app.conversations.values():
            conversation["engine"].store.conn.close()
            conversation["engine"].sender.receiver.conn.close()
        if self.app.persona_conn:
            self.app.persona_conn.close()
        self.tmp.cleanup()

    def test_post_retries_incomplete_model_claims_once(self):
        repository = self.app.persona_repository()
        persona = next(p for p in self.app.personas() if p.get('slug') == 'nam_creta')
        snapshot = repository.get_release_snapshot(persona['id'], self.app.fixture_release_id)
        calls = []

        def call(_post, _prompt, **kwargs):
            calls.append(kwargs)
            if kwargs['persona_phase'] == 'analysis':
                return {'ok': True, 'needs': {'topics': [],
                    'keys': []}, 'attempts': 1, 'cost_usd_est': 0.001,
                    'cost_unknown': False}
            if not kwargs.get('repair_note'):
                return {'ok': False, 'error_code': 'MODEL_PERSONA_POST_SHAPE',
                    'attempts': 1, 'cost_usd_est': 0.001, 'cost_unknown': False}
            fact = next(f for f in kwargs['persona_context']['facts'] if f['key'] == 'vehicle.current.model')
            text = 'Mình đang đi Hyundai Creta.'
            return {'ok': True, 'attempts': 1, 'cost_usd_est': 0.001, 'cost_unknown': False,
                'post_candidate': {'post_text': text, 'fact_ids': [fact['fact_id']],
                'claims': [{'text': text, 'subject': 'persona', 'predicate': fact['key'],
                    'fact_ids': [fact['fact_id']], 'memory_ids': [], 'message_ids': []}],
                'fact_proposals': []}}

        metrics = {}
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only-key'}), \
             patch('sandbox.server.call_luna', side_effect=call):
            post = self.app.generate_post(persona, snapshot, _metrics=metrics, owner_id='retry-test')
        self.assertEqual(post, 'Mình đang đi Hyundai Creta.')
        self.assertEqual([c['persona_phase'] for c in calls], ['analysis', 'post', 'post'])
        self.assertIn('predicate đúng key', calls[-1]['repair_note'])
        self.assertEqual(metrics['attempts'], 3)
        self.assertAlmostEqual(metrics['cost_usd_est'], 0.003)

    def request(self, method, path, payload=None):
        from sandbox.server import Handler
        body = json.dumps(payload).encode() if payload is not None else b""
        request = (f"{method} {path} HTTP/1.0\r\nHost: 127.0.0.1:8766\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body

        class Connection:
            def __init__(self):
                self.output = io.BytesIO()

            def makefile(self, *_):
                return io.BytesIO(request)

            def sendall(self, value):
                self.output.write(value)

        connection = Connection()
        Handler(connection, ("127.0.0.1", 12345), SimpleNamespace(app=self.app))
        headers, response = connection.output.getvalue().split(b"\r\n\r\n", 1)
        return int(headers.split(b" ")[1]), json.loads(response)

    def test_model_led_tool_loop_persists_customer_evidence_and_interleaves_history(self):
        from app.customer_facts import list_customer_facts
        conversation = self.conversation
        conversation['model_led'] = True
        conversation['engine'].model_led_conversation = True
        first_text = 'Nhà mình 5 người, cuối tuần hay đi xa.'
        question = 'Nhà mình thường mang nhiều hành lý không ạ?'
        second_text = 'Cảm ơn bạn nhé.'

        def tool(action, payload):
            return {'ok': True, 'message': {'schema_version': 'conversation_tools_v1',
                'message': {'action': action, 'payload': payload}}, 'attempts': 1,
                'usage': {'prompt_tokens': 10, 'completion_tokens': 12},
                'cost_usd_est': 0.001, 'cost_unknown': False, 'model_effective': 'fixture/luna'}

        def plan(text, *, next_action='answer'):
            return tool('plan', {'intents': [{'kind': 'share_need', 'subject': 'customer',
                'normalized': text, 'evidence': text, 'context_source': 'current_comment',
                'certainty': 'explicit'}], 'priority_order': [0], 'unresolved': [],
                'objective': 'Trả lời đúng điều khách đang nói', 'next_action': next_action,
                'question': None, 'handoff_reason': None})

        def final(text, reply_text, *, asks=False, question_text=None):
            reply = {'reply_text': reply_text, 'fact_ids': [], 'memory_ids': [], 'claims': [],
                'product_claims': [], 'fact_proposals': [], 'asks_question': asks, 'question': {'kind': 'conversation', 'field': None,
                    'text': question_text} if asks else None}
            semantic = persona_semantic(text)
            semantic['operations'][0].update(request_ref=None, source_message_id=None)
            return tool('final', {'schema_version': 'persona_turn_v1',
                'semantic': semantic, 'reply': reply, 'diagnostics': None,
                'peer_interpretation': {'speaker_intent': 'social', 'address_term': None,
                    'persona_topic': None, 'asks_persona_question': False, 'claims': [],
                    'evidence_excerpt': text}})

        facts = {'facts': [
            {'predicate': 'family_size', 'value': '5', 'evidence_excerpt': '5 người',
             'subject': 'customer', 'certainty': 'explicit'},
            {'predicate': 'usage_need', 'value': 'cuối tuần hay đi xa',
             'evidence_excerpt': 'cuối tuần hay đi xa', 'subject': 'customer',
             'certainty': 'explicit'}]}
        first_calls = [plan(first_text), tool('record_customer_facts', facts),
                       final(first_text, question, asks=True, question_text=question)]
        seen_contexts = []

        def first_model(_context, _transcript, _schema, _timeout, **_kwargs):
            return first_calls.pop(0)

        with patch('app.adapters.luna.call_luna_tool', side_effect=first_model):
            first = self.app.turn(conversation, {'text': first_text})['turn']
        self.assertEqual(first.get('generation_validation'), 'OK', first)
        self.assertEqual(first['response_text'], question)
        self.assertTrue(first['outbound_permission'])
        self.assertEqual([row['predicate'] for row in list_customer_facts(
            conversation['engine'].persona_store, self.app.event(conversation, first_text,
                first['comment_id']))], ['family_size', 'usage_need'])

        def second_model(context, transcript, _schema, _timeout, **_kwargs):
            seen_contexts.append(context)
            if not transcript:
                return plan(second_text)
            if transcript[-1].get('tool_result', {}).get('status') == 'ACCEPTED':
                return tool('retrieve', {'kind': 'product_facts', 'topic': None, 'query': None,
                    'keys': ['list_price'], 'entities': {'vehicle': 'Creta', 'location': None, 'people': []},
                    'time_start': None, 'time_end': None, 'variant': None})
            self.assertEqual(transcript[-1]['tool_result']['status'], 'UNKNOWN')
            self.assertEqual(transcript[-1]['tool_result']['reason'], 'PRODUCT_SOURCE_UNAVAILABLE')
            return final(second_text, 'Có gì đâu, mình cùng trao đổi thôi.')

        with patch('app.adapters.luna.call_luna_tool', side_effect=second_model):
            second = self.app.turn(conversation, {'text': second_text})['turn']
        self.assertEqual(second['generation_validation'], 'OK')
        self.assertEqual(seen_contexts[0]['customer_facts'][0]['value'], '5')
        self.assertEqual([item['role'] for item in seen_contexts[0]['history']],
                         ['customer', 'assistant'])
        self.assertEqual(seen_contexts[0]['history'][0]['text'], first_text)
        self.assertEqual(seen_contexts[0]['history'][1]['text'], question)

    def test_multi_intent_interest_answers_persona_first_without_handoff(self):
        from app.customer_facts import list_customer_facts

        conversation = self.conversation
        conversation['model_led'] = True
        conversation['engine'].model_led_conversation = True
        text = 'Bác đi xe màu gì thế? Em cũng đang cân nhắc con này cho gia đình.'
        post_text = 'Mình đang chia sẻ trải nghiệm với Hyundai Creta.'
        conversation['post']['text'] = post_text
        conversation['post']['vehicles'] = ['Creta']
        conversation['postcard'].update(text=post_text, vehicles=['Creta'])
        conversation['postcard']['persona_authored_post']['text'] = post_text
        expected_question = 'Nhà mình thường đi mấy người ạ?'
        plan = {'intents': [
            {'kind': 'persona_question', 'subject': 'persona', 'normalized': 'màu xe persona',
             'evidence': 'Bác đi xe màu gì thế?', 'context_source': 'current_comment', 'certainty': 'explicit'},
            {'kind': 'buying_interest', 'subject': 'customer', 'normalized': 'đang cân nhắc Creta cho gia đình',
             'evidence': 'Em cũng đang cân nhắc con này cho gia đình.', 'context_source': 'post', 'certainty': 'explicit'},
            {'kind': 'share_need', 'subject': 'customer', 'normalized': 'đi xe cho gia đình',
             'evidence': 'cho gia đình', 'context_source': 'current_comment', 'certainty': 'explicit'}],
            'priority_order': [0, 1, 2], 'unresolved': [],
            'objective': 'Trả lời màu xe trước, ghi nhận nhu cầu gia đình và tiếp nối tự nhiên',
            'next_action': 'ask', 'question': expected_question, 'handoff_reason': None}

        def tool(action, payload):
            return {'ok': True, 'message': {'schema_version': 'conversation_tools_v1',
                'message': {'action': action, 'payload': payload}}, 'attempts': 1,
                'usage': {'prompt_tokens': 10, 'completion_tokens': 12},
                'cost_usd_est': 0.001, 'cost_unknown': False, 'model_effective': 'fixture/luna'}

        peer = {'speaker_intent': 'buying_interest', 'address_term': 'bác',
            'persona_topic': 'vehicle_current', 'asks_persona_question': True,
            'claims': [{'predicate': 'usage_need', 'value': 'cho gia đình',
                'evidence_excerpt': 'cho gia đình'}], 'evidence_excerpt': text}
        semantic = {'schema_version': '2', 'sentiment': 'NEU', 'sarcasm': 'absent',
            'scope': 'in_scope', 'requests': [{'topic': 'buy', 'desired_outcome': 'considering',
                'polarity': 'positive'}], 'operations': [{'op': 'START_REQUEST',
                'evidence_excerpt': text, 'request_ref': 'topic:buy', 'topic': 'buy',
                'request': None, 'slot': None, 'source_message_id': None}],
            'uncertainties': [], 'intent_labels': ['buy'], 'category': 'sales',
            'tags': ['multi_intent']}
        reply_text = 'Xe mình màu trắng. ' + expected_question
        final = {'schema_version': 'persona_turn_v1', 'semantic': semantic,
            'reply': {'reply_text': reply_text, 'fact_ids': ['new1'], 'memory_ids': [],
                'claims': [{'text': 'Xe mình màu trắng.', 'subject': 'persona', 'predicate': 'gen.car',
                    'fact_ids': ['new1'], 'memory_ids': [], 'message_ids': []}],
                'product_claims': [], 'fact_proposals': [{'proposal_id': 'new1',
                    'namespace': 'car', 'slug': 'color', 'value': 'Trắng', 'precision': 'exact'}],
                'asks_question': True, 'question': {'kind': 'conversation', 'field': None,
                    'text': expected_question}},
            'diagnostics': None, 'peer_interpretation': peer}

        def provider(_context, transcript, _schema, _timeout, **_kwargs):
            if not transcript:
                return tool('plan', plan)
            action = transcript[-2]['message']['action']
            if action == 'plan':
                return tool('retrieve', {'kind': 'persona_facts', 'topic': 'vehicle_current',
                    'query': None, 'keys': ['vehicle.current.color'],
                    'entities': {'vehicle': 'Creta', 'location': None, 'people': []},
                    'time_start': None, 'time_end': None, 'variant': None})
            if action == 'retrieve':
                self.assertEqual(transcript[-1]['tool_result']['status'], 'NOT_FOUND')
                return tool('record_customer_facts', {'facts': [
                    {'predicate': 'usage_need', 'value': 'cho gia đình',
                     'evidence_excerpt': 'cho gia đình', 'subject': 'customer', 'certainty': 'explicit'},
                    {'predicate': 'interested_vehicle', 'value': 'Creta',
                     'evidence_excerpt': 'con này', 'subject': 'customer', 'certainty': 'inferred'}]})
            self.assertEqual(action, 'record_customer_facts')
            self.assertEqual(transcript[-1]['tool_result']['status'], 'RECORDED')
            return tool('final', final)

        with patch('app.adapters.luna.call_luna_tool', side_effect=provider):
            turn = self.app.turn(conversation, {'text': text})['turn']

        self.assertEqual(turn.get('generation_validation'), 'OK', turn)
        self.assertEqual(turn['response_text'], reply_text)
        self.assertTrue(turn['outbound_permission'])
        self.assertIn('AUTO_REPLY', turn['routing'])
        self.assertNotIn('HANDOFF_DRAFT_ONLY', turn['routing'])
        self.assertEqual(turn['semantic']['intent_labels'], ['buy'])
        facts = list_customer_facts(conversation['engine'].persona_store,
            self.app.event(conversation, text, turn['comment_id']))
        self.assertEqual({(row['predicate'], row['value'], row['certainty']) for row in facts}, {
            ('usage_need', 'cho gia đình', 'explicit'),
            ('interested_vehicle', 'Creta', 'inferred')})
        generated = conversation['engine'].persona_store.list_generated(
            conversation['post']['persona_id'])
        self.assertEqual([(row['fact_key'], row['value_text'], row['status']) for row in generated],
            [('gen.car.color', 'Trắng', 'ACTIVE')])

    def test_story_claim_allows_grounded_paraphrase_and_rejects_changed_place(self):
        from app.persona_stories import StoryStore
        engine = self.conversation['engine']
        engine.model_led_conversation = True
        repo = engine.persona_store
        snapshot = repo.get_binding_snapshot(self.conversation['page_id'], self.conversation['post']['id'])
        vehicle_id = next(f['fact_id'] for f in snapshot['facts']
                          if f['key'] == 'vehicle.current.model')
        accepted = StoryStore(repo).propose(snapshot, {
            'proposal_id': 'new1', 'idempotency_key': 'story-paraphrase-test',
            'persona_version': snapshot['release_id'], 'topic': 'chuyến đi',
            'event': 'Mình đi Đà Lạt bằng Creta', 'time_start': '2025-04-01',
            'time_end': None, 'entities': {'vehicle': 'Creta', 'location': 'Đà Lạt', 'people': []},
            'base_fact_ids': [vehicle_id], 'new_details': [], 'relation': None})
        self.assertEqual(accepted['status'], 'ACCEPTED', accepted)
        memory = accepted['memory']
        memory_id = memory['memory_id']

        retrieved = StoryStore(repo).retrieve(snapshot, topic='chuyến đi',
            query='Mình đi Đà Lạt bằng Creta')
        self.assertEqual(retrieved['status'], 'FOUND')
        retrieved['memories'][0]['already_told_to_customer'] = True
        metadata_candidate = {'memory_ids': [memory_id], 'claims': [{
            'predicate': 'gen.memory', 'text': memory['event'], 'memory_ids': [memory_id]}]}
        _, _, metadata_error = engine._attach_cited_stories(snapshot,
            {'stories': retrieved['memories']}, metadata_candidate)
        self.assertIsNone(metadata_error)

        def attach(text):
            candidate = {'memory_ids': [memory_id], 'claims': [{
                'predicate': 'gen.memory', 'text': text, 'memory_ids': [memory_id]}]}
            return engine._attach_cited_stories(snapshot, {'stories': [memory]}, candidate)

        attached, ids, error = attach('Mình lái Creta tới Đà Lạt.')
        self.assertIsNone(error)
        self.assertEqual(ids, {memory_id})
        self.assertTrue(any(f['fact_id'] == 'story:' + memory_id for f in attached['facts']))
        self.assertEqual(attach('Mình lái Creta tới Vũng Tàu.')[2], 'STORY_CLAIM_UNVERIFIED')
        self.assertEqual(attach('Mình đi Đà Lạt bằng Creta, vui lắm.')[2], 'STORY_CLAIM_UNVERIFIED')

    def test_persona_generation_uses_two_phases_and_sends_without_review(self):
        text = "Bác đi xe gì?"
        captured = []

        def call(_post, comment, **kwargs):
            captured.append(kwargs)
            return generated_turn(comment, kwargs.get("persona_phase"), kwargs.get("persona_context"))

        with patch("sandbox.server.call_luna", side_effect=call):
            state = self.app.turn(self.conversation, {"text": text})

        turn = state["turn"]
        self.assertEqual([call["persona_phase"] for call in captured], ["analysis", "reply"])
        self.assertNotIn("persona_context", captured[0])
        supplied = captured[1]["persona_context"]
        self.assertEqual(supplied.keys() & {"facts", "memories", "voice_guide"},
                         {"facts", "memories", "voice_guide"})
        self.assertEqual(supplied["channel"], "public")
        self.assertTrue(supplied["context_token"])
        self.assertTrue(supplied["messages"])
        self.assertEqual(self.conversation["generation_mode"], "generated")
        self.assertEqual(state["generation_mode"], "generated")
        self.assertEqual(state["channel"], "public")
        self.assertEqual(state["post"]["persona_origin"], "fictional_fixture")
        self.assertEqual(turn["diagnostics"]["intent_confidence"][0]["confidence"], 0.7)
        self.assertEqual(turn["peer_signals"]["speaker_intent"], "social")
        self.assertEqual(turn["response_text"], "Mình đang đi Hyundai Creta.")
        self.assertEqual(len(turn["sends"]), 1)
        self.assertTrue(turn["outbound_permission"])
        self.assertNotIn("review_drafts", state)
        self.assertEqual(turn["attempts"], 2)
        self.assertEqual([call["phase"] for call in turn["model_calls"]], ["analysis", "reply"])
        self.assertIsNone(turn.get("jev_shadow_event_id"))
        audit = self.conversation["engine"].store.conn.execute(
            "SELECT payload FROM audit WHERE comment_id=? AND kind='persona_generated_turn'",
            (turn["comment_id"],)).fetchone()
        self.assertIsNotNone(audit)
        recorded = json.loads(audit[0])
        self.assertEqual(recorded["persona_id"], supplied["persona_uuid"])
        self.assertEqual(recorded["fact_ids_supplied"], [fact["fact_id"] for fact in supplied["facts"]])
        self.assertEqual(recorded["validation"], "OK")

    def test_generated_reply_does_not_answer_dealer_question_with_vehicle_fact(self):
        text = "Bạn mua xe ở đại lý nào?"

        def call(_post, comment, **kwargs):
            phase = kwargs.get("persona_phase")
            context = kwargs.get("persona_context")
            result = generated_turn(comment, phase, context)
            if phase == "analysis":
                result["needs"] = {"topics": ["vehicle_current"],
                                   "keys": ["vehicle.current.model"]}
            else:
                fact = next(row for row in context["facts"]
                            if row["key"] == "vehicle.current.model")
                reply = "Mình đang đi Hyundai Creta."
                result["reply_candidate"] = {"reply_text": reply,
                    "fact_ids": [fact["fact_id"]], "memory_ids": [],
                    "claims": [{"text": reply, "subject": "persona",
                        "predicate": "vehicle.current.model", "fact_ids": [fact["fact_id"]],
                        "memory_ids": [], "message_ids": []}],
                    "asks_question": False, "question": None, "fact_proposals": []}
            return result

        with patch("sandbox.server.call_luna", side_effect=call):
            state = self.app.turn(self.conversation, {"text": text})

        turn = state["turn"]
        self.assertEqual(turn["generation_validation"], "REPLY_TOPIC_MISMATCH")
        self.assertNotEqual(turn["response_text"], "Mình đang đi Hyundai Creta.")
        self.assertIn("chưa có thông tin xác nhận đại lý", turn["response_text"])

    def test_sandbox_does_not_reapprove_after_failed_generated_dispatch(self):
        def call(_post, comment, **kwargs):
            return generated_turn(comment, kwargs.get("persona_phase"), kwargs.get("persona_context"))

        engine = self.conversation["engine"]
        with patch("sandbox.server.call_luna", side_effect=call), \
             patch.object(engine.sender, "send", return_value={"status": "failed"}), \
             patch.object(engine, "review_generated") as review:
            state = self.app.turn(self.conversation, {"text": "Bác đi xe gì?"})

        review.assert_not_called()
        self.assertFalse(state["turn"]["outbound_permission"])
        self.assertEqual(state["turn"]["routing"][-1], "SEND_FAILED")
        self.assertIsNone(state["turn"]["response_text"])
        self.assertEqual(state["turn"]["candidate_text"], "Mình đang đi Hyundai Creta.")

    def test_failed_generated_dispatch_marks_proposals_discarded_in_turn_and_audit(self):
        text = "Cuối tuần bạn hay đi đâu?"
        def call(_post, comment, **kwargs):
            if kwargs.get("persona_phase") == "analysis":
                result = generated_turn(comment, "analysis", kwargs.get("persona_context"))
                result["needs"] = {"topics": ["trip"], "keys": []}
                return result
            reply = "Cuối tuần mình hay đi Đà Lạt."
            return {"ok": True, "semantic": persona_semantic(comment),
                "peer_signals": {"speaker_intent": "social", "address_term": None,
                    "persona_topic": "travel_story", "asks_persona_question": True,
                    "claims": [], "evidence_excerpt": comment},
                "reply_candidate": {"reply_text": reply, "fact_ids": ["new1"], "memory_ids": [],
                    "claims": [{"text": reply, "subject": "persona", "predicate": "gen.trip",
                        "fact_ids": ["new1"], "memory_ids": [], "message_ids": []}],
                    "asks_question": False, "question": None,
                    "fact_proposals": [{"proposal_id": "new1", "namespace": "trip",
                        "slug": "weekend_getaway", "value": "Cuối tuần mình hay đi Đà Lạt",
                        "precision": "approximate"}]},
                "attempts": 1, "usage": {}, "cost_usd_est": 0.001,
                "cost_unknown": False, "model_effective": "fixture/luna"}

        engine = self.conversation["engine"]
        with patch("sandbox.server.call_luna", side_effect=call), \
             patch.object(engine.sender, "send", return_value={"status": "failed"}):
            state = self.app.turn(self.conversation, {"text": text})

        turn = state["turn"]
        self.assertEqual(turn["routing"][-1], "SEND_FAILED")
        self.assertEqual(turn["proposal_results"][0]["status"], "DISCARDED")
        audit = engine.store.conn.execute(
            "SELECT payload FROM audit WHERE comment_id=? AND kind='persona_generated_turn'",
            (turn["comment_id"],)).fetchone()
        self.assertEqual(json.loads(audit[0])["proposals"][0]["status"], "DISCARDED")
        fact = engine.store.conn.execute(
            "SELECT status FROM persona_generated_facts WHERE created_comment_id=?",
            (turn["comment_id"],)).fetchone()
        self.assertEqual(fact[0], "DISCARDED")

    def test_real_persona_uses_its_own_published_binding_and_sandbox_autosend(self):
        persona = {'id': 'content-person-42', 'name': 'Linh', 'occupation': 'Giáo viên',
                   'car_owned': 'Hyundai Tucson', 'tone': 'warm'}
        with patch.object(self.app, 'personas', return_value=[persona]), \
             patch.object(self.app, 'generate_post', return_value='Bài do Linh viết.'):
            state = self.app.create({'actor_id': 'customer-real', 'persona_id': persona['id']})
        conversation = self.app.conversations[state['id']]
        binding = conversation['engine'].persona_store.get_binding_snapshot(
            conversation['page_id'], conversation['post']['id'])
        self.assertFalse(binding['fixture_only'])
        self.assertEqual(binding['source_kind'], 'other')
        self.assertEqual(binding['snapshot']['release']['status'], 'PUBLISHED')
        self.assertEqual(conversation['post']['persona_id'], binding['persona_uuid'])
        self.assertNotEqual(binding['persona_uuid'], self.conversation['post']['persona_id'])
        self.assertTrue(conversation['engine'].auto_send_generated)
        self.assertEqual(conversation['generation_mode'], 'generated')

    def test_generated_post_facts_activate_and_return_as_authored_post_evidence(self):
        post_text = 'Cuối tuần mình hay đi Đà Lạt.'
        proposal = {'proposal_id': 'new1', 'namespace': 'trip', 'slug': 'weekend_getaway',
                    'value': 'Cuối tuần mình hay đi Đà Lạt', 'precision': 'approximate'}
        candidate = {'post_text': post_text, 'fact_ids': ['new1'], 'memory_ids': [],
            'claims': [{'text': post_text, 'subject': 'persona', 'predicate': 'gen.trip',
                'fact_ids': ['new1'], 'memory_ids': [], 'message_ids': []}],
            'fact_proposals': [proposal]}

        def call(_post, text, **kwargs):
            if kwargs.get('persona_phase') == 'analysis':
                return {'ok': True, 'needs': {'topics': ['trip'], 'keys': []}, 'attempts': 1,
                        'usage': {'prompt_tokens': 10, 'completion_tokens': 10},
                        'cost_usd_est': 0.001, 'cost_unknown': False, 'model_effective': 'fixture/luna'}
            return {'ok': True, 'post_candidate': candidate, 'attempts': 1,
                    'usage': {'prompt_tokens': 20, 'completion_tokens': 30},
                    'cost_usd_est': 0.002, 'cost_unknown': False, 'model_effective': 'fixture/luna'}

        persona = next(p for p in self.app.personas() if p.get('slug') == 'nam_creta')
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only-key'}), \
             patch('sandbox.server.call_luna', side_effect=call):
            state = self.app.create({'actor_id': 'post-customer', 'persona_id': persona['id']})
        conversation = self.app.conversations[state['id']]
        facts = conversation['engine'].persona_store.list_generated(
            conversation['post']['persona_id'])
        self.assertEqual([phase['phase'] for phase in conversation['post']['generation']['phase_calls']],
                         ['analysis', 'post'])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]['status'], 'ACTIVE')
        self.assertEqual(facts[0]['source_ref'], f"luna_generated_post:{conversation['post']['id']}")
        authored = conversation['postcard']['persona_authored_post']
        self.assertEqual(authored['text'], post_text)
        self.assertIn(facts[0]['gen_fact_id'], authored['fact_ids'])
        event = self.app.event(conversation, 'Kể thêm về chuyến đi đi.')
        context = conversation['engine'].persona_generation_context(
            event, conversation['postcard'], {'topics': ['trip'], 'keys': []})
        self.assertEqual(context['persona_authored_post'], authored)
        self.assertIn(facts[0]['gen_fact_id'], {fact['fact_id'] for fact in context['facts']})

    def test_authored_post_fact_order_does_not_reject_reply_context(self):
        persona = next(p for p in self.app.personas() if p.get('slug') == 'nam_creta')
        post_text = 'Mình đang đi Hyundai Creta và làm kỹ sư cơ khí.'

        def generate(_persona, snapshot, _metrics=None, owner_id=None):
            by_key = {fact['key']: fact for fact in snapshot['facts']}
            fact_ids = sorted((by_key['vehicle.current.model']['fact_id'],
                               by_key['occupation.title']['fact_id']), reverse=True)
            _metrics.update(fact_ids=fact_ids, generated_fact_ids=[],
                generation_validation={'status': 'OK'}, proposal_results=[])
            return post_text

        with patch.object(self.app, 'generate_post', side_effect=generate):
            state = self.app.create({'actor_id': 'post-fact-order-customer',
                                     'persona_id': persona['id']})
        conversation = self.app.conversations[state['id']]
        with patch('sandbox.server.call_luna', side_effect=lambda _post, text, **kwargs:
                   generated_turn(text, kwargs.get('persona_phase'), kwargs.get('persona_context'))):
            state = self.app.turn(conversation, {'text': 'Bạn đang đi xe gì?'})

        turn = state['turn']
        self.assertEqual(turn['generation_validation'], 'OK')
        self.assertEqual(turn['response_text'], 'Mình đang đi Hyundai Creta.')

    def test_generated_post_creation_failure_discards_pending_facts(self):
        proposal = {'proposal_id': 'new1', 'namespace': 'trip', 'slug': 'weekend_getaway',
                    'value': 'Cuối tuần mình hay đi Đà Lạt', 'precision': 'approximate'}
        post_text = 'Cuối tuần mình hay đi Đà Lạt.'
        candidate = {'post_text': post_text, 'fact_ids': ['new1'], 'memory_ids': [],
            'claims': [{'text': post_text, 'subject': 'persona', 'predicate': 'gen.trip',
                'fact_ids': ['new1'], 'memory_ids': [], 'message_ids': []}],
            'fact_proposals': [proposal]}

        def call(_post, _text, **kwargs):
            if kwargs.get('persona_phase') == 'analysis':
                return {'ok': True, 'needs': {'topics': ['trip'], 'keys': []}, 'attempts': 1,
                    'usage': {}, 'cost_usd_est': 0.0, 'cost_unknown': False, 'model_effective': 'fixture/luna'}
            return {'ok': True, 'post_candidate': candidate, 'attempts': 1, 'usage': {},
                'cost_usd_est': 0.0, 'cost_unknown': False, 'model_effective': 'fixture/luna'}

        persona = next(p for p in self.app.personas() if p.get('slug') == 'nam_creta')
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only-key'}), \
             patch('sandbox.server.call_luna', side_effect=call), \
             patch('sandbox.server.make_engine', side_effect=RuntimeError('engine init failed')):
            with self.assertRaisesRegex(RuntimeError, 'engine init failed'):
                self.app.create({'actor_id': 'post-failure', 'persona_id': persona['id']})

        facts = self.app.persona_repository().conn.execute(
            "SELECT status FROM persona_generated_facts WHERE persona_id=? AND source_ref LIKE 'luna_generated_post:%'",
            (persona['id'],)).fetchall()
        self.assertEqual([row[0] for row in facts], ['DISCARDED'])

    def test_generated_post_content_is_not_validated_and_proposals_activate(self):
        post_text = 'Cuối tuần mình hay đi Đà Lạt.'
        candidate = {'post_text': post_text, 'fact_ids': ['new1'], 'memory_ids': [],
            'claims': [{'text': post_text, 'subject': 'persona', 'predicate': 'gen.trip',
                'fact_ids': ['new2'], 'memory_ids': [], 'message_ids': []}],
            'fact_proposals': [{'proposal_id': 'new1', 'namespace': 'trip',
                'slug': 'weekend_getaway', 'value': 'Cuối tuần mình hay đi Đà Lạt',
                'precision': 'approximate'}]}

        def call(_post, _text, **kwargs):
            if kwargs.get('persona_phase') == 'analysis':
                return {'ok': True, 'needs': {'topics': ['trip'], 'keys': []}, 'attempts': 1,
                    'usage': {}, 'cost_usd_est': 0.0, 'cost_unknown': False, 'model_effective': 'fixture/luna'}
            return {'ok': True, 'post_candidate': candidate, 'attempts': 1, 'usage': {},
                'cost_usd_est': 0.0, 'cost_unknown': False, 'model_effective': 'fixture/luna'}

        persona = next(p for p in self.app.personas() if p.get('slug') == 'nam_creta')
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only-key'}), \
             patch('sandbox.server.call_luna', side_effect=call):
            created = self.app.create({'actor_id': 'post-validation-disabled', 'persona_id': persona['id']})
        self.assertTrue(created['id'])
        post = self.app.conversations[created['id']]['post']
        self.assertEqual(post['text'], post_text)
        self.assertEqual(post['generation']['generation_validation']['status'], 'SKIPPED')

        facts = self.app.persona_repository().conn.execute(
            "SELECT status FROM persona_generated_facts WHERE persona_id=? AND source_ref LIKE 'luna_generated_post:%'",
            (persona['id'],)).fetchall()
        self.assertEqual([row[0] for row in facts], ['ACTIVE'])

    def test_generated_post_does_not_call_reply_validator(self):
        post_text = 'Cuối tuần mình hay đi Đà Lạt.'
        candidate = {'post_text': post_text, 'fact_ids': ['new1'], 'memory_ids': [],
            'claims': [{'text': post_text, 'subject': 'persona', 'predicate': 'gen.trip',
                'fact_ids': ['new1'], 'memory_ids': [], 'message_ids': []}],
            'fact_proposals': [{'proposal_id': 'new1', 'namespace': 'trip',
                'slug': 'weekend_getaway', 'value': 'Cuối tuần mình hay đi Đà Lạt',
                'precision': 'approximate'}]}

        def call(_post, _text, **kwargs):
            if kwargs.get('persona_phase') == 'analysis':
                return {'ok': True, 'needs': {'topics': ['trip'], 'keys': []}, 'attempts': 1,
                    'usage': {}, 'cost_usd_est': 0.0, 'cost_unknown': False, 'model_effective': 'fixture/luna'}
            return {'ok': True, 'post_candidate': candidate, 'attempts': 1, 'usage': {},
                'cost_usd_est': 0.0, 'cost_unknown': False, 'model_effective': 'fixture/luna'}

        persona = next(p for p in self.app.personas() if p.get('slug') == 'nam_creta')
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only-key'}), \
             patch('sandbox.server.call_luna', side_effect=call), \
             patch('app.persona_generation.validate_generated_post', side_effect=RuntimeError('validator failed')) as validator:
            created = self.app.create({'actor_id': 'post-validator-disabled', 'persona_id': persona['id']})
        self.assertTrue(created['id'])
        validator.assert_not_called()

        facts = self.app.persona_repository().conn.execute(
            "SELECT status FROM persona_generated_facts WHERE persona_id=? AND source_ref LIKE 'luna_generated_post:%'",
            (persona['id'],)).fetchall()
        self.assertEqual([row[0] for row in facts], ['ACTIVE'])

    def test_generated_post_audit_failure_discards_pending_facts(self):
        from app.state import Store
        from datetime import datetime, timezone
        repository = self.app.persona_repository()
        persona = next(p for p in self.app.personas() if p.get('slug') == 'nam_creta')
        now = datetime.now(timezone.utc).isoformat()
        def generate(_persona, snapshot, _metrics=None, owner_id=None):
            fact_id = repository.propose_generated(snapshot['persona']['id'], 'gen.trip.test_post_story',
                'Cuối tuần mình hay đi Đà Lạt', 'approximate', 'test_post', owner_id, now)
            _metrics.update(generated_fact_ids=[fact_id], fact_ids=[fact_id],
                generation_validation={'status': 'OK'}, proposal_results=[{'status': 'PENDING'}])
            return 'Cuối tuần mình hay đi Đà Lạt.'
        original = Store.audit
        def fail_audit(store, comment_id, kind, payload, created_at):
            if kind == 'persona_authored_post_generated_facts':
                raise OSError('audit unavailable')
            return original(store, comment_id, kind, payload, created_at)
        with patch.object(self.app, 'generate_post', side_effect=generate), \
             patch.object(Store, 'audit', fail_audit), \
             self.assertRaisesRegex(OSError, 'audit unavailable'):
            self.app.create({'actor_id': 'post-audit-failure', 'persona_id': persona['id']})
        facts = repository.conn.execute(
            "SELECT status FROM persona_generated_facts WHERE persona_id=? AND source_ref='test_post'",
            (persona['id'],)).fetchall()
        self.assertEqual([row[0] for row in facts], ['DISCARDED'])

    def test_analysis_truncation_retry_counts_both_phases_and_keeps_cost_unknown(self):
        from types import SimpleNamespace

        text = "Chào bạn!"
        semantic = persona_semantic(text)
        analysis_envelope = {"schema_version": "persona_analysis_v1", "semantic": semantic,
            "needs": {"topics": [], "keys": []}, "diagnostics": None, "peer_interpretation": None}
        snapshot = self.conversation['engine'].persona_store.get_binding_snapshot(
            self.conversation['page_id'], self.conversation['post']['id'])
        name_fact = next(fact for fact in snapshot['facts'] if fact['key'] == 'identity.name')
        reply_text = 'Chào bạn, mình là Nam.'
        reply_envelope = {"schema_version": "persona_turn_v1", "semantic": semantic,
            "reply": {"reply_text": reply_text, "fact_ids": [name_fact['fact_id']], "memory_ids": [],
                "claims": [{"text": reply_text, "subject": "persona", "predicate": "identity.name",
                    "fact_ids": [name_fact['fact_id']], "memory_ids": [], "message_ids": []}],
                "asks_question": False, "question": None, "fact_proposals": []},
            "diagnostics": None, "peer_interpretation": None}
        responses = [
            {"model": "fixture/luna", "usage": {"prompt_tokens": 300, "completion_tokens": 2400},
             "choices": [{"finish_reason": "length", "message": {"content": None}}]},
            {"model": "fixture/luna", "usage": {"prompt_tokens": 300, "completion_tokens": 70,
                                                    "cost": 0.001},
             "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(analysis_envelope)}}]},
            {"model": "fixture/luna", "usage": {"prompt_tokens": 350, "completion_tokens": 80,
                                                    "cost": 0.002},
             "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(reply_envelope)}}]},
        ]
        payloads = []

        def open_request(request, timeout):
            payloads.append(json.loads(request.data))
            return io.BytesIO(json.dumps(responses.pop(0)).encode())

        opener = SimpleNamespace(open=open_request)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch("app.adapters.luna.urllib.request.build_opener", return_value=opener):
            state = self.app.turn(self.conversation, {"text": text})

        self.assertEqual([payload["max_tokens"] for payload in payloads], [2400, 3200, 2400])
        self.assertEqual([payload["response_format"]["json_schema"]["name"] for payload in payloads],
                         ["persona_analysis_v1", "persona_analysis_v1", "persona_turn_v1"])
        self.assertEqual(state["turn"]["attempts"], 3)
        self.assertTrue(state["turn"]["cost_unknown"])
        self.assertIsNone(state["turn"]["cost_usd_est"])
        self.assertEqual(state["turn"]["response_text"], reply_text)

    def test_draft_review_route_is_removed(self):
        with patch("sandbox.server.call_luna", side_effect=lambda _post, text, **kw: generated_turn(text, kw.get("persona_phase"), kw.get("persona_context"))):
            state = self.app.turn(self.conversation, {"text": "Chào bạn!"})
        status, result = self.request("POST",
            f"/api/conversations/{self.state['id']}/drafts/anything", {"action": "approve"})
        self.assertEqual(status, 404, result)
        self.assertNotIn("review_drafts", state)

    def test_public_and_private_channel_are_explicit_and_shadow_is_off_by_default(self):
        with patch.object(self.app, "generate_post", return_value="Private-channel post."):
            private = self.app.create({"actor_id": "customer-2", "persona_id": self.conversation["post"]["persona_id"],
                                       "channel": "private"})
        self.assertEqual(self.state["channel"], "public")
        self.assertEqual(private["channel"], "private")
        self.assertEqual(self.app.event(self.conversation)["channel"], "public")
        self.assertIsNone(self.app.start_jev_shadow(self.conversation, "disabled-event", "{}"))
        self.assertNotIn("disabled-event", self.conversation["jev_shadows"])

    def test_missing_persona_snapshot_support_fails_closed_instead_of_using_legacy_auto_reply(self):
        with patch.object(self.conversation["engine"], "persona_generation_context", None), \
             patch("sandbox.server.call_luna") as call:
            result = self.app.turn(self.conversation, {"text": "Chào bạn!"})
        self.assertIn("REVIEW_GENERATION_CONTEXT", result["turn"]["routing"])
        self.assertEqual(result["turn"]["attempts"], 0)
        self.assertFalse(result["turn"]["sends"])
        call.assert_not_called()

    def test_approved_reply_is_in_next_turn_context(self):
        def call(_post, text, **kwargs):
            if kwargs.get('persona_phase') == 'reply' and text == 'cảm ơn':
                replies = json.loads(kwargs['context'])['recent_bot_replies']
                self.assertIn('Mình đang đi Hyundai Creta.', [r['text'] for r in replies])
            return generated_turn(text, kwargs.get('persona_phase'), kwargs.get('persona_context'))
        with patch('sandbox.server.call_luna', side_effect=call):
            state = self.app.turn(self.conversation, {'text': 'Bác đi xe gì?'})
            self.assertEqual(state['turn']['response_text'], 'Mình đang đi Hyundai Creta.')
            self.assertTrue(state['turn']['outbound_permission'])
            self.assertEqual(len(state['fake_sends']), 1)
            self.app.turn(self.conversation, {'text': 'cảm ơn'})

    def test_grounded_fallback_is_sent_automatically(self):
        def call(_post, text, **kwargs):
            if kwargs.get('persona_phase') == 'analysis':
                return generated_turn(text, 'analysis')
            return {"ok": False, "attempts": 1, "error_code": "MODEL_RESPONSE_SHAPE",
                    "error": "reply phase unavailable", "cost_unknown": True}
        with patch('sandbox.server.call_luna', side_effect=call):
            state = self.app.turn(self.conversation, {'text': 'Bác đi xe gì?'})
        self.assertEqual(state['turn']['response_text'], 'Mình đang đi Hyundai Creta.')
        self.assertEqual(len(state['fake_sends']), 1)

    def test_phase_one_failure_falls_back_to_semantic_parser_with_all_facts(self):
        seen = []
        def call(_post, text, **kwargs):
            seen.append(kwargs)
            if kwargs.get('persona_phase') == 'analysis':
                return {"ok": False, "attempts": 1, "error_code": "MODEL_SEMANTIC_SHAPE",
                        "error": "analysis failed", "cost_unknown": False, "cost_usd_est": 0.0}
            return {"ok": True, "semantic": persona_semantic(text), "attempts": 1,
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "cost_unknown": False, "cost_usd_est": 0.001}
        with patch('sandbox.server.call_luna', side_effect=call):
            state = self.app.turn(self.conversation, {'text': 'Bác đi xe gì?'})
        self.assertEqual([call.get('persona_phase') for call in seen], ['analysis', None])
        self.assertEqual(state['turn']['response_text'], 'Mình đang đi Hyundai Creta.')
        supplied_keys = {fact['key'] for fact in state['turn']['persona_facts_supplied']}
        self.assertIn('vehicle.current.model', supplied_keys)

    def test_generated_story_fact_is_shown_and_can_be_revoked(self):
        def call(_post, text, **kwargs):
            if kwargs.get('persona_phase') == 'analysis':
                result = generated_turn(text, 'analysis')
                result['needs'] = {'topics': ['trip'], 'keys': []}
                return result
            existing = next((fact for fact in kwargs['persona_context']['facts'] if fact.get('generated')), None)
            if existing:
                reply_text = 'Cuối tuần mình hay đi Đà Lạt.'
                return {"ok": True, "semantic": persona_semantic(text), "attempts": 1,
                    "usage": {"prompt_tokens": 20, "completion_tokens": 20},
                    "cost_unknown": False, "cost_usd_est": 0.001,
                    "reply_candidate": {"reply_text": reply_text, "fact_ids": [existing['fact_id']], "memory_ids": [],
                        "claims": [{"text": reply_text, "subject": "persona", "predicate": "gen.trip",
                            "fact_ids": [existing['fact_id']], "memory_ids": [], "message_ids": []}],
                        "asks_question": False, "question": None, "fact_proposals": []}}
            reply_text = 'Cuối tuần mình hay đi Đà Lạt.'
            return {"ok": True, "semantic": persona_semantic(text), "attempts": 1,
                "usage": {"prompt_tokens": 20, "completion_tokens": 20},
                "cost_unknown": False, "cost_usd_est": 0.001,
                "reply_candidate": {"reply_text": reply_text, "fact_ids": ['new1'], "memory_ids": [],
                    "claims": [{"text": reply_text, "subject": "persona", "predicate": "gen.trip",
                        "fact_ids": ['new1'], "memory_ids": [], "message_ids": []}],
                    "asks_question": False, "question": None,
                    "fact_proposals": [{"proposal_id": "new1", "namespace": "trip",
                        "slug": "weekend_getaway", "value": "Cuối tuần mình hay đi Đà Lạt",
                        "precision": "approximate"}]}}
        with patch('sandbox.server.call_luna', side_effect=call):
            state = self.app.turn(self.conversation, {'text': 'Bạn kể chuyện cuối tuần đi'})
        turn = state['turn']
        self.assertEqual(turn['response_text'], 'Cuối tuần mình hay đi Đà Lạt.')
        self.assertEqual(turn['proposal_results'][0]['status'], 'ACTIVE')
        self.assertEqual(state['generated_facts'][0]['status'], 'ACTIVE')
        self.assertTrue(self.conversation['engine'].persona_store.conn is self.conversation['engine'].store.conn)
        with patch('sandbox.server.call_luna', side_effect=call):
            second = self.app.turn(self.conversation, {'text': 'Bạn đi cuối tuần thế nào?', 'actor_id': 'customer-2'})
        self.assertEqual(second['turn']['response_text'], 'Cuối tuần mình hay đi Đà Lạt.')
        self.assertEqual(len(second['generated_facts']), 1)
        fact_id = state['generated_facts'][0]['gen_fact_id']
        status, result = self.request('POST',
            f"/api/conversations/{self.state['id']}/facts/{fact_id}/revoke", {'reason': 'Không dùng nữa'})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['generated_facts'][0]['status'], 'REVOKED')

    def test_real_persona_generated_fact_can_be_revoked(self):
        persona = {'id': 'real-person-revoke', 'name': 'Linh', 'occupation': 'Giáo viên',
                   'car_owned': 'Hyundai Tucson', 'tone': 'warm'}
        with patch.object(self.app, 'personas', return_value=[persona]), \
             patch.object(self.app, 'generate_post', return_value='Bài do Linh viết.'):
            state = self.app.create({'actor_id': 'revoke-real-customer', 'persona_id': persona['id']})
        conversation = self.app.conversations[state['id']]
        store = conversation['engine'].persona_store
        binding = store.get_binding_snapshot(conversation['page_id'], conversation['post']['id'])
        now = conversation['engine'].clock.now().isoformat()
        fact_id = store.propose_generated(binding['persona_uuid'], 'gen.trip.weekend_route',
            'Cuối tuần mình hay đi Đà Lạt', 'approximate', 'test', 'real-post-fact', now)
        with store._atomic():
            store.activate_generated('real-post-fact', [fact_id], now)

        status, result = self.request('POST',
            f"/api/conversations/{state['id']}/facts/{fact_id}/revoke", {'reason': 'Thu hồi dữ kiện thử'})

        self.assertEqual(status, 200, result)
        self.assertEqual(next(fact for fact in result['generated_facts']
                              if fact['gen_fact_id'] == fact_id)['status'], 'REVOKED')


if __name__ == "__main__":
    unittest.main()
