"""Offline Graph boundary checks; transport is always replaced."""
import http.client
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from meta_services.graph import MetaGraphClient, MetaGraphError, NoRedirect


class MetaGraphTests(unittest.TestCase):
    def setUp(self):
        self.client = MetaGraphClient('v25.0', '123', 'private-test-token')
        self.transport = patch('urllib.request.OpenerDirector.open')
        self.open = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.open.return_value = io.BytesIO(b'{"id":"456"}')

    def test_env_configuration_and_no_secret_repr(self):
        with patch.dict(os.environ, {'META_GRAPH_API_VERSION': 'v25.0',
                                    'META_PAGE_ID': '123',
                                    'META_PAGE_ACCESS_TOKEN': 'private-test-token'}, clear=True):
            client = MetaGraphClient.from_env()
            self.assertEqual(client.page_id, '123')
            self.assertNotIn('private-test-token', repr(client))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                MetaGraphClient.from_env()

    def test_posts_use_page_and_return_only_safe_paging(self):
        self.open.return_value = io.BytesIO(json.dumps({
            'data': [{'id': '123_456', 'message': 'Hello'}],
            'paging': {'cursors': {'after': 'abc', 'before': 'xyz'},
                       'next': 'https://graph.facebook.com/?access_token=private-test-token'}
        }).encode())
        result = self.client.list_posts(limit=100, after='cursor+/=')
        request = self.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).path, '/v25.0/123/posts')
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query['limit'], ['100'])
        self.assertEqual(query['after'], ['cursor+/='])
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-test-token')
        self.assertNotIn('private-test-token', request.full_url)
        self.assertEqual(result['paging'], {'cursors': {'after': 'abc', 'before': 'xyz'}, 'has_next': True})

    def test_get_post_fetches_canonical_context(self):
        self.open.return_value = io.BytesIO(b'{"id":"123_456","message":"Canonical post",'
            b'"created_time":"2026-10-26T00:00:00+0000",'
            b'"permalink_url":"https://www.facebook.com/123/posts/456","from":{"id":"123"}}')
        result = self.client.get_post('123_456')
        self.assertEqual(result['message'], 'Canonical post')
        self.assertEqual(result['from'], {'id': '123'})
        request = self.open.call_args.args[0]
        self.assertEqual(request.method, 'GET')
        self.assertEqual(urlsplit(request.full_url).path, '/v25.0/123_456')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)['fields'],
                         ['id,message,created_time,permalink_url,from{id}'])
        with self.assertRaises(ValueError):
            self.client.get_post('../me')
        self.assertEqual(self.open.call_count, 1)

    def test_comments_and_parent_metadata(self):
        self.client.list_comments('123_456')
        self.assertEqual(urlsplit(self.open.call_args.args[0].full_url).path, '/v25.0/123_456/comments')
        self.open.return_value = io.BytesIO(b'{"id":"456_789","object":{"id":"123_456","from":{"id":"123"}}}')
        self.assertEqual(self.client.get_comment('456_789')['object']['from']['id'], '123')
        fields = parse_qs(urlsplit(self.open.call_args.args[0].full_url).query)['fields'][0]
        self.assertIn('object{id,from{id}}', fields)
        self.assertIn('parent', fields)

    def test_write_paths_and_form_encoding(self):
        self.assertEqual(self.client.reply_to_comment('456_789', 'Xin chào & thanks'), {'id': '456'})
        request = self.open.call_args.args[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(urlsplit(request.full_url).path, '/v25.0/456_789/comments')
        self.assertEqual(parse_qs(request.data.decode()), {'message': ['Xin chào & thanks']})
        self.open.return_value = io.BytesIO(b'{"id":"123_999"}')
        self.client.publish_post('Hello', 'https://example.com/story')
        request = self.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).path, '/v25.0/123/feed')
        self.assertEqual(parse_qs(request.data.decode())['link'], ['https://example.com/story'])

    def test_invalid_input_never_reaches_transport(self):
        for limit in (0, 101, True, '25', 2.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.client.list_posts(limit=limit)
        for identifier in ('', '../me', '123?fields=token', 'abc', '12/34'):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.client.get_comment(identifier)
        for message in ('', '   ', None):
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.client.publish_post(message)
        for link in ('http://example.com', 'https://user:password@example.com', 'javascript:alert(1)'):
            with self.subTest(link=link), self.assertRaises(ValueError):
                self.client.publish_post('Hi', link)
        self.assertEqual(self.open.call_count, 0)

    def test_http_error_is_sanitized_and_not_retried(self):
        self.open.side_effect = urllib.error.HTTPError('https://graph.facebook.com/', 400,
            'private-test-token', {}, io.BytesIO(b'{"error":{"message":"private-test-token","code":190}}'))
        with self.assertRaises(MetaGraphError) as raised:
            self.client.publish_post('Hello')
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(raised.exception.code, 190)
        self.assertFalse(raised.exception.outcome_unknown)
        self.assertNotIn('private-test-token', str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.open.call_count, 1)

    def test_transport_failure_and_invalid_response_make_write_outcome_unknown(self):
        for error in (urllib.error.URLError('private-test-token'), TimeoutError('private-test-token'),
                      http.client.IncompleteRead(b'private-test-token')):
            self.open.side_effect = error
            with self.assertRaises(MetaGraphError) as raised:
                self.client.reply_to_comment('456_789', 'Hello')
            self.assertTrue(raised.exception.outcome_unknown)
            self.assertNotIn('private-test-token', str(raised.exception))
        self.assertEqual(self.open.call_count, 3)
        self.open.side_effect = None
        self.open.return_value = io.BytesIO(b'not JSON private-test-token')
        with self.assertRaises(MetaGraphError) as raised:
            self.client.publish_post('Hello')
        self.assertTrue(raised.exception.outcome_unknown)

    def test_write_without_confirmation_is_unknown(self):
        self.open.return_value = io.BytesIO(b'{"success":true}')
        with self.assertRaises(MetaGraphError) as raised:
            self.client.publish_post('Hello')
        self.assertTrue(raised.exception.outcome_unknown)

    def test_redirected_write_is_an_uncertain_outcome(self):
        self.open.side_effect = urllib.error.HTTPError('https://graph.facebook.com/', 302,
            'redirect', {}, io.BytesIO(b''))
        with self.assertRaises(MetaGraphError) as raised:
            self.client.publish_post('Hello')
        self.assertTrue(raised.exception.outcome_unknown)

    def test_graph_error_in_success_response_is_rejected(self):
        self.open.return_value = io.BytesIO(b'{"error":{"message":"private-test-token","code":200}}')
        with self.assertRaises(MetaGraphError) as raised:
            self.client.publish_post('Hello')
        self.assertEqual(raised.exception.code, 200)
        self.assertFalse(raised.exception.outcome_unknown)
        self.assertNotIn('private-test-token', str(raised.exception))

    def test_redirect_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError):
            NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'http://example.com')


if __name__ == '__main__':
    unittest.main()
