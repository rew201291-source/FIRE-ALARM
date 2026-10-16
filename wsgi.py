"""Authenticated WSGI entry point for a single-instance Render test deployment."""
import hashlib
import hmac
import io
import json
import os
import secrets
import time
from email.message import Message
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
from urllib.parse import parse_qs

import app

LOGIN_HTML = '''<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MPOWER 登入</title>
<style>body{font:15px system-ui;background:#edf2f5;color:#203446;display:grid;place-items:center;min-height:95vh;margin:0}main{background:white;padding:40px;border-radius:14px;width:min(340px,75vw);box-shadow:0 10px 50px #18394c15}h1{letter-spacing:2px}label{display:grid;gap:8px;margin:20px 0}input,button{font:inherit;padding:12px;border:1px solid #cfdae1;border-radius:7px}button{width:100%;background:#147c75;color:white;border:0;cursor:pointer}p{color:#728692;line-height:1.7}</style>
<main><h1>MPOWER</h1><p>出口業務管理 · 團隊測試站</p><form method="post" action="/login">
<label>帳號<input name="username" autocomplete="username" required maxlength="100"></label>
<label>密碼<input name="password" type="password" autocomplete="current-password" required maxlength="1024"></label>
<button>登入系統</button></form><p>請使用管理者提供的測試帳號。<br>目前僅供測試，尚未提供個別角色權限。</p></main></html>'''


class Bridge(app.Handler):
    def __init__(self, environ):
        self.path = environ.get('PATH_INFO', '/')
        self.headers = Message()
        for key, value in environ.items():
            if key.startswith('HTTP_'):
                self.headers[key[5:].replace('_', '-')] = value
        self.headers['Content-Length'] = environ.get('CONTENT_LENGTH', '') or '0'
        self.rfile = environ['wsgi.input']
        self.wfile = io.BytesIO()
        self.output_headers = []

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.output_headers.append((key, str(value)))

    def end_headers(self):
        pass

    def reply(self, code, content, content_type='application/json; charset=utf-8'):
        if self.path == '/' and content_type.startswith('text/html'):
            content = content.replace('<main>', '<main><form method="post" action="/logout" style="text-align:right;margin-bottom:12px"><button>登出測試站</button></form>', 1)
        super().reply(code, content, content_type)


def create_application(username=None, password=None, secure=True):
    username = username if username is not None else os.environ.get('APP_USERNAME', '')
    password = password if password is not None else os.environ.get('APP_PASSWORD', '')
    if not username.strip() or len(username) > 100 or len(password) < 16 or len(password) > 1024:
        raise RuntimeError('Set APP_USERNAME and APP_PASSWORD (16–1024 characters) in Render environment settings.')
    app.init()
    with app.db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, expires REAL NOT NULL, version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts(at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_config(name TEXT PRIMARY KEY, value BLOB NOT NULL);
        ''')
        c.execute('INSERT OR IGNORE INTO auth_config VALUES(?,?)', ('salt', secrets.token_bytes(32)))
        salt = c.execute('SELECT value FROM auth_config WHERE name=?', ('salt',)).fetchone()[0]
    password_hash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000)
    # A stable, slow-derived version survives restarts but invalidates old credentials.
    credential_version = hashlib.sha256(username.encode() + b'\0' + password_hash).hexdigest()

    def result(start, status, body, headers=()):
        body = body.encode() if isinstance(body, str) else body
        start(f'{status} {HTTPStatus(status).phrase}', [
            ('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', str(len(body))),
            ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
            ('X-Frame-Options', 'DENY'), ('Referrer-Policy', 'same-origin'), *headers])
        return [body]

    def cookie(value, age):
        return f'trade_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}' + ('; Secure' if secure else '')

    def same_origin(environ):
        # Render terminates TLS; never derive the trusted host from forwarded-host.
        origin = environ.get('HTTP_ORIGIN', '')
        scheme = 'https' if secure else environ.get('wsgi.url_scheme', 'http')
        return origin == f"{scheme}://{environ.get('HTTP_HOST', '')}"

    def application(environ, start_response):
        path = environ.get('PATH_INFO', '/')
        method = environ.get('REQUEST_METHOD', 'GET')
        if path == '/healthz' and method == 'GET':
            try:
                with app.db() as c:
                    c.execute('SELECT count(*) FROM documents').fetchone()
                return result(start_response, 200, 'ok')
            except Exception:
                return result(start_response, 503, 'not ready')
        if path == '/login':
            if method == 'GET':
                return result(start_response, 200, LOGIN_HTML)
            if method != 'POST':
                return result(start_response, 405, 'Method not allowed')
            if not same_origin(environ):
                return result(start_response, 403, '請從本站登入頁面操作。')
            try:
                length = int(environ.get('CONTENT_LENGTH', '0'))
            except ValueError:
                length = 0
            if not 0 < length <= 8192:
                return result(start_response, 413, '登入資料過長')
            p = parse_qs(environ['wsgi.input'].read(length).decode('utf-8', errors='replace'))
            name, candidate = p.get('username', [''])[0], p.get('password', [''])[0]
            now = time.time()
            # A shared, persistent limiter also bounds concurrent password-hashing work.
            with app.db() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('DELETE FROM login_attempts WHERE at < ?', (now - 300,))
                if c.execute('SELECT count(*) FROM login_attempts').fetchone()[0] >= 10:
                    return result(start_response, 429, '登入嘗試過多，請於五分鐘後重試。')
                c.execute('INSERT INTO login_attempts(at) VALUES(?)', (now,))
            supplied = hashlib.pbkdf2_hmac('sha256', candidate.encode(), salt, 600000)
            valid = hmac.compare_digest(supplied, password_hash) & hmac.compare_digest(name.encode(), username.encode())
            if not valid:
                return result(start_response, 401, '帳號或密碼不正確。<a href="/login">重新登入</a>')
            token = secrets.token_urlsafe(48)
            with app.db() as c:
                c.execute('DELETE FROM sessions WHERE expires < ? OR version != ?', (now, credential_version))
                c.execute('INSERT INTO sessions VALUES(?,?,?)',
                          (hashlib.sha256(token.encode()).hexdigest(), now + 28800, credential_version))
            return result(start_response, 303, '', [('Location', '/'), ('Set-Cookie', cookie(token, 28800))])
        cookies = SimpleCookie()
        try:
            cookies.load(environ.get('HTTP_COOKIE', ''))
        except CookieError:
            pass
        token = cookies['trade_session'].value if 'trade_session' in cookies else ''
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with app.db() as c:
            session = c.execute('SELECT 1 FROM sessions WHERE token_hash=? AND expires>? AND version=?',
                                (token_hash, time.time(), credential_version)).fetchone()
        if not session:
            if path.startswith('/api/'):
                return result(start_response, 401, json.dumps({'error': '登入已過期，請重新登入'}))
            return result(start_response, 303, '', [('Location', '/login')])
        if method == 'POST' and not same_origin(environ):
            return result(start_response, 403, '請從本站操作。')
        if path == '/logout':
            if method != 'POST':
                return result(start_response, 405, 'Method not allowed')
            with app.db() as c:
                c.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash,))
            return result(start_response, 303, '', [('Location', '/login'), ('Set-Cookie', cookie('', 0))])
        if method not in ['GET', 'POST']:
            return result(start_response, 405, 'Method not allowed')
        try:
            length = int(environ.get('CONTENT_LENGTH', '') or '0')
        except ValueError:
            return result(start_response, 400, 'Invalid content length')
        if length < 0 or length > 15 * 1024 * 1024:
            return result(start_response, 413, '檔案上限 15MB')
        bridge = Bridge(environ)
        try:
            if method == 'GET':
                bridge.do_GET()
            else:
                bridge.do_POST()
        except Exception:
            # Do not leak database paths or internal diagnostics to a public response.
            import logging
            logging.exception('Request failed')
            return result(start_response, 500, '伺服器錯誤，請稍後重試。')
        start_response(f'{bridge.status} {HTTPStatus(bridge.status).phrase}', bridge.output_headers)
        return [bridge.wfile.getvalue()]

    return application


