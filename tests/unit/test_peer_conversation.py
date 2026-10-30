import unittest

from app.harness import make_engine
from app.persona_fixture import seed_nam_creta
from app.persona_store import PersonaStore, migrate_sqlite
from tests.unit.test_persona_engine import _social_semantic

SYSTEM_VOICE = ('Cảm ơn bạn nhé', 'xác nhận việc mua', 'hệ thống', 'Mình hiểu ý', 'Đã ghi nhận')


class ConversationBase(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        self.addCleanup(self.engine.store.conn.close)
        self.addCleanup(self.engine.sender.receiver.conn.close)
        migrate_sqlite(self.engine.store.conn)
        repository = PersonaStore(self.engine.store)
        pid, release = seed_nam_creta(repository)
        repository.bind_post('p', 'post', pid, release)
        self.engine.persona_store = repository
        self.engine.allow_persona_fixtures = True
        self.post = dict(id='post', page_id='p', text='Creta', vehicles=['Creta'], persona_binding=True)
        self.counter = 0

    def turn(self, text, actor='a', semantic=None):
        self.counter += 1
        event = dict(page_id='p', post_id='post', thread_root_id='root', actor_id=actor,
                     comment_id=f'm{self.counter}', text=text, actor_role='user', event_type='create')
        return event, self.engine.process(event, self.post, semantic or _social_semantic(text))

    def pending(self, actor='a'):
        return [p['expected_slot'] for p in self.engine.store.open_pending_for(
            actor, page_id='p', post_id='post', thread_root_id='root')]

    def state(self, actor='a'):
        return self.engine.store.peer_state(dict(page_id='p', post_id='post', thread_root_id='root', actor_id=actor))

    def profile(self, actor='a'):
        return self.engine.store.lead_profile(dict(page_id='p', post_id='post', thread_root_id='root', actor_id=actor))

    def say(self, result):
        text = result['response_text'] or ''
        for phrase in SYSTEM_VOICE:
            self.assertNotIn(phrase, text)
        return text


class OriginalTranscriptTests(ConversationBase):
    def test_the_three_turns_that_went_wrong(self):
        _, result = self.turn('Bác chạy bao lâu rồi')
        text = self.say(result)
        self.assertIn('3/2024', text)
        self.assertNotIn('?', text)
        self.assertEqual((self.pending(), self.profile()), ([], {}))

        _, result = self.turn('Creta giống bác')
        text = self.say(result)
        self.assertEqual(text.count('?'), 1)
        self.assertIn('bác', text.lower())
        self.assertNotIn('khi nào', text)
        self.assertEqual(self.profile(), {})
        self.assertNotEqual(self.state().get('purchase_intent'), 'exploring')

        _, result = self.turn('tôi có xe rồi')
        text = self.say(result)
        self.assertIn('bác', text)
        self.assertNotIn('?', text)
        self.assertEqual(self.state()['ownership_status'], 'owns_vehicle')
        self.assertEqual(self.state()['address_term'], 'bác')
        self.assertEqual(self.pending(), [])

    def test_owning_a_car_stops_a_pending_buy_question(self):
        self.turn('Toi cũng tính mua')
        self.assertEqual(self.pending(), ['interested_vehicle'])
        _, result = self.turn('tôi có xe rồi')
        text = self.say(result)
        self.assertIn('hiểu nhầm', text)
        self.assertNotIn('?', text)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.state()['purchase_intent'], 'unknown')
        _, result = self.turn('ừ')
        self.assertNotIn('?', result['response_text'] or '')
        self.assertEqual(self.pending(), [])

    def test_owner_who_wants_to_switch_can_still_be_asked(self):
        _, result = self.turn('có xe rồi nhưng muốn đổi')
        self.say(result)
        self.assertEqual((self.state()['ownership_status'], self.state()['purchase_intent']),
                         ('owns_vehicle', 'exploring'))
        self.assertEqual(result['response_text'].count('?'), 1)


if __name__ == '__main__':
    unittest.main()
