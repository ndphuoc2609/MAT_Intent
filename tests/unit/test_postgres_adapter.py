import unittest

from app.pg_adapter import _adapt_sql


class PostgresSqlCompatibilityTests(unittest.TestCase):
    def test_translates_existing_state_sql(self):
        self.assertEqual(_adapt_sql("BEGIN IMMEDIATE"), "BEGIN")
        self.assertIn("ON CONFLICT DO NOTHING", _adapt_sql(
            "INSERT OR IGNORE INTO events(dedupe_key) VALUES (?)"))
        self.assertEqual(_adapt_sql(
            "SELECT json_extract(o.payload,'$.comment_id') FROM outbox o ORDER BY rowid DESC"),
            "SELECT (o.payload::jsonb ->> 'comment_id') FROM outbox o ORDER BY seq DESC")

