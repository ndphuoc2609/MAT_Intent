"""Small Page Graph API boundary; write requests are never retried."""
import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


class MetaGraphError(RuntimeError):
    """Sanitized failure; an unknown write outcome must not be blindly retried."""

    def __init__(self, message, *, status=None, code=None, outcome_unknown=False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.outcome_unknown = outcome_unknown


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(getattr(req, 'full_url', ''), code,
                                     'Graph redirect refused', headers, fp)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+(?:_[0-9]+)*', value) or len(value) > 100:
        raise ValueError('Graph ID must contain only digits and underscore-separated digits')
    return value


def _message(value):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('message must be nonempty text without NUL characters')
    return value


class MetaGraphClient:
    def __init__(self, api_version, page_id, access_token):
        if not isinstance(api_version, str) or not re.fullmatch(r'v[0-9]+\.[0-9]+', api_version):
            raise ValueError('META_GRAPH_API_VERSION must be an explicit version such as v25.0')
        self.api_version = api_version
        self.page_id = _identifier(page_id)
        if not isinstance(access_token, str) or not access_token or any(c.isspace() for c in access_token):
            raise ValueError('META_PAGE_ACCESS_TOKEN must be set without whitespace')
        self._access_token = access_token
        self._opener = urllib.request.build_opener(NoRedirect())

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('META_GRAPH_API_VERSION', ''),
                   os.environ.get('META_PAGE_ID', ''),
                   os.environ.get('META_PAGE_ACCESS_TOKEN', ''))

    def _request(self, path, params=None, *, method='GET'):
        params = params or {}
        url = f'https://graph.facebook.com/{self.api_version}/{path}'
        data = None
        if method == 'GET':
            url += '?' + urllib.parse.urlencode(params)
        else:
            data = urllib.parse.urlencode(params).encode('utf-8')
        request = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': f'Bearer {self._access_token}',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
        })
        status = None
        try:
            with self._opener.open(request, timeout=20) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            status = error.code
            code = None
            try:
                body = json.loads(error.read(65536))
                if isinstance(body, dict) and isinstance(body.get('error'), dict):
                    code = body['error'].get('code')
                    code = code if type(code) is int else None
            except (ValueError, OSError, http.client.HTTPException):
                pass
            finally:
                error.close()
            raise MetaGraphError('Meta Graph request rejected', status=status, code=code,
                                 outcome_unknown=method == 'POST' and (300 <= status < 400 or status >= 500)) from None
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
            raise MetaGraphError('Meta Graph transport or response failure',
                                 outcome_unknown=method == 'POST') from None
        if not isinstance(payload, dict):
            raise MetaGraphError('Invalid Meta Graph response', outcome_unknown=method == 'POST')
        if 'error' in payload:
            error = payload['error']
            code = error.get('code') if isinstance(error, dict) else None
            raise MetaGraphError('Meta Graph request rejected',
                                 code=code if type(code) is int else None)
        if method == 'POST' and not isinstance(payload.get('id'), str):
            raise MetaGraphError('Meta Graph write confirmation missing', outcome_unknown=True)
        if 'paging' in payload:
            paging = payload['paging']
            if not isinstance(paging, dict):
                raise MetaGraphError('Invalid Meta Graph pagination')
            cursors = paging.get('cursors', {})
            # Navigation URLs may contain the Page token; expose only cursor values.
            payload['paging'] = {'cursors': {
                key: value for key, value in cursors.items()
                if key in ('after', 'before') and isinstance(value, str)
            } if isinstance(cursors, dict) else {}, 'has_next': bool(paging.get('next'))}
        return payload

    def _pagination(self, limit, after):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be an integer between 1 and 100')
        params = {'limit': limit}
        if after is not None:
            if not isinstance(after, str) or not after or len(after) > 4096 or any(ord(c) < 32 for c in after):
                raise ValueError('after must be a nonempty cursor of at most 4096 characters')
            params['after'] = after
        return params

    def list_posts(self, limit=25, after=None):
        return self._request(f'{self.page_id}/posts', {
            **self._pagination(limit, after), 'fields': 'id,message,created_time,permalink_url,from{id,name}'})

    def get_post(self, post_id):
        return self._request(_identifier(post_id), {
            'fields': 'id,message,created_time,permalink_url,from{id}'})

    def list_comments(self, post_id, limit=25, after=None):
        return self._request(f'{_identifier(post_id)}/comments', {
            **self._pagination(limit, after), 'fields': 'id,message,created_time,from{id,name},parent{id}'})

    def get_comment(self, comment_id):
        return self._request(_identifier(comment_id), {
            'fields': 'id,message,created_time,from{id,name},parent{id},object{id,from{id}}'})

    def reply_to_comment(self, comment_id, message):
        return self._request(f'{_identifier(comment_id)}/comments',
                             {'message': _message(message)}, method='POST')

    def publish_post(self, message, link=None):
        params = {'message': _message(message)}
        if link is not None:
            if not isinstance(link, str):
                raise ValueError('link must be an HTTPS URL')
            try:
                parsed = urllib.parse.urlsplit(link)
                valid = parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
                valid = valid and not any(c.isspace() or ord(c) < 32 for c in link)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('link must be an HTTPS URL without credentials')
            params['link'] = link
        return self._request(f'{self.page_id}/feed', params, method='POST')
