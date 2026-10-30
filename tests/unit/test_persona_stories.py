import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.persona_fixture import seed_database_persona, seed_nam_creta
from app.persona_store import PersonaStore, migrate_sqlite
from app.persona_stories import StoryStore


class StoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'stories.sqlite')
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        migrate_sqlite(self.conn)
        self.repo = PersonaStore(self.conn)
        self.persona, self.release = seed_nam_creta(self.repo)
        self.repo.bind_post('page', 'post', self.persona, self.release)
        self.snap = self.repo.get_binding_snapshot('page', 'post')
        self.vehicle = next(f['fact_id'] for f in self.snap['facts'] if f['key'] == 'vehicle.current.model')
        self.stories = StoryStore(self.repo)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def proposal(self, key='request-1', event='Mình đi Đà Lạt bằng Creta'):
        return {'proposal_id': 'new1', 'idempotency_key': key, 'persona_version': self.release,
            'topic': 'chuyến đi', 'event': event, 'time_start': '2025-04-01', 'time_end': None,
            'entities': {'vehicle': 'Creta', 'location': 'Đà Lạt', 'people': []},
            'vehicle_use': 'owned', 'base_fact_ids': [self.vehicle], 'new_details': [], 'relation': None}

    def test_commit_before_use_idempotency_and_revoke(self):
        p = self.proposal()
        accepted = self.stories.propose(self.snap, p)
        self.assertEqual(accepted['status'], 'ACCEPTED', accepted)
        self.assertEqual(accepted['memory']['label'], 'fictional_generated')
        self.assertEqual(self.stories.propose(self.snap, p), accepted)
        self.assertEqual(self.stories.propose(self.snap, dict(p, event='Mình đi Hà Nội'))['status'], 'CONFLICT')
        mid = accepted['memory_id']
        self.assertEqual(self.stories.get(self.snap, mid)['event'], p['event'])
        self.assertTrue(self.stories.revoke(self.snap, mid, 'thu hồi'))
        self.assertIsNone(self.stories.get(self.snap, mid))
        self.assertEqual(self.stories.retrieve(self.snap, topic='chuyến đi')['status'], 'NOT_FOUND')

    def test_paraphrase_reuses_id_when_structure_and_semantic_agree(self):
        first = self.stories.propose(self.snap, self.proposal())
        second = self.stories.propose(self.snap, self.proposal('another', 'Mình lái Creta tới Đà Lạt'),
                                      judge=lambda new, old: 'SAME')
        self.assertEqual(second['status'], 'EXISTING', second)
        self.assertEqual(second['memory_id'], first['memory_id'])
        other_topic = self.proposal('different-topic', 'Mình lái Creta tới Đà Lạt')
        other_topic['topic'] = 'du lịch'
        third = self.stories.propose(self.snap, other_topic, judge=lambda *_: 'SAME')
        self.assertEqual(third['memory_id'], first['memory_id'])
        uncertain = self.proposal('uncertain', 'Mình tới Đà Lạt cuối tuần')
        uncertain['time_start'] = None
        self.assertEqual(self.stories.propose(self.snap, uncertain, judge=lambda *_: 'SAME')['status'], 'AMBIGUOUS')

    def test_semantic_story_proposal_commits_through_caller_mutation_guard(self):
        original = self.stories.propose(self.snap, self.proposal())
        seen = []

        def read_guard(operation):
            seen.append('read-start')
            result = operation()
            seen.append('read-end')
            return result

        def mutation_guard(operation):
            seen.append('commit')
            return operation()

        def judge(*_):
            seen.append('judge')
            return 'SAME'

        result = self.stories.propose(self.snap,
            self.proposal('guarded-retell', 'Mình lái Creta tới Đà Lạt'),
            judge=judge, read_guard=read_guard, mutation_guard=mutation_guard)

        self.assertEqual(result['status'], 'EXISTING', result)
        self.assertEqual(result['memory_id'], original['memory_id'])
        self.assertEqual(seen, ['read-start', 'read-end', 'read-start', 'read-end', 'judge', 'commit'])

    def test_addendum_appends_revision_and_reuses_event_id(self):
        original = self.stories.propose(self.snap, self.proposal())
        self.assertEqual(original['status'], 'ACCEPTED', original)
        addition = self.proposal('addendum')
        addition['new_details'] = ['Trong chuyến đó xe bị ngập']
        addition['relation'] = {'type': 'addendum', 'memory_id': original['memory_id'], 'base_version': 1}
        result = self.stories.propose(self.snap, addition, judge=lambda *_: 'ADDENDUM')
        self.assertEqual(result['status'], 'ADDENDUM', result)
        self.assertEqual(result['memory_id'], original['memory_id'])
        self.assertEqual(result['memory']['version'], 2)
        self.assertIn('Trong chuyến đó xe bị ngập', result['memory']['new_details'])
        self.assertEqual(len(result['memory']['revisions']), 1)
        self.assertEqual(result['memory']['revisions'][0]['base_version'], 1)
        self.assertEqual(self.stories.get(self.snap, original['memory_id'])['version'], 2)

    def test_stale_or_conflicting_addendum_does_not_change_canonical_story(self):
        original = self.stories.propose(self.snap, self.proposal())
        addition = self.proposal('stale')
        addition['new_details'] = ['Xe bị ngập']
        addition['relation'] = {'type': 'addendum', 'memory_id': original['memory_id'], 'base_version': 0}
        self.assertEqual(self.stories.propose(self.snap, addition, judge=lambda *_: 'ADDENDUM')['status'], 'CONFLICT')
        addition['idempotency_key'] = 'conflicting'
        addition['relation']['base_version'] = 1
        self.assertEqual(self.stories.propose(self.snap, addition, judge=lambda *_: 'CONFLICT')['status'], 'CONFLICT')
        current = self.stories.get(self.snap, original['memory_id'])
        self.assertEqual(current['version'], 1)
        self.assertEqual(current['new_details'], [])

    def test_story_claim_rejects_unsaved_clause_and_accepts_saved_addendum(self):
        from app.persona_runtime import _story_claim_compatible
        story = self.stories.propose(self.snap, self.proposal())['memory']
        text = 'Mình đi Đà Lạt bằng Creta bị ngập.'
        self.assertFalse(_story_claim_compatible(text, story))
        story['new_details'] = ['Trong chuyến đó xe bị ngập']
        self.assertTrue(_story_claim_compatible(text, story))

    def test_purchase_month_rejects_owned_event_before_purchase_month(self):
        proposal = self.proposal()
        proposal['time_start'] = '2024-01-01'
        self.assertEqual(self.stories.propose(self.snap, proposal)['status'], 'CONFLICT')

    def test_pre_purchase_test_drive_without_evidence_is_ambiguous(self):
        proposal = self.proposal()
        proposal['time_start'] = '2024-01-01'
        proposal['vehicle_use'] = 'test_drive'
        self.assertEqual(self.stories.propose(self.snap, proposal)['status'], 'AMBIGUOUS')

    def test_explicit_retell_reuses_story_when_known_time_and_entities_match(self):
        first = self.stories.propose(self.snap, self.proposal())
        retell = self.proposal('retell-1', 'Cuối tuần đó mình lái chiếc Hyundai Creta lên Đà Lạt')
        retell['entities']['vehicle'] = 'Hyundai Creta'
        retell['relation'] = {'type': 'retell', 'memory_id': first['memory_id']}
        result = self.stories.propose(self.snap, retell)
        self.assertEqual(result['status'], 'EXISTING', result)
        self.assertEqual(result['memory_id'], first['memory_id'])

    def test_conflicts_dates_people_and_unavailable_retrieval(self):
        p = self.proposal(); p['time_start'] = '2022-01-01'
        self.assertEqual(self.stories.propose(self.snap, p)['status'], 'CONFLICT')
        p = self.proposal('family', 'Vợ mình thích chuyến đi Đà Lạt')
        self.assertEqual(self.stories.propose(self.snap, p)['status'], 'REJECTED')
        p = self.proposal('customer', 'Mình đi Đà Lạt bằng Creta')
        self.assertEqual(self.stories.propose(self.snap, p, customer_texts=['Bạn đi Đà Lạt bằng Creta à?'])['status'], 'CONFLICT')
        self.conn.execute('DROP TABLE persona_stories')
        self.assertEqual(self.stories.retrieve(self.snap, topic='x')['status'], 'UNKNOWN')

    def test_personas_are_isolated_and_revoke_is_scoped(self):
        first = self.stories.propose(self.snap, self.proposal())
        person, release = seed_database_persona(self.repo, {'id':'isolated-linh', 'name':'Linh', 'car_owned':'Hyundai Tucson'})
        self.repo.bind_post('page', 'other', person, release)
        other = self.repo.get_binding_snapshot('page', 'other')
        self.assertIsNone(self.stories.get(other, first['memory_id']))
        self.assertFalse(self.stories.revoke(other, first['memory_id'], 'wrong persona'))
        self.assertEqual(self.stories.retrieve(other)['status'], 'NOT_FOUND')
        self.assertIsNotNone(self.stories.get(self.snap, first['memory_id']))

    def test_delivery_only_after_sent_is_idempotent_and_scope_checked(self):
        result = self.stories.propose(self.snap, self.proposal()); mid = result['memory_id']
        scope = dict(page_id='page', post_id='post', thread_root_id='root', actor_id='alice')
        self.assertFalse(self.stories.record_delivery(self.snap, scope, 'send-1', [mid], status='FAILED'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM persona_story_deliveries').fetchone()[0], 0)
        self.assertTrue(self.stories.reserve(self.snap, scope, 'send-1', mid))
        self.assertTrue(self.stories.start_delivery(self.snap, 'send-1'))
        self.assertTrue(self.stories.record_delivery(self.snap, scope, 'send-1', [mid], status='SENT'))
        self.assertTrue(self.stories.record_delivery(self.snap, scope, 'send-1', [mid], status='SENT'))
        self.assertFalse(self.stories.reserve(self.snap, scope, 'send-2', mid))
        self.assertEqual(self.stories.told(self.snap, scope)['deliveries'][0]['memory_id'], mid)
        self.assertEqual(self.stories.told(self.snap, dict(scope, actor_id='bob'))['status'], 'NOT_FOUND')
        self.assertFalse(self.stories.record_delivery(self.snap, dict(scope, actor_id='bob'), 'send-1', [mid], status='SENT'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM persona_story_deliveries').fetchone()[0], 1)

    def test_sent_after_revocation_still_records_and_unknown_keeps_reservation(self):
        mid = self.stories.propose(self.snap, self.proposal())['memory_id']
        scope = dict(page_id='page', post_id='post', thread_root_id='root', actor_id='alice')
        self.assertTrue(self.stories.reserve(self.snap, scope, 'send-1', mid))
        self.assertTrue(self.stories.start_delivery(self.snap, 'send-1'))
        self.assertFalse(self.stories.release_reservation(self.snap, 'send-1', delivery_status='UNKNOWN'))
        self.assertFalse(self.stories.reserve(self.snap, scope, 'send-2', mid))
        self.assertTrue(self.stories.revoke(self.snap, mid, 'withdrawn'))
        self.assertTrue(self.stories.record_delivery(self.snap, scope, 'send-1', [mid], status='SENT'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM persona_story_deliveries').fetchone()[0], 1)
        self.assertIsNone(self.stories.get(self.snap, mid))

    def test_database_enforces_story_immutability(self):
        mid = self.stories.propose(self.snap, self.proposal())['memory_id']
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('UPDATE persona_stories SET event_text=? WHERE memory_id=?', ('changed', mid))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('DELETE FROM persona_stories WHERE memory_id=?', (mid,))

    def test_concurrent_different_wording_cannot_create_two_stories(self):
        second_conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        second_repo = PersonaStore(second_conn)
        barrier = threading.Barrier(2)
        p1, p2 = self.proposal('race-1'), self.proposal('race-2', 'Mình lái Creta tới Đà Lạt')
        def run(pair):
            repo, proposal = pair
            barrier.wait()
            return StoryStore(repo).propose(self.snap, proposal, judge=lambda *_: 'SAME')
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run, [(self.repo, p1), (second_repo, p2)]))
            self.assertEqual(sorted(x['status'] for x in results), ['ACCEPTED', 'EXISTING'], results)
            self.assertEqual(results[0]['memory_id'], results[1]['memory_id'])
            self.assertEqual(self.conn.execute('SELECT count(*) FROM persona_stories').fetchone()[0], 1)
        finally:
            second_conn.close()


if __name__ == '__main__':
    unittest.main()
