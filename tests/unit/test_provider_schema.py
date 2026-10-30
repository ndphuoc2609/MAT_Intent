"""Strict provider schemas must not put null in string-only enum branches."""
import unittest
from app.semantics import semantic_model_json_schema


class ProviderSchemaTests(unittest.TestCase):
    def test_nullable_category_enum_matches_each_branch_type(self):
        branches = semantic_model_json_schema('2')['properties']['category']['anyOf']
        string = next(b for b in branches if b.get('type') == 'string')
        null = next(b for b in branches if b.get('type') == 'null')
        self.assertTrue(all(isinstance(v, str) for v in string['enum']))
        self.assertEqual(null['enum'], [None])
