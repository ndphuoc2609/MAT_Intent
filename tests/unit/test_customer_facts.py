import sqlite3
import unittest

from app.customer_facts import list_customer_facts, record_customer_facts
from app.persona_store import PersonaStore, migrate_sqlite


class CustomerFactTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        migrate_sqlite(self.conn)
        self.repo = PersonaStore(self.conn)
        self.event = {'page_id': 'page-a', 'post_id': 'post-a', 'thread_root_id': 'root-a',
            'actor_id': 'customer-a', 'comment_id': 'comment-a', 'text': 'Mình đang đi Hyundai Creta.'}

    def tearDown(self):
        self.conn.close()

    def test_known_vehicle_alias_keeps_original_evidence(self):
        fact = {'predicate': 'current_vehicle', 'value': 'Creta',
            'evidence_excerpt': 'Hyundai Creta', 'subject': 'customer', 'certainty': 'explicit'}
        self.assertEqual(record_customer_facts(self.repo, self.event, [fact], 'now')['status'], 'RECORDED')
        saved = list_customer_facts(self.repo, self.event)[0]
        self.assertEqual(saved['value'], 'Creta')
        self.assertEqual(saved['evidence'], 'Hyundai Creta')

    def test_proxy_report_is_not_saved_as_current_customer_fact(self):
        event = dict(self.event, text='Hỏi hộ chị mình, chị ấy đang đi Creta.')
        fact = {'predicate': 'current_vehicle', 'value': 'Creta',
            'evidence_excerpt': 'đang đi Creta', 'subject': 'customer', 'certainty': 'explicit'}
        self.assertEqual(record_customer_facts(self.repo, event, [fact], 'now')['reason'], 'SUBJECT_UNCLEAR')
        self.assertEqual(list_customer_facts(self.repo, event), [])

    def test_facts_are_isolated_by_customer_and_thread(self):
        fact = {'predicate': 'current_vehicle', 'value': 'Hyundai Creta',
            'evidence_excerpt': 'Hyundai Creta', 'subject': 'customer', 'certainty': 'explicit'}
        record_customer_facts(self.repo, self.event, [fact], 'now')
        self.assertEqual(list_customer_facts(self.repo, dict(self.event, actor_id='customer-b')), [])
        self.assertEqual(list_customer_facts(self.repo, dict(self.event, thread_root_id='root-b')), [])


if __name__ == '__main__':
    unittest.main()
