import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

import app
import wsgi


class Auth(unittest.TestCase):
    # Test fixtures only; these are not deployment credentials.
    password = 'fixture-only-password-2026'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(app, 'DATA', Path(self.tmp.name))
        self.patch.start()
        self.server = wsgi.create_application('tester', self.password, secure=False)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def request(self, path='/', method='GET', body=b'', cookie='', origin='http://test.local', server=None, **headers):
        env = {}
        setup_testing_defaults(env)
        env.update(PATH_INFO=path, REQUEST_METHOD=method, HTTP_HOST='test.local',
                   CONTENT_LENGTH=str(len(body)), HTTP_COOKIE=cookie, HTTP_ORIGIN=origin,
                   **{'wsgi.input': io.BytesIO(body)}, **headers)
        result = {}
        def start(status, hs):
            result.update(code=int(status.split()[0]), headers=dict(hs))
        result['body'] = b''.join((server or self.server)(env, start))
        return result

    def login(self, password=None, origin='http://test.local', server=None):
        return self.request('/login', 'POST', urlencode({'username': 'tester',
                            'password': password or self.password}).encode(), origin=origin, server=server)

    def cookie(self):
        r = self.login()
        self.assertEqual(r['code'], 303)
        self.assertIn('HttpOnly', r['headers']['Set-Cookie'])
        self.assertIn('SameSite=Strict', r['headers']['Set-Cookie'])
        return r['headers']['Set-Cookie'].split(';')[0]

    def test_all_data_routes_require_login(self):
        for path in ['/', '/print/1', '/attachment/1', '/api/state']:
            r = self.request(path)
            self.assertIn(r['code'], [303, 401])
            self.assertNotIn(b'documents', r['body'])
        r = self.request('/api/action', 'POST', b'{}')
        self.assertEqual(r['code'], 401)
        self.assertEqual(self.request('/healthz')['code'], 200)

    def test_session_logout_and_secure_cookie(self):
        cookie = self.cookie()
        self.assertEqual(self.request('/api/state', cookie=cookie)['code'], 200)
        page = self.request('/', cookie=cookie)
        self.assertIn('登出測試站'.encode(), page['body'])
        self.assertEqual(self.request('/logout', 'POST', cookie=cookie)['code'], 303)
        self.assertEqual(self.request('/api/state', cookie=cookie)['code'], 401)
        secure = wsgi.create_application('tester', self.password, secure=True)
        response = self.login(origin='https://test.local', server=secure)
        self.assertIn('; Secure', response['headers']['Set-Cookie'])

    def test_csrf_and_bad_password(self):
        self.assertEqual(self.login(password='wrong')['code'], 401)
        self.assertEqual(self.login(origin='https://evil.example')['code'], 403)
        cookie = self.cookie()
        for path in ['/logout', '/api/action', '/api/upload/1']:
            self.assertEqual(self.request(path, 'POST', b'{}', cookie=cookie,
                                         origin='https://evil.example')['code'], 403)

    def test_session_restart_expiry_and_password_rotation(self):
        cookie = self.cookie()
        restarted = wsgi.create_application('tester', self.password, secure=False)
        self.assertEqual(self.request('/api/state', cookie=cookie, server=restarted)['code'], 200)
        rotated = wsgi.create_application('tester', 'a-new-fixture-password-2026', secure=False)
        self.assertEqual(self.request('/api/state', cookie=cookie, server=rotated)['code'], 401)
        with app.db() as c:
            c.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.request('/api/state', cookie=cookie)['code'], 401)

    def test_rate_limit(self):
        with app.db() as c:
            import time
            c.executemany('INSERT INTO login_attempts VALUES(?)', [(time.time(),)] * 10)
        self.assertEqual(self.login()['code'], 429)

    def test_missing_credentials_fail_closed(self):
        with self.assertRaises(RuntimeError):
            wsgi.create_application('', '')
        with self.assertRaises(RuntimeError):
            wsgi.create_application('admin', 'short')

    def test_authenticated_workflow_and_persistent_storage(self):
        cookie = self.cookie()
        page = self.request('/', cookie=cookie)['body'].decode()
        token = page.split("const token='")[1].split("'")[0]
        quote = {'op': 'quote', 'customer': 'WSGI fixture', 'currency': 'VND',
                 'items': [{'model': 'PANEL', 'qty': '2', 'price': '250000'}]}
        response = self.request('/api/action', 'POST', json.dumps(quote).encode(), cookie=cookie,
                                HTTP_X_TRADE_TOKEN=token)
        self.assertEqual(response['code'], 200, response['body'])
        ident = json.loads(response['body'])['id']
        self.assertEqual(self.request('/api/upload/' + str(ident), 'POST', b'%PDF-test',
                         cookie=cookie, HTTP_X_TRADE_TOKEN=token, HTTP_X_FILE_NAME='signed.pdf')['code'], 200)
        restarted = wsgi.create_application('tester', self.password, secure=False)
        state = json.loads(self.request('/api/state', cookie=cookie, server=restarted)['body'])
        self.assertEqual(state['documents'][0]['total'], '500000')
        attachment = state['attachments'][0]['id']
        self.assertEqual(self.request('/attachment/' + str(attachment), cookie=cookie)['body'], b'%PDF-test')
        self.assertIn(b'QUOTATION', self.request('/print/' + str(ident), cookie=cookie)['body'])


if __name__ == '__main__':
    unittest.main()
