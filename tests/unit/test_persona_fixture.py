import sqlite3
import unittest

from app.persona_store import PersonaStore, migrate_sqlite
from app.persona_fixture import seed_nam_creta


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


if __name__ == "__main__":
    unittest.main()
