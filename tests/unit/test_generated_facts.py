import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from app.persona_fact_policy import validate_proposals
from app.persona_fixture import seed_nam_creta
from app.persona_store import PersonaStore, SQLITE_MIGRATIONS, migrate_sqlite


def proposal(namespace='trip', slug='weekend_route', value='Cuối tuần mình thường về quê', precision='approximate', proposal_id='new1'):
    return {'proposal_id': proposal_id, 'namespace': namespace, 'slug': slug,
            'value': value, 'precision': precision}


class GeneratedFactPolicyTests(unittest.TestCase):
    def test_accepts_limited_story_fact_and_builds_canonical_key(self):
        accepted, reason = validate_proposals([proposal()], today=date(2026, 10, 30))
        self.assertIsNone(reason)
        self.assertEqual(accepted[0]['key'], 'gen.trip.weekend_route')

    def test_rejects_unknown_namespace_slug_or_extra_field(self):
        for value in (proposal(namespace='price'), proposal(slug='Bad Slug'),
                      {**proposal(), 'source': 'customer'}):
            with self.subTest(value=value):
                self.assertEqual(validate_proposals([value])[1], 'GENERATED_FACT_SHAPE')

    def test_rejects_business_contact_and_sensitive_details(self):
        cases = [
            (proposal(value='Giá xe khoảng 700 triệu'), 'GENERATED_FACT_PRODUCT'),
            (proposal(value='Số điện thoại 0987654321'), 'GENERATED_FACT_CONTACT'),
            (proposal(value='Mình đang điều trị ung thư'), 'GENERATED_FACT_SENSITIVE'),
        ]
        for item, expected in cases:
            with self.subTest(reason=expected):
                self.assertEqual(validate_proposals([item])[1], expected)

    def test_rejects_future_year_and_car_year_before_purchase(self):
        future = proposal(namespace='trip', value='Mình đi chuyến này năm 2027')
        older_car = proposal(namespace='car', slug='first_car', value='Mình mua xe này năm 2018')
        self.assertEqual(validate_proposals([future], today=date(2026, 10, 30))[1], 'GENERATED_FACT_TIMELINE')
        self.assertEqual(validate_proposals([older_car], anchors={'purchase_year': 2020},
                                            today=date(2026, 10, 30))[1], 'GENERATED_FACT_TIMELINE')

    def test_rejects_duplicate_live_key_and_turn_or_daily_limits(self):
        self.assertEqual(validate_proposals([proposal()], live_keys={'gen.trip.weekend_route'})[1],
                         'GENERATED_FACT_EXISTS')
        self.assertEqual(validate_proposals([proposal(proposal_id='new1'), proposal(proposal_id='new2')],
                                            created_today=19)[1], 'GENERATED_FACT_RATE_LIMIT')
        over_turn = [proposal(slug='route_one', proposal_id='new1'), proposal(slug='route_two', proposal_id='new2'),
                     proposal(slug='route_three', proposal_id='new1')]
        self.assertEqual(validate_proposals(over_turn)[1], 'GENERATED_FACT_LIMIT')

    def test_rejects_fact_lifted_from_customer_question(self):
        item = proposal(namespace='car', slug='purchase_year', value='Mình mua xe năm 2020')
        accepted, reason = validate_proposals([item], source_text='Bạn mua xe năm 2020 à?')
        self.assertIsNone(accepted)
        self.assertEqual(reason, 'GENERATED_FACT_FROM_CUSTOMER')

    def test_rejects_paraphrase_from_any_visible_customer_turn(self):
        item = proposal(value='Cuối tuần mình hay về quê')
        accepted, reason = validate_proposals([item],
            source_text=['Bạn hay đi xe gì?', 'Cuối tuần bạn thường về quê à?'])
        self.assertIsNone(accepted)
        self.assertEqual(reason, 'GENERATED_FACT_FROM_CUSTOMER')


class GeneratedFactStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        migrate_sqlite(self.db)
        self.store = PersonaStore(self.db)
        self.persona, self.release = seed_nam_creta(self.store)
        self.store.bind_post('page', 'post', self.persona, self.release)

    def tearDown(self):
        self.db.close()

    def test_migration_010_is_idempotent_and_does_not_change_release_checksum(self):
        checksum = self.store.get_release_snapshot(self.persona, self.release)['release']['checksum']
        migration = next(path for version, path in SQLITE_MIGRATIONS if version == 10)
        self.db.executescript(migration.read_text(encoding='utf-8'))
        migrate_sqlite(self.db)
        self.assertEqual(self.store.get_release_snapshot(self.persona, self.release)['release']['checksum'], checksum)
        self.assertEqual(self.db.execute('SELECT count(*) FROM persona_generated_facts').fetchone()[0], 0)

    def test_migration_011_is_idempotent_and_adds_memory_reservations(self):
        migration = next(path for version, path in SQLITE_MIGRATIONS if version == 11)
        self.db.executescript(migration.read_text(encoding='utf-8'))
        migrate_sqlite(self.db)
        self.assertIsNotNone(self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona_memory_reservations'").fetchone())

    def test_pending_fact_is_private_to_source_comment_until_activated(self):
        fact_id = self.store.propose_generated(self.persona, 'gen.trip.weekend_route',
            'Cuối tuần mình thường về quê', 'approximate', 'luna_generated:c1', 'c1', '2026-10-30T10:00:00+00:00')
        base = self.store.get_binding_snapshot('page', 'post')
        self.assertNotIn(fact_id, {fact['fact_id'] for fact in self.store.attach_generated(base, 'c2')['facts']})
        self.assertIn(fact_id, {fact['fact_id'] for fact in self.store.attach_generated(base, 'c1')['facts']})
        self.store.activate_generated('c1', [fact_id], '2026-10-30T10:00:01+00:00')
        self.assertIn(fact_id, {fact['fact_id'] for fact in self.store.attach_generated(base, 'c2')['facts']})
        self.assertEqual(self.store.list_generated(self.persona)[0]['status'], 'ACTIVE')

    def test_unique_live_key_race_is_constraint_error_without_overwriting_winner(self):
        winner = self.store.propose_generated(self.persona, 'gen.trip.weekend_route', 'Giá trị thứ nhất',
            'exact', 'luna_generated:c1', 'c1', '2026-10-30T10:00:00+00:00')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.propose_generated(self.persona, 'gen.trip.weekend_route', 'Giá trị thứ hai',
                'exact', 'luna_generated:c2', 'c2', '2026-10-30T10:00:01+00:00')
        self.assertEqual(self.store.generated_rows(self.persona, 'c1')[0]['gen_fact_id'], winner)
        self.assertFalse(self.db.in_transaction)

    def test_generated_fact_namespace_is_isolated_by_persona_and_revocation(self):
        from app.persona_fixture import seed_database_persona
        second_persona, second_release = seed_database_persona(self.store, {
            'id': 'real-person-27', 'name': 'Linh', 'occupation': 'Giáo viên',
            'car_owned': 'Hyundai Tucson'})
        self.store.bind_post('page', 'other-post', second_persona, second_release)
        first_id = self.store.propose_generated(self.persona, 'gen.trip.weekend_route',
            'Persona A hay về Nam Định', 'approximate', 'luna_generated:a', 'a', '2026-10-30T10:00:00+00:00')
        second_id = self.store.propose_generated(second_persona, 'gen.trip.weekend_route',
            'Persona B hay đi Đà Lạt', 'approximate', 'luna_generated:b', 'b', '2026-10-30T10:00:00+00:00')
        self.store.activate_generated('a', [first_id], '2026-10-30T10:00:01+00:00')
        self.store.activate_generated('b', [second_id], '2026-10-30T10:00:01+00:00')
        first = self.store.attach_generated(self.store.get_binding_snapshot('page', 'post'))
        second = self.store.attach_generated(self.store.get_binding_snapshot('page', 'other-post'))
        self.assertEqual({fact['fact_id'] for fact in first['facts'] if fact.get('generated')}, {first_id})
        self.assertEqual({fact['fact_id'] for fact in second['facts'] if fact.get('generated')}, {second_id})
        self.store.revoke_generated(first_id, 'remove A story', 'operator')
        first = self.store.attach_generated(self.store.get_binding_snapshot('page', 'post'))
        second = self.store.attach_generated(self.store.get_binding_snapshot('page', 'other-post'))
        self.assertFalse(any(fact.get('generated') for fact in first['facts']))
        self.assertEqual({fact['fact_id'] for fact in second['facts'] if fact.get('generated')}, {second_id})

    def test_discard_ttl_and_revoke_remove_fact_from_attach(self):
        fact_id = self.store.propose_generated(self.persona, 'gen.trip.weekend_route', 'Cuối tuần mình thường về quê',
            'approximate', 'luna_generated:c1', 'c1', '2026-10-30T10:00:00+00:00')
        base = self.store.get_binding_snapshot('page', 'post')
        self.store.purge_generated(self.persona, '2026-10-30T10:31:00+00:00', 30)
        self.assertEqual(self.store.list_generated(self.persona), [])
        self.assertNotIn(fact_id, {fact['fact_id'] for fact in self.store.attach_generated(base, 'c1')['facts']})
        live = self.store.propose_generated(self.persona, 'gen.trip.weekend_route', 'Cuối tuần mình thường về quê',
            'approximate', 'luna_generated:c2', 'c2', '2026-10-30T10:32:00+00:00')
        self.store.activate_generated('c2', [live], '2026-10-30T10:32:01+00:00')
        self.assertTrue(self.store.revoke_generated(live, 'model made up story', 'sandbox-operator'))
        self.assertNotIn(live, {fact['fact_id'] for fact in self.store.attach_generated(base)['facts']})
        self.assertEqual(self.store.list_generated(self.persona)[0]['status'], 'REVOKED')

    def test_memory_reservation_is_atomic_across_sqlite_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            first_db = sqlite3.connect(f'{directory}/shared.db', check_same_thread=False, timeout=5)
            migrate_sqlite(first_db)
            first = PersonaStore(first_db)
            persona, release = seed_nam_creta(first)
            memory_id = first.get_release_snapshot(persona, release)['memories'][0]['memory_id']
            second = PersonaStore(sqlite3.connect(f'{directory}/shared.db', check_same_thread=False, timeout=5))
            scope = dict(page_id='page', post_id='post', thread_root_id='root', actor_id='actor')
            barrier = threading.Barrier(2)

            def reserve(store, dedupe):
                barrier.wait()
                return store.reserve_memory_delivery(scope, [memory_id], dedupe, 1,
                    '2026-10-30T00:00:00+00:00')

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda item: reserve(*item),
                    ((first, 'worker-a:reply'), (second, 'worker-b:reply'))))
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(len(first.reserved_memory_ids(scope)), 1)
            first.conn.close()
            second.conn.close()


if __name__ == '__main__':
    unittest.main()
