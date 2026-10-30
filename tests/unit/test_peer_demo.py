import unittest
from app.harness import make_engine
from app.persona_store import PersonaStore, migrate_sqlite
from app.persona_fixture import seed_nam_creta
from tests.unit.test_persona_engine import _social_semantic


class PeerDemoTests(unittest.TestCase):
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

    def turn(self, text, semantic=None):
        self.counter += 1
        event = dict(page_id='p', post_id='post', thread_root_id='root', actor_id='a',
            comment_id=f'm{self.counter}', text=text, actor_role='user', event_type='create')
        return event, self.engine.process(event, self.post, semantic or _social_semantic(text))

    def test_decline_and_optout_stop_questions(self):
        self.turn('Toi cũng tính mua')
        event, result = self.turn('đừng hỏi')
        self.assertEqual(self.engine.store.lead_profile(event)['interested_vehicle']['status'], 'DECLINED')
        self.assertNotIn('?', result['response_text'])
        self.engine.store.block('p','a','public_reply'); self.engine.store.conn.commit()
        _, result = self.turn('Bác đi màu gì')
        self.assertFalse(result['outbound_permission'])

    def test_deposit_has_peer_draft_and_handoff_lock(self):
        text = 'Cho mình đặt cọc Creta'
        sem = dict(_social_semantic(text), requests=[dict(topic='buy',desired_outcome='buy',polarity='positive')],
            operations=[dict(op='START_REQUEST',request_ref='topic:buy',evidence_excerpt=text)])
        _, result = self.turn(text, sem)
        self.assertIn('HANDOFF_DRAFT_ONLY', result['routing'])
        self.assertIn('đại lý', result['response_text'])
        self.assertNotIn('xác nhận việc mua', result['response_text'])
        _, result = self.turn('Bác đi màu gì')
        self.assertFalse(result['outbound_permission'])

    def test_model_answer_pending_and_unanswered_limit(self):
        event, _ = self.turn('Toi cũng tính mua')
        pending = self.engine.store.open_pending_for('a', page_id='p', post_id='post', thread_root_id='root')[0]
        text = 'Creta'
        sem = dict(_social_semantic(text), operations=[dict(op='ANSWER_PENDING', request_ref=pending['request_id'],
            pending_id=pending['id'], evidence_excerpt=text, slot=dict(kind='interested_vehicle',value='Creta',explicit=True))])
        event, result = self.turn(text, sem)
        self.assertTrue(result['outbound_permission'], result)
        self.assertEqual(self.engine.store.lead_profile(event)['interested_vehicle']['value'], 'Creta')
        self.assertIn('khi nào', result['response_text'])
        pending = self.engine.store.open_pending_for('a', page_id='p', post_id='post', thread_root_id='root')[0]
        text = 'tháng sau'
        sem = dict(_social_semantic(text), operations=[dict(op='ANSWER_PENDING', request_ref='topic:social',
            pending_id=pending['id'], evidence_excerpt=text, slot=dict(kind='purchase_timeline',value=text,explicit=True))])
        event, result = self.turn(text, sem)
        self.assertTrue(result['outbound_permission'], result)
        self.assertEqual(self.engine.store.lead_profile(event)['purchase_timeline']['value'], text)
        for _ in range(4):
            event, result = self.turn('mình vẫn đang xem thêm')
        # Not understood and nothing left to ask: no canned reply, no question.
        self.assertFalse(result['outbound_permission'])
        self.assertIsNone(result['response_text'])
        self.assertEqual(self.engine.store.open_pending_for('a', page_id='p', post_id='post', thread_root_id='root'), [])

    def test_sqlite_migration_is_additive(self):
        import sqlite3
        from pathlib import Path
        migration = Path('migrations/sqlite/007_lead_drafts.sql').read_text()
        with sqlite3.connect(':memory:') as conn:
            conn.execute('CREATE TABLE existing_data(value TEXT)')
            conn.execute("INSERT INTO existing_data VALUES ('keep')")
            conn.executescript(migration)
            conn.executescript(migration)
            self.assertEqual(conn.execute('SELECT value FROM existing_data').fetchone()[0], 'keep')

    def test_questions_and_lead_progress(self):
        engine = make_engine()
        self.addCleanup(engine.store.conn.close)
        self.addCleanup(engine.sender.receiver.conn.close)
        migrate_sqlite(engine.store.conn)
        repository = PersonaStore(engine.store)
        pid, release = seed_nam_creta(repository)
        repository.bind_post('p', 'post', pid, release)
        engine.persona_store = repository
        engine.allow_persona_fixtures = True
        post = dict(id='post', page_id='p', text='Creta', vehicles=['Creta'], persona_binding=True)
        for i, text in enumerate(('Bác đi màu gì', 'Toi cũng tính mua', 'Creta', 'tháng sau')):
            event = dict(page_id='p', post_id='post', thread_root_id='root', actor_id='a',
                         comment_id=f'c{i}', text=text, actor_role='user', event_type='create')
            result = engine.process(event, post, _social_semantic(text))
            self.assertTrue(result['outbound_permission'], result)
            # A question about Nam is answered without steering into a sales question;
            # asking only starts once the customer says they want to buy.
            self.assertEqual(result['response_text'].count('?'), 1 if i in (1, 2) else 0, result)
            self.assertNotIn('Cảm ơn bạn nhé', result['response_text'])
            if i == 0:
                self.assertIn('bác', result['response_text'].lower())
            if i == 2:
                profile = engine.store.lead_profile(event)
                self.assertEqual(profile['interested_vehicle']['value'], 'Creta')
                self.assertIn('purchase_timeline', [p['expected_slot'] for p in engine.store.open_pending_for(
                    'a', page_id='p', post_id='post', thread_root_id='root')])
