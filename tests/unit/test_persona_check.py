import unittest

from scripts.persona_check import ACCEPTANCE_GAPS, sqlite_preflight


class PersonaCheckTests(unittest.TestCase):
    def test_sqlite_preflight_keeps_fixture_unapproved_and_gaps_explicit(self):
        sqlite_preflight()
        self.assertIn("C01-C12", ACCEPTANCE_GAPS)
        self.assertIn("probes_01_20", ACCEPTANCE_GAPS)


if __name__ == "__main__":
    unittest.main()
