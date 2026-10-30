import sqlite3
import unittest

from app.persona_store import PersonaStore, migrate_sqlite
from app.persona_fixture import seed_database_demo, seed_nam_creta


class PersonaFixtureTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        migrate_sqlite(self.conn)
        self.store = PersonaStore(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_nam_creta_is_a_disclosed_fictional_release_with_provenance(self):
        persona_id, release_id = seed_nam_creta(self.store)
        snapshot = self.store.get_release_snapshot(persona_id, release_id)
        self.assertEqual(snapshot["persona"]["source_kind"], "fictional_fixture")
        self.assertTrue(snapshot["fixture_only"])
        self.assertFalse(snapshot["release"]["approved"])
        self.assertTrue(any(fact["source_ref"].startswith("fictional_fixture:nam_creta:")
                            for fact in snapshot["facts"]))
        self.assertFalse(any(fact["key"] == "purchase.dealer" for fact in snapshot["facts"]))

    def test_fixture_seed_is_idempotent(self):
        first = seed_nam_creta(self.store)
        second = seed_nam_creta(self.store)
        self.assertEqual(first, second)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM personas").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM persona_releases").fetchone()[0], 1)

    def test_database_demo_snapshots_can_coexist_when_source_changes(self):
        template = seed_nam_creta(self.store)
        self.store.create_persona('legacy_demo', 'Coach Tuấn Gym · kịch bản demo',
                                  source_kind='fictional_fixture')
        persona = {'id': '21', 'name': 'Coach Tuấn Gym', 'car_owned': 'Ford Ranger XLS'}
        first = seed_database_demo(self.store, persona, template)
        self.assertEqual(first, seed_database_demo(self.store, persona, template))
        second = seed_database_demo(self.store, dict(persona, car_owned='Ford Everest'), template)
        self.assertNotEqual(first, second)
        for fixture, car in ((first, 'Ford Ranger XLS'), (second, 'Ford Everest')):
            snapshot = self.store.get_release_snapshot(*fixture)
            self.assertTrue(snapshot['fixture_only'])
            self.assertIn(car, [fact['value'] for fact in snapshot['facts']])
            guide = snapshot['release']['style']['voice_guide']
            fact_ids = {fact['fact_id'] for fact in snapshot['facts']}
            self.assertEqual(snapshot['release']['style']['data_origin'], 'database_snapshot')
            self.assertEqual(guide['provenance'], 'database_snapshot')
            self.assertTrue(all(fact['source_ref'].startswith('database_snapshot:content_ai_fac.personas:')
                                for fact in snapshot['facts']))
            self.assertEqual(len(guide['few_shots']), 12)
            self.assertTrue(all(set(example['fact_ids']).issubset(fact_ids)
                                for example in guide['few_shots']))
            self.assertFalse(any('Nam Định' in example['reply_text'] or 'Hyundai Creta' in example['reply_text']
                                 for example in guide['few_shots']))

    def test_real_persona_import_keeps_its_own_approved_non_fixture_release(self):
        from app import persona_fixture
        seed_database_persona = getattr(persona_fixture, 'seed_database_persona', None)
        self.assertTrue(callable(seed_database_persona), 'real persona import is not implemented')
        source = {'id': 'person-17', 'name': 'Linh', 'occupation': 'Giáo viên',
                  'car_owned': 'Hyundai Tucson', 'tone': 'warm'}
        first = seed_database_persona(self.store, source)
        self.assertEqual(first, seed_database_persona(self.store, source))
        snapshot = self.store.get_release_snapshot(*first)
        self.assertEqual(snapshot['persona']['source_kind'], 'other')
        self.assertFalse(snapshot['fixture_only'])
        self.assertTrue(snapshot['release']['approved'])
        self.assertEqual({fact['key']: fact['value'] for fact in snapshot['facts']}, {
            'identity.name': 'Linh', 'occupation.title': 'Giáo viên',
            'vehicle.current.model': 'Hyundai Tucson'})
        self.assertTrue(all(fact['source_kind'] == 'other' and fact['approval'] == 'approved'
                            for fact in snapshot['facts']))
        self.assertEqual(snapshot['utterances'], [])
        other = seed_database_persona(self.store, dict(source, id='person-18', name='An'))
        self.assertNotEqual(first[0], other[0])

    def test_database_description_is_retained_and_changes_release_on_update(self):
        from app.persona_fixture import seed_database_persona
        source = dict(id='description-test', name='Linh', occupation='Giáo viên',
            car_owned='Hyundai Tucson', category='Gia đình', role='Người viết bài',
            perspective='Chia sẻ ngắn gọn', sample_quote='Chào bạn nhé', tone='warm')
        first = seed_database_persona(self.store, source)
        snapshot = self.store.get_release_snapshot(*first)
        self.assertEqual(snapshot['release']['style']['persona_description'],
            {key: source[key] for key in ('category', 'role', 'perspective', 'sample_quote')})
        second = seed_database_persona(self.store, dict(source, perspective='Thích câu trả lời ngắn'))
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])

    def test_fixture_contains_draft_details_without_inventing_dates(self):
        persona_id, release_id = seed_nam_creta(self.store)
        facts = {f["key"]: f for f in self.store.get_release_snapshot(persona_id, release_id)["facts"]}
        self.assertEqual(facts["vehicle.current.trim"]["value"], "1.5 Cao cấp")
        self.assertEqual(facts["purchase.date"]["value"], "03/2024")
        self.assertEqual(facts["purchase.down_payment_percent"]["value"], 35)
        self.assertEqual(facts["purchase.loan_term_years"]["value"], 5)
        self.assertTrue(facts["vehicle.current.first_car"]["value"])
        self.assertEqual(facts["vehicle.current.mileage_km"]["value"], 28000)
        self.assertIsNone(facts["vehicle.current.mileage_km"]["as_of"])
        self.assertEqual(facts["location.base.district"]["value"], "Cầu Giấy")
        self.assertEqual(facts["travel.weekend.destination"]["value"], "Nam Định")

    def test_voice_guide_examples_and_memory_are_disclosed_fixtures(self):
        persona, release = seed_nam_creta(self.store)
        snapshot = self.store.get_release_snapshot(persona, release)
        guide = snapshot["release"]["style"]["voice_guide"]

        self.assertEqual(guide["approval"], "fixture")
        self.assertEqual(guide["provenance"], "synthetic_fixture")
        self.assertEqual(guide["privacy"], "public")
        self.assertEqual(guide["human_approved_example_target"]["status"], "missing")
        self.assertEqual(guide["human_approved_example_target"]["collected"], 0)
        self.assertTrue(guide["speaker_habits"])
        self.assertTrue(guide["reactions"])
        self.assertTrue(guide["interests"])
        self.assertGreaterEqual(len(guide["few_shots"]), 10)
        self.assertLessEqual(len(guide["few_shots"]), 20)
        self.assertTrue(all(example["approval"] == "fixture" for example in guide["few_shots"]))
        self.assertTrue(all(example["privacy"] == "public" and example["consent_ref"] is None
                            for example in guide["few_shots"]))
        memory = snapshot["memories"][0]
        self.assertEqual(memory["topic"], "travel_story")
        self.assertIsNone(memory["occurred_at"])
        self.assertEqual(memory["approved_summary"], "Cuối tuần mình về Nam Định.")
        self.assertEqual(memory["fact_ids"], [next(f["fact_id"] for f in snapshot["facts"]
                                                       if f["key"] == "travel.weekend.destination")])

    def test_new_fixture_release_preserves_existing_post_binding(self):
        persona = self.store.create_persona("nam_creta", "Nam", source_kind="fictional_fixture")
        self.store.register_key("identity", "identity.name", value_type="text")
        fact = self.store.add_fact(persona, "identity", "identity.name", "Nam",
            source_ref="fictional_fixture:nam_creta:identity.name:v1", source_kind="fictional_fixture", tellable="public")
        self.store.approve_fact(fact, "reviewer")
        old = self.store.create_release(persona, style={"tone": "warm"})
        self.store.add_release_fact(old, fact)
        self.store.publish_release(old, "reviewer")
        self.store.bind_post("page", "post", persona, old)
        _, new = seed_nam_creta(self.store)
        self.assertNotEqual(old, new)
        self.assertEqual(self.store.binding("page", "post")["release_id"], old)
        self.assertEqual(seed_nam_creta(self.store), (persona, new))

    def test_fixture_answers_supported_personal_questions_from_bound_release(self):
        from app.harness import make_engine
        persona, release = seed_nam_creta(self.store)
        self.store.bind_post("page", "post", persona, release)
        engine = make_engine()
        engine.persona_store, engine.allow_persona_fixtures = self.store, True
        cases = [("Tên bạn là gì?", "Nam"), ("Bạn làm nghề gì?", "kỹ sư cơ khí"),
                 ("Bạn ở đâu vậy?", "Hà Nội"), ("Bạn ở khu nào?", "Cầu Giấy"),
                 ("Bạn đang đi xe gì?", "Hyundai Creta"), ("Xe của bạn là bản gì?", "1.5 Cao cấp"),
                 ("Xe của bạn có phải xe đầu đời không?", "xe đầu tiên"),
                 ("Xe của bạn đi bao nhiêu km?", "28.000 km"),
                 ("Bạn mua xe khi nào?", "3/2024"), ("Bạn mua xe năm nào?", "3/2024"),
                 ("Bạn trả trước bao nhiêu?", "35%"), ("Bạn góp mấy năm?", "5 năm"),
                 ("Bạn mua xe trả góp không?", "35%"), ("Bạn mua xe trả thẳng không?", "35%"),
                 ("Bạn đi làm tuyến nào?", "Cầu Giấy–Mỹ Đình"),
                 ("Cuối tuần bạn về đâu?", "Nam Định"), ("Bạn mua xe ở đại lý nào?", "chưa có thông tin xác nhận")]
        for index, (text, expected) in enumerate(cases):
            with self.subTest(text=text):
                event = {"page_id": "page", "post_id": "post", "thread_root_id": "root", "parent_id": "root",
                         "comment_id": f"c{index}", "actor_id": "a", "actor_role": "user", "event_type": "create",
                         "content_version": 1, "text": text, "event_dedupe_key": f"c{index}:1"}
                semantic = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
                            "requests": [{"topic": "social", "desired_outcome": "ack", "polarity": "positive"}],
                            "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}], "uncertainties": []}
                result = engine.process(event, {"text": "Bài viết", "persona_binding": {"release_id": release}}, semantic)
                self.assertIn(expected, result.get("response_text") or "", result)
                self.assertTrue(result["outbound_permission"], result)


if __name__ == "__main__":
    unittest.main()
