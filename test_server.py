"""HTTP-level tests for the admin panel (FastAPI TestClient; needs httpx)."""
import base64
import csv
import io
import logging
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

TEMP = tempfile.TemporaryDirectory()
os.environ.update({'ADMIN_PASSWORD': 'test-password-long-enough', 'ADMIN_USER': 'owner',
                   'STOCK_KEY': Fernet.generate_key().decode(),
                   'DATABASE_PATH': str(Path(TEMP.name) / 'shop.sqlite3')})
for name in ('PANEL_ORIGIN', 'BOT_TOKEN', 'PANEL_BASIC_AUTH'):
    os.environ.pop(name, None)

try:
    from fastapi.testclient import TestClient
    import server
except ImportError as error:  # pragma: no cover - httpx missing
    raise unittest.SkipTest('fastapi TestClient unavailable: ' + str(error))

logging.getLogger('themarket.panel').setLevel(logging.CRITICAL)
OWNER_PASSWORD = 'test-password-long-enough'
AUTH = 'Basic ' + base64.b64encode(b'owner:' + OWNER_PASSWORD.encode()).decode()
ORIGIN = {'Origin': 'http://testserver'}


def client(ip='testclient'):
    return TestClient(server.app, client=(ip, 50000), follow_redirects=False)


def signed_in(username='owner', password=OWNER_PASSWORD, ip='testclient'):
    http = client(ip)
    response = http.post('/api/login', json={'username': username, 'password': password}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    return http


def png(width=40, height=30, mode='RGBA', fmt='PNG', **save):
    from PIL import Image
    output = io.BytesIO()
    Image.new(mode, (width, height), (200, 30, 30, 128) if mode == 'RGBA' else (200, 30, 30)).save(output, fmt, **save)
    return output.getvalue()


class Base(unittest.TestCase):
    http = None

    def setUp(self):
        server.throttle.failures.clear()
        if self.http is None:
            type(self).http = signed_in()

    def get(self, path, **kw):
        return self.http.get(path, headers={'Sec-Fetch-Site': 'same-origin'}, **kw)

    def send(self, method, path, body=None, http=None):
        return (http or self.http).request(method, path, json=body, headers=ORIGIN)

    def product(self, **extra):
        data = {'category': 'Тест', 'name': 'Товар ' + uuid.uuid4().hex[:6], 'price': '5', 'mode': 'auto', **extra}
        response = self.send('POST', '/api/products', data)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['id']

    def staff_member(self, role):
        name = role + uuid.uuid4().hex[:6]
        response = self.send('POST', '/api/staff', {'username': name, 'password': 'long-password-' + role, 'role': role})
        self.assertEqual(response.status_code, 200, response.text)
        return name, signed_in(name, 'long-password-' + role)


class SessionTests(Base):
    def test_healthz_and_login_page_are_public(self):
        anonymous = client()
        response = anonymous.get('/healthz')
        self.assertEqual(response.json(), {'ok': True})
        self.assertEqual(response.headers['x-frame-options'], 'DENY')
        page = anonymous.get('/login')
        self.assertEqual(page.status_code, 200)
        self.assertIn('login.js', page.text)
        for asset in ('/assets/panel.css', '/assets/login.js', '/assets/favicon.svg'):
            self.assertEqual(anonymous.get(asset).status_code, 200, asset)
        self.assertEqual(anonymous.get('/assets/core.js').status_code, 303)

    def test_unauthenticated_requests(self):
        anonymous = client()
        response = anonymous.get('/')
        self.assertEqual((response.status_code, response.headers['location']), (303, '/login'))
        response = anonymous.get('/api/state')
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('www-authenticate', response.headers)  # no browser Basic prompt in staff mode
        self.assertEqual(response.headers['cache-control'], 'no-store')
        response = anonymous.post('/api/products', content=b'{"broken', headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(anonymous.post('/api/products', content=b'x' * (3 * 1024 * 1024)).status_code, 401)
        # Basic is ignored in staff mode unless PANEL_BASIC_AUTH=1 (scripts).
        self.assertEqual(anonymous.get('/api/state', headers={'Authorization': AUTH}).status_code, 401)
        with patch.dict(os.environ, {'PANEL_BASIC_AUTH': '1'}):
            self.assertEqual(anonymous.get('/api/state', headers={'Authorization': AUTH}).json()['me']['mode'], 'basic')

    def test_cookie_flags_and_logout(self):
        http = client()
        response = http.post('/api/login', json={'username': 'owner', 'password': OWNER_PASSWORD}, headers=ORIGIN)
        cookie = response.headers['set-cookie']
        for flag in ('tm_session=', 'HttpOnly', 'SameSite=strict', 'Path=/'):
            self.assertIn(flag.lower(), cookie.lower())
        self.assertNotIn('secure', cookie.lower())
        me = http.get('/api/me').json()
        self.assertEqual((me['username'], me['role'], me['mode']), ('owner', 'owner', 'staff'))
        self.assertIn('staff', me['permissions'])
        self.assertEqual(http.get('/').status_code, 200)
        self.assertEqual(http.post('/api/logout', headers=ORIGIN).status_code, 200)
        self.assertEqual(http.get('/api/state').status_code, 401)
        # Behind an HTTPS proxy the cookie is Secure.
        response = client().post('/api/login', json={'username': 'owner', 'password': OWNER_PASSWORD},
                                 headers={**ORIGIN, 'X-Forwarded-Proto': 'https'})
        self.assertIn('secure', response.headers['set-cookie'].lower())

    def test_login_needs_same_origin(self):
        http = client()
        body = {'username': 'owner', 'password': OWNER_PASSWORD}
        self.assertEqual(http.post('/api/login', json=body, headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(http.post('/api/login', json=body).status_code, 403)
        self.assertEqual(http.post('/api/login', json=body, headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)

    def test_bruteforce_lockout(self):
        attacker = client('10.0.0.9')
        for _ in range(server.AUTH_FAILURES):
            response = attacker.post('/api/login', json={'username': 'owner', 'password': 'wrong-password'}, headers=ORIGIN)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()['detail'], 'Неверный логин или пароль')
        locked = attacker.post('/api/login', json={'username': 'owner', 'password': OWNER_PASSWORD}, headers=ORIGIN)
        self.assertEqual(locked.status_code, 429)
        self.assertIn('retry-after', locked.headers)
        self.assertEqual(signed_in(ip='10.0.0.10').get('/api/me').status_code, 200)

    def test_forged_cookie_is_rejected(self):
        http = client()
        http.cookies.set('tm_session', 'x' * 43)
        self.assertEqual(http.get('/api/state').status_code, 401)
        http.cookies.set('tm_session', 'x' * 5000)
        self.assertEqual(http.get('/api/state').status_code, 401)

    def test_assets_need_auth_and_exist(self):
        response = self.http.get('/assets/core.js')
        self.assertEqual(response.status_code, 200)
        self.assertIn('content-security-policy', response.headers)
        self.assertEqual(self.http.get('/assets/missing.js').status_code, 404)
        index = self.http.get('/')
        for name in __import__('re').findall(r'/assets/([\w.-]+)', index.text):
            self.assertEqual(self.http.get('/assets/' + name).status_code, 200, name)


class GuardTests(Base):
    def test_body_limits(self):
        big = b'{"lines":"' + b'a' * (2 * 1024 * 1024) + b'"}'
        headers = {**ORIGIN, 'Content-Type': 'application/json'}
        self.assertEqual(self.http.post('/api/products', content=big, headers=headers).status_code, 413)
        self.assertNotEqual(self.http.post('/api/products/1/stock', content=big, headers=headers).status_code, 413)

        def chunks():
            for _ in range(3):
                yield b'x' * (512 * 1024)
        self.assertEqual(self.http.post('/api/products', content=chunks(), headers=headers).status_code, 413)

    def test_origin_checks(self):
        body = {'new_product': True, 'restock': True}
        url = '/api/broadcasts/settings'
        self.assertEqual(self.http.put(url, json=body).status_code, 403)
        self.assertEqual(self.http.put(url, json=body, headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.http.put(url, json=body, headers={'Origin': 'null'}).status_code, 403)
        self.assertEqual(self.http.put(url, json=body, headers={**ORIGIN, 'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.assertEqual(self.http.put(url, json=body, headers=ORIGIN).status_code, 200)
        self.assertEqual(self.http.put(url, json=body, headers={'Origin': 'https://testserver'}).status_code, 200)
        self.assertEqual(self.http.put(url, json=body, headers={'Sec-Fetch-Site': 'same-origin'}).status_code, 200)
        with patch.dict(os.environ, {'PANEL_ORIGIN': 'https://shop.example.com, https://admin.example.com'}):
            self.assertEqual(self.http.put(url, json=body, headers=ORIGIN).status_code, 403)
            self.assertEqual(self.http.put(url, json=body, headers={'Origin': 'https://admin.example.com'}).status_code, 200)

    def test_side_effect_gets_require_same_origin(self):
        oid = 'f' * 32
        self.assertEqual(self.http.get('/api/orders/' + oid).status_code, 403)
        self.assertEqual(self.http.get('/api/orders/' + oid, headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        response = self.get('/api/orders/' + oid)
        self.assertEqual((response.status_code, response.json()['detail']), (404, 'Заказ не найден'))
        self.assertEqual(self.http.get('/api/orders/' + oid + '/details', headers={'X-TheMarket': '1'}).status_code, 404)
        self.assertEqual(self.get('/api/backups/recovery-key').status_code, 405)

    def test_etag_and_not_modified(self):
        first = self.get('/api/products')
        tag = first.headers['etag']
        again = self.http.get('/api/products', headers={'If-None-Match': tag})
        self.assertEqual((again.status_code, again.content), (304, b''))
        self.assertEqual(again.headers['etag'], tag)
        self.assertEqual(again.headers['x-frame-options'], 'DENY')
        self.assertEqual(self.http.get('/api/products', headers={'If-None-Match': 'W/' + tag}).status_code, 304)
        self.product()
        changed = self.http.get('/api/products', headers={'If-None-Match': tag})
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed.headers['etag'], tag)
        # Mutations and errors carry no ETag.
        self.assertNotIn('etag', self.send('PUT', '/api/broadcasts/settings', {'new_product': True, 'restock': True}).headers)
        self.assertNotIn('etag', self.get('/api/customers/0').headers)


class PermissionTests(Base):
    def test_every_api_route_checks_a_permission(self):
        public = {'/api/login'}
        for route in server.app.routes:
            path = getattr(route, 'path', '')
            if not path.startswith('/api/') or path in public:
                continue
            checks = [d.call for d in route.dependant.dependencies if hasattr(d.call, 'permissions')]
            checks += [d.call for d in route.dependant.dependencies for d in d.dependencies if hasattr(d.call, 'permissions')]
            self.assertTrue(checks, f'{route.methods} {path} has no permission check')
            for check in checks:
                self.assertTrue(set(check.permissions) <= set(server.PERMISSIONS) | {'signed-in'}, path)

    def test_support_role(self):
        _, http = self.staff_member('support')
        state = http.get('/api/state').json()
        self.assertEqual(state['me']['role'], 'support')
        self.assertNotIn('payment_issues', state)
        self.assertEqual(http.get('/api/support').status_code, 200)
        self.assertEqual(http.get('/api/orders').status_code, 200)
        self.assertEqual(http.get('/api/customers').status_code, 200)
        for method, path, body in (('POST', '/api/products', {}), ('POST', '/api/customers/5/wallet', {}),
                                   ('GET', '/api/staff', None), ('GET', '/api/analytics', None), ('GET', '/api/events', None),
                                   ('GET', '/api/promos', None), ('POST', '/api/backups', None), ('GET', '/api/notify', None),
                                   ('POST', '/api/orders/' + 'a' * 32 + '/claim', None)):
            response = self.send(method, path, body, http=http)
            self.assertEqual(response.status_code, 403, path)
            self.assertIn('Недостаточно прав', response.json()['detail'])

    def test_secrets_costs_and_balances_need_their_permissions(self):
        pid = self.product(cost_price='2.50')
        server.store.import_stock(pid, 'SECRET-KEY-1')
        order = server.store.create_order(pid, 810001)
        server.store.adjust_wallet(810001, 10000, 'test', str(uuid.uuid4()), 'owner')
        server.store.remember_customer(810001, 'reveal_case', 'R', '')
        server.store.pay_wallet(order['id'], 810001)
        self.assertEqual(self.get(f"/api/orders/{order['id']}").json()['result'], 'SECRET-KEY-1')
        for role in ('support', 'manager'):
            _, http = self.staff_member(role)
            head = {'Sec-Fetch-Site': 'same-origin'}
            info = http.get(f"/api/orders/{order['id']}", headers=head).json()
            self.assertIsNone(info['result'], role)
            self.assertTrue(info['result_hidden'])
            self.assertNotIn('cost_total', info)
            product = next(p for p in http.get('/api/products').json() if p['id'] == pid)
            self.assertNotIn('cost_price', product)
            if role == 'support':
                item = http.get('/api/customers?query=reveal_case').json()['items'][0]
                self.assertNotIn('balance', item); self.assertNotIn('spent', item)
                self.assertIsNone(http.get('/api/customers/810001', headers=head).json()['wallet'])
                self.assertNotIn('wallet_total', http.get('/api/state').json()['metrics'])
                header = http.get('/api/customers/export?query=reveal_case', headers=head).content.decode('utf-8-sig').splitlines()[0]
                self.assertNotIn('Баланс', header)
            else:
                # A manager saving the product keeps the purchase price it cannot see.
                self.assertEqual(self.send('PUT', f'/api/products/{pid}', {'category': 'Тест', 'name': 'M', 'price': '5',
                                                                           'mode': 'auto', 'cost_price': None}, http=http).status_code, 200)
                self.assertEqual(server.store.product(pid, public=False)['cost_price'], 250)
                self.assertEqual(self.send('POST', f"/api/orders/{order['id']}/resend", http=http).status_code, 200)
        self.assertIn('balance', self.get('/api/customers?query=reveal_case').json()['items'][0])

    def test_manager_role(self):
        name, http = self.staff_member('manager')
        pid = self.product()
        self.assertEqual(self.send('PUT', f'/api/products/{pid}', {'category': 'Тест', 'name': 'M', 'price': '7', 'mode': 'auto'}, http=http).status_code, 200)
        self.assertEqual(http.get('/api/promos').status_code, 200)
        self.assertEqual(self.send('POST', '/api/promos', {'code': 'MGR1', 'amount': '5'}, http=http).status_code, 403)
        self.assertEqual(self.send('POST', '/api/customers/5/balance', {}, http=http).status_code, 403)
        self.assertEqual(http.get('/api/analytics').status_code, 403)
        # Audit actor is the staff username.
        event = server.store.events(query=str(pid))['items'][0]
        self.assertEqual(event['actor'], name)


class StaffTests(Base):
    def test_create_change_role_deactivate_and_revoke(self):
        name, http = self.staff_member('support')
        members = self.get('/api/staff').json()
        member = next(m for m in members['items'] if m['username'] == name)
        self.assertNotIn('hash', member)
        self.assertEqual(members['roles']['manager'], 'Менеджер')
        self.assertEqual(self.send('PUT', f"/api/staff/{member['id']}", {'role': 'manager'}).status_code, 200)
        self.assertEqual(http.get('/api/state').status_code, 401)  # role change ends old sessions
        http = signed_in(name, 'long-password-support')
        self.assertEqual(http.get('/api/me').json()['role'], 'manager')
        self.assertEqual(self.send('POST', f"/api/staff/{member['id']}/revoke").status_code, 200)
        self.assertEqual(http.get('/api/me').status_code, 401)
        http = signed_in(name, 'long-password-support')
        self.assertEqual(self.send('PUT', f"/api/staff/{member['id']}", {'active': False}).status_code, 200)
        self.assertEqual(http.get('/api/me').status_code, 401)
        response = client().post('/api/login', json={'username': name, 'password': 'long-password-support'}, headers=ORIGIN)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.send('PUT', f"/api/staff/{member['id']}", {'password': 'new-password-123', 'active': True}).status_code, 200)
        signed_in(name, 'new-password-123')

    def test_validation_and_self_protection(self):
        self.assertEqual(self.send('POST', '/api/staff', {'username': 'x', 'password': 'long-password', 'role': 'support'}).status_code, 422)
        self.assertEqual(self.send('POST', '/api/staff', {'username': 'shortpw', 'password': 'short', 'role': 'support'}).json()['field'], 'password')
        self.assertEqual(self.send('POST', '/api/staff', {'username': 'godmode', 'password': 'long-password', 'role': 'root'}).status_code, 422)
        me = self.get('/api/staff').json()['me']
        response = self.send('PUT', f'/api/staff/{me}', {'role': 'support'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('собственную', response.json()['detail'])
        self.assertEqual(self.send('PUT', f'/api/staff/{me}', {'active': False}).status_code, 400)


class ImageTests(Base):
    def upload(self, pid, data, content_type='image/png'):
        return self.http.post(f'/api/products/{pid}/image', content=data, headers={**ORIGIN, 'Content-Type': content_type})

    def test_upload_reencodes_and_serves(self):
        from PIL import Image
        pid = self.product()
        response = self.upload(pid, png(2600, 1300))
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertRegex(data['image_path'], r'^product-images/\d+-[0-9a-f]{8}\.jpg$')
        self.assertEqual((data['width'], data['height']), (1280, 640))
        served = self.get(f'/api/products/{pid}/image')
        self.assertEqual(served.headers['content-type'], 'image/jpeg')
        image = Image.open(io.BytesIO(served.content))
        self.assertEqual((image.format, image.mode), ('JPEG', 'RGB'))
        self.assertGreater(image.getpixel((5, 5))[1], 100)  # transparent red flattened on white
        stored = Path(server.store.path).parent / data['image_path']
        self.assertTrue(stored.is_file())
        # A new photo replaces the old file.
        second = self.upload(pid, png(50, 50, mode='RGB', fmt='WEBP'), 'image/webp').json()
        self.assertFalse(stored.exists())
        self.assertEqual(server.store.product(pid, public=False)['image_path'], second['image_path'])
        self.assertTrue(self.send('DELETE', f'/api/products/{pid}/image').json()['ok'])
        self.assertEqual(self.get(f'/api/products/{pid}/image').status_code, 404)
        self.assertFalse((Path(server.store.path).parent / second['image_path']).exists())

    def test_exif_is_stripped(self):
        from PIL import Image
        exif = Image.Exif()
        exif[0x010F] = 'SecretCamera'
        exif[0x0112] = 6  # rotate 90°
        pid = self.product()
        response = self.upload(pid, png(60, 20, mode='RGB', fmt='JPEG', exif=exif.tobytes()), 'image/jpeg')
        self.assertEqual(response.status_code, 200, response.text)
        content = self.get(f'/api/products/{pid}/image').content
        image = Image.open(io.BytesIO(content))
        self.assertEqual(image.size, (20, 60))  # orientation applied before stripping
        self.assertFalse(image.getexif())
        self.assertNotIn(b'SecretCamera', content)

    def test_rejects_bad_uploads(self):
        pid = self.product()
        response = self.upload(pid, b'GIF89a' + b'0' * 100, 'image/gif')
        self.assertEqual(response.status_code, 400)
        self.assertIn('JPEG, PNG или WebP', response.json()['detail'])
        self.assertEqual(self.upload(pid, b'not an image at all', 'image/png').status_code, 400)
        self.assertEqual(self.upload(pid, png()[:60], 'image/png').status_code, 400)  # truncated
        self.assertEqual(self.upload(pid, png(fmt='PNG'), 'image/jpeg').status_code, 200)  # sniffed, not trusted
        self.assertEqual(self.upload(pid, b'\x89PNG\r\n\x1a\n' + b'0' * (5 * 1024 * 1024), 'image/png').status_code, 413)
        self.assertEqual(self.upload(999999, png()).status_code, 404)
        self.assertEqual(self.http.post(f'/api/products/{pid}/image', content=b'x' * (7 * 1024 * 1024),
                                        headers={**ORIGIN, 'Content-Type': 'image/png'}).status_code, 413)
        with patch.object(server, 'IMAGE_PIXELS', 100):
            self.assertEqual(self.upload(pid, png(20, 20)).status_code, 400)

    def test_badge_old_price_and_ordering(self):
        category = 'Порядок ' + uuid.uuid4().hex[:4]
        first, second = self.product(category=category, badge='Хит', old_price='9'), self.product(category=category)
        item = next(p for p in self.get('/api/products').json() if p['id'] == first)
        self.assertEqual((item['badge'], item['old_price']), ('Хит', 9))
        bad = self.send('POST', '/api/products', {'category': 'A', 'name': 'B', 'price': '5', 'mode': 'auto', 'old_price': '4'})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.send('POST', '/api/products', {'category': 'A', 'name': 'B', 'price': '5', 'mode': 'auto', 'badge': 'x' * 17}).json()['field'], 'badge')
        self.assertTrue(self.send('POST', f'/api/products/{second}/move', {'direction': 'up'}).json()['ok'])
        order = [p['id'] for p in self.get('/api/products').json() if p['category'] == category]
        self.assertEqual(order, [second, first])
        self.assertEqual(self.send('POST', f'/api/products/{second}/move', {'direction': 'sideways'}).status_code, 422)


class ApiTests(Base):
    def test_state_is_small(self):
        data = self.get('/api/state').json()
        for key in ('metrics', 'currency', 'actor', 'payment_issues', 'me', 'features', 'support_unread'):
            self.assertIn(key, data)
        for key in ('customers', 'orders', 'audit', 'products'):
            self.assertNotIn(key, data)
        self.assertEqual(data['actor'], 'owner')
        self.assertTrue(data['features']['staff'])

    def test_orders_are_paginated(self):
        pid = self.product()
        server.store.import_stock(pid, '\n'.join('k' + str(i) for i in range(5)), 'owner')
        for uid in range(800001, 800004):
            server.store.create_order(pid, uid)
        data = self.get('/api/orders?page_size=2').json()
        self.assertEqual(len(data['items']), 2)
        self.assertGreaterEqual(data['total'], 3)
        self.assertEqual(set(data), {'items', 'total', 'page', 'pages'})
        self.assertEqual(self.get('/api/orders?query=800002').json()['items'][0]['user_id'], 800002)
        self.assertEqual(self.get('/api/orders?status=attention').json()['total'], 0)
        self.assertEqual(self.get('/api/orders?status=paid').json()['total'], 0)
        self.assertEqual(self.get('/api/orders?status=bogus').status_code, 422)
        self.assertEqual(self.get('/api/orders?page_size=500').status_code, 422)
        rows = list(csv.reader(io.StringIO(self.get('/api/orders/export?query=800001').content.decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(rows), 2)

    def test_dashboard_and_search(self):
        pid = self.product(name='Поисковый товар')
        data = self.get('/api/dashboard').json()
        self.assertEqual(len(data['series']), 30)
        self.assertEqual(set(data['kpi']), {'revenue', 'orders', 'average', 'customers'})
        self.assertIn('support', data)
        self.assertIn(pid, [p['id'] for p in data['low_stock']['items']] + [None] * (data['low_stock']['total'] > 6))
        server.store.remember_customer(900077, 'searchme', 'Поиск', '')
        found = self.get('/api/search?q=searchme').json()
        self.assertEqual(found['customers'][0]['user_id'], 900077)
        self.assertEqual(self.get('/api/search?q=поисковый').json()['products'][0]['id'], pid)
        self.assertEqual(self.get('/api/search?q=').status_code, 422)

    def test_promos(self):
        body = {'code': 'spring10', 'amount': '10', 'max_uses': 100, 'note': 'Весна'}
        created = self.send('POST', '/api/promos', body)
        self.assertEqual(created.status_code, 200, created.text)
        item = created.json()
        self.assertEqual((item['code'], item['amount_cents'], item['used'], item['credited_cents'], item['users']),
                         ('SPRING10', 10, 0, 0, 0))  # Stars shop: whole units
        for key in ('kind', 'value', 'min_order_cents', 'product_ids'):
            self.assertNotIn(key, item)
        self.assertEqual(self.send('POST', '/api/promos', {**body, 'code': 'X1', 'amount': '0'}).status_code, 422)
        self.assertEqual(self.send('POST', '/api/promos', {**body, 'code': 'X2', 'amount': '10001'}).status_code, 422)
        self.assertEqual(self.send('POST', '/api/promos', {**body, 'code': 'HALF', 'amount': '9.5'}).status_code, 400)
        self.assertEqual(self.send('POST', '/api/promos', {'code': 'NOAMOUNT'}).status_code, 422)
        self.assertEqual(self.send('POST', '/api/promos', {**body, 'code': 'bad code!'}).json()['field'], 'code')
        self.assertEqual(self.send('POST', '/api/promos', {**body, 'starts_at': 2000000000, 'ends_at': 1900000000,
                                                           'code': 'DATES'}).status_code, 400)
        self.send('POST', '/api/promos', {'code': 'MINUS5', 'amount': '5'})
        page = self.get('/api/promos?page_size=1').json()
        self.assertEqual(len(page['items']), 1)
        self.assertGreaterEqual(page['total'], 2)
        self.assertEqual(set(page['totals']), {'uses', 'credited_cents', 'users'})
        self.assertEqual(self.get('/api/promos?query=minus').json()['items'][0]['code'], 'MINUS5')
        promo_id = item['id']
        server.store.remember_customer(910077, 'promo_user', 'Promo', 'User')
        server.promos.redeem_to_balance('spring10', 910077)
        used = self.get(f'/api/promos/{promo_id}/redemptions').json()
        self.assertEqual((used['total'], used['items'][0]['user_id'], used['items'][0]['amount_cents'], used['items'][0]['name']),
                         (1, 910077, 10, 'Promo User'))
        self.assertEqual(self.get('/api/promos/999999/redemptions').status_code, 404)
        listed = [p for p in self.get('/api/promos').json()['items'] if p['id'] == promo_id][0]
        self.assertEqual((listed['used'], listed['credited_cents'], listed['users']), (1, 10, 1))
        self.assertTrue(self.send('POST', f'/api/promos/{promo_id}/active', {'active': False}).json()['ok'])
        self.assertNotIn(promo_id, [p['id'] for p in self.get('/api/promos?status=active').json()['items']])
        self.assertEqual(self.send('PUT', f'/api/promos/{promo_id}', {**body, 'amount': '15'}).json()['amount_cents'], 15)

    def test_support_inbox(self):
        ticket = server.support.open_or_append(910001, 'Не пришёл ключ')['ticket_id']
        self.assertGreaterEqual(self.get('/api/state').json()['support_unread'], 1)
        inbox = self.get('/api/support').json()
        self.assertIn(ticket, [t['id'] for t in inbox['items']])
        thread = self.get(f'/api/support/{ticket}').json()
        self.assertEqual(thread['messages'][0]['text'], 'Не пришёл ключ')
        self.assertEqual(thread['ticket']['unread_for_staff'], 0)
        reply = self.send('POST', f'/api/support/{ticket}/reply', {'text': 'Отправили повторно'})
        self.assertEqual(reply.json()['actor'], 'owner')
        self.assertEqual(self.send('POST', f'/api/support/{ticket}/reply', {'text': ' '}).status_code, 400)
        self.assertEqual(self.send('POST', f'/api/support/{ticket}/reply', {'text': 'x' * 3501}).status_code, 422)
        self.assertEqual(self.send('POST', f'/api/support/{ticket}/assign').json()['assignee'], 'owner')
        self.assertIsNone(self.send('POST', f'/api/support/{ticket}/unassign').json()['assignee'])
        self.assertEqual(self.send('POST', f'/api/support/{ticket}/close').json()['status'], 'closed')
        self.assertEqual(self.send('POST', f'/api/support/{ticket}/close').status_code, 400)
        self.assertEqual(self.send('POST', f'/api/support/{ticket}/reopen').json()['status'], 'open')
        page = self.get(f'/api/support/{ticket}?limit=1').json()
        self.assertEqual((len(page['messages']), page['has_more']), (1, True))
        older = self.get(f"/api/support/{ticket}?limit=1&before={page['messages'][0]['id']}").json()
        self.assertEqual(older['messages'][0]['text'], 'Не пришёл ключ')
        self.assertEqual(self.send('POST', f'/api/support/{ticket}/delete').status_code, 422)
        self.assertEqual(self.get('/api/support/99999').status_code, 404)

    def test_notify_settings_and_test_message(self):
        settings = {**self.get('/api/notify').json(), 'admin_chat_ids': [], 'daily_summary_time': '21:30'}
        self.assertEqual(self.send('PUT', '/api/notify', settings).status_code, 200)
        self.assertEqual(self.send('POST', '/api/notify/test').status_code, 400)
        settings['admin_chat_ids'] = [123456, -1001234]
        settings['low_stock_overrides'] = {'5': 10}
        saved = self.send('PUT', '/api/notify', settings).json()
        self.assertEqual((saved['admin_chat_ids'], saved['low_stock_overrides']), ([123456, -1001234], {'5': 10}))
        self.assertEqual(self.send('PUT', '/api/notify', {**settings, 'daily_summary_time': '25:00'}).status_code, 422)
        self.assertEqual(self.send('POST', '/api/notify/test').json(), {'queued': 2})

    def test_broadcast_segments(self):
        self.assertIsInstance(self.get('/api/broadcasts/audience').json()['count'], int)
        self.assertEqual(self.get('/api/broadcasts/audience?type=language').status_code, 400)
        self.assertEqual(self.get('/api/broadcasts/audience?type=inactive&days=30').json()['segment'], {'type': 'inactive', 'days': 30})
        draft = {'reference': str(uuid.uuid4()), 'title': 'Сегмент', 'text': 'Привет', 'segment': {'type': 'language', 'language': 'en'},
                 'scheduled_at': time.time() + 3600}
        created = self.send('POST', '/api/broadcasts', draft)
        self.assertEqual(created.status_code, 200, created.text)
        detail = self.get('/api/broadcasts/' + created.json()['id']).json()
        self.assertEqual(detail['segment'], {'type': 'language', 'language': 'en'})
        past = {**draft, 'reference': str(uuid.uuid4()), 'scheduled_at': 1000}
        self.assertEqual(self.send('POST', '/api/broadcasts', past).status_code, 400)

    def test_validation_errors_are_russian_with_field(self):
        response = self.send('POST', '/api/products', {'category': 'A', 'name': '', 'price': '1', 'mode': 'auto'})
        self.assertEqual((response.status_code, response.json()['field'], response.json()['detail']), (422, 'name', 'Название: заполните поле'))
        response = self.send('POST', '/api/products', {'category': 'A', 'name': 'B', 'price': 'abc', 'mode': 'auto'})
        self.assertIn('число', response.json()['detail'])
        response = self.http.post('/api/products', content=b'{"broken', headers={**ORIGIN, 'Content-Type': 'application/json'})
        self.assertEqual(response.json()['detail'], 'Некорректное тело запроса')
        self.assertNotIn('ctx', response.text)

    def test_huge_path_numbers_do_not_crash(self):
        for path in ('/api/customers/99999999999999999999999', '/api/customers/0', '/api/products/99999999999999999999999/stock'):
            response = self.get(path)
            self.assertEqual((response.status_code, response.json()['detail']), (422, 'Некорректный адрес запроса'), path)
        self.assertEqual(self.get('/api/events?page=999999999999999999999').status_code, 422)
        self.assertEqual(self.send('PUT', '/api/products/99999999999999999999999', {}).status_code, 422)

    def test_internal_error_is_audited_without_message(self):
        with patch.object(server.store, 'metrics', side_effect=RuntimeError('/secret/path token=abc')):
            response = self.get('/api/state')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertNotIn('secret', response.text)
        event = server.store.events(category='system', level='error')['items'][0]
        self.assertEqual(event['action'], 'server.error')
        self.assertIn('/api/state', event['details'])
        self.assertNotIn('secret', event['details'])

    def test_library_value_errors_are_hidden(self):
        with patch.object(server.store, 'payment_list', side_effect=ValueError('invalid literal for int()')):
            response = self.get('/api/payments')
        self.assertEqual((response.status_code, response.json()['detail']), (400, server.GENERIC_ERROR))
        with patch.object(server.store, 'payment_list', side_effect=ValueError('Счёт уже оплачен')):
            self.assertEqual(self.get('/api/payments').json()['detail'], 'Счёт уже оплачен')

    def test_backup_create_download_and_key_are_posts(self):
        created = self.send('POST', '/api/backups')
        self.assertEqual(created.status_code, 200, created.text)
        name = created.json()['name']
        archive = self.send('POST', f'/api/backups/{name}/download')
        self.assertEqual(archive.status_code, 200)
        self.assertIn('attachment', archive.headers['content-disposition'])
        self.assertEqual(self.send('POST', '/api/backups/recovery-key').status_code, 200)
        self.assertEqual(self.send('POST', f'/api/backups/{name}/verify').status_code, 200)
        self.assertEqual(self.send('POST', '/api/backups/bad-name/download').status_code, 400)

    def test_backup_busy_is_409(self):
        with patch.object(server.backups, 'create', side_effect=server.BackupBusy('Уже выполняется операция с резервной копией')):
            self.assertEqual(self.send('POST', '/api/backups').status_code, 409)

    def test_channel_check_telegram_failure_is_502(self):
        async def broken(*args):
            raise ConnectionResetError('socket details')
        with patch.dict(os.environ, {'BOT_TOKEN': '123456:' + 'A' * 35}), patch.object(server, 'inspect_channel', broken):
            response = self.send('POST', '/api/channel-gate/check', {'enabled': True, 'channel': '@shop'})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('socket', response.text)

    def test_customers_pagination_and_export(self):
        for index in range(7):
            server.store.remember_customer(500000 + index, 'pager' + str(index), '=Evil', '')
        data = self.get('/api/customers?query=pager&page=2&page_size=3').json()
        self.assertEqual((data['total'], data['page'], data['pages'], len(data['items'])), (7, 2, 3, 3))
        self.assertEqual(self.get('/api/customers?query=@pager6').json()['total'], 1)
        rows = list(csv.reader(io.StringIO(self.get('/api/customers/export?query=pager').content.decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[1][2], "'=Evil")

    def test_wallet_and_stock(self):
        uid = 700001
        body = {'delta': '5', 'reason': 'Тест', 'reference': str(uuid.uuid4())}
        self.assertEqual(self.send('POST', f'/api/customers/{uid}/wallet', body).json()['wallet']['balance'], 5)
        self.assertEqual(self.get(f'/api/customers/{uid}').json()['wallet']['balance'], 5)
        pid = self.product()
        self.assertEqual(self.send('POST', f'/api/products/{pid}/stock', {'lines': 'a\nb'}).json()['added'], 2)
        items = self.get(f'/api/products/{pid}/stock').json()['items']
        self.assertIn(self.get(f'/api/products/{pid}/stock/{items[0]["id"]}').json()['content'], ('a', 'b'))
        self.assertEqual(self.http.get(f'/api/products/{pid}/stock/{items[0]["id"]}').status_code, 403)
        self.assertTrue(self.send('DELETE', f'/api/products/{pid}').json()['ok'])

    def test_events_export_escapes_text(self):
        event = {'at': 1.0, 'level': 'info', 'category': 'products', 'summary': '=cmd|calc', 'action': 'x',
                 'actor': '@owner', 'user_id': None, 'order_id': None, 'details': '{}'}
        with patch.object(server.store, 'events', return_value={'items': [event], 'total': 1, 'page': 1, 'pages': 1}):
            text = self.get('/api/events/export?category=products').content.decode('utf-8-sig')
        self.assertIn("'@owner", text)
        self.assertIn("'=cmd|calc", text)

    def test_payment_issues_endpoints(self):
        self.assertIn('available', self.get('/api/payment-issues').json())
        issues = [{'id': 3, 'kind': 'stars_order', 'amount': 10, 'currency': 'XTR', 'detail': 'Сумма не совпала',
                   'user_id': 1, 'created': 1.0, 'resolved_at': None}]
        calls = []
        with patch.object(server, 'store', _WithIssues(server.store, issues, calls)):
            self.assertEqual(self.get('/api/payment-issues').json()['items'], issues)
            self.assertEqual(self.get('/api/state').json()['payment_issues'], 1)
            self.assertEqual(self.send('POST', '/api/payment-issues/3/resolve', {'note': 'Возврат сделан'}).json(), {'ok': True})
            self.assertEqual(self.send('POST', '/api/payment-issues/3/resolve', {'note': ''}).status_code, 422)
        self.assertEqual(calls, [(3, 'Возврат сделан', 'owner')])


class CsvTests(unittest.TestCase):
    def test_csv_cell(self):
        for value in ('=1+1', '+7', '-cmd', '@x', '\tx', '\rx', ' =x', '＝1', '＋1', '－1', '＠1'):
            self.assertEqual(server.csv_cell(value), "'" + value)
        self.assertEqual(server.csv_cell(-5), -5)
        self.assertEqual(server.csv_cell('Обычный'), 'Обычный')
        self.assertEqual(server.csv_cell(None), '')
        from decimal import Decimal
        self.assertEqual(server.csv_line([Decimal('-1.50'), '-1.50']).strip(), "-1.50;'-1.50")

    def test_analytics_export_keeps_negative_numbers(self):
        rows = list(csv.reader(io.StringIO(server.csv_line([server.usd(-150), server.usd(None)])), delimiter=';'))
        self.assertEqual(rows[0], ['-1.50', 'Не указана'])


class _WithIssues:
    def __init__(self, inner, issues, calls):
        self.inner, self.issues, self.calls = inner, issues, calls

    def payment_issues(self, open_only=True):
        return self.issues

    def resolve_payment_issue(self, issue_id, note, actor):
        self.calls.append((issue_id, note, actor))

    def __getattr__(self, name):
        return getattr(self.inner, name)


if __name__ == '__main__':
    unittest.main()


class Features31Tests(Base):
    """3.1: CRM, order notes, templates, bulk products, insights and the live feed."""

    def paid_order(self, user_id=810001):
        pid = self.product()
        server.store.import_stock(pid, 'K-' + uuid.uuid4().hex)
        order = server.store.create_order(pid, user_id)
        server.store.approve(order['id'], user_id, order['price'], 'XTR')
        server.store.paid(order['id'], user_id, order['price'], 'XTR', 'charge-' + order['id'])
        return order

    def test_customer_crm_and_permissions(self):
        uid = 820001
        server.store.remember_customer(uid, 'crm_buyer', 'Иван', '')
        _, support_http = self.staff_member('support')
        _, manager_http = self.staff_member('manager')
        response = self.send('PUT', f'/api/customers/{uid}/tags', {'tags': ['VIP', ' опт ']}, http=support_http)
        self.assertEqual(response.json()['tags'], ['VIP', 'опт'])
        self.assertEqual(self.send('PUT', f'/api/customers/{uid}/note', {'note': 'Звонить вечером'}, http=manager_http).json()['note'],
                         'Звонить вечером')
        for http in (support_http, manager_http):
            self.assertEqual(self.send('POST', f'/api/customers/{uid}/ban', {'reason': 'Спам'}, http=http).status_code, 403)
            self.assertEqual(self.send('POST', f'/api/customers/{uid}/unban', None, http=http).status_code, 403)
        self.assertEqual(self.send('PUT', f'/api/customers/{uid}/tags', {'tags': ['x' * 25]}).status_code, 400)
        self.assertEqual(self.send('PUT', '/api/customers/999999991/tags', {'tags': ['VIP']}).status_code, 404)
        self.assertTrue(self.send('POST', f'/api/customers/{uid}/ban', {'reason': 'Спам'}).json()['banned'])
        self.assertEqual(self.send('POST', f'/api/customers/{uid}/ban', {'reason': ''}).status_code, 422)
        listed = support_http.get('/api/customers?tag=vip').json()
        self.assertEqual([(c['user_id'], c['tags'], c['banned']) for c in listed['items']], [(uid, ['VIP', 'опт'], True)])
        self.assertEqual(self.get('/api/customers?banned=true').json()['items'][0]['user_id'], uid)
        self.assertIn({'tag': 'VIP', 'count': 1}, support_http.get('/api/customers/tags').json()['items'])
        marks = support_http.get(f'/api/customers/marks?ids={uid},5').json()['items']
        self.assertEqual(marks, {str(uid): {'tags': ['VIP', 'опт'], 'banned': True}, '5': {'tags': [], 'banned': False}})
        self.assertEqual(self.get('/api/customers/marks?ids=1;DROP').status_code, 422)
        info = support_http.get(f'/api/customers/{uid}').json()
        self.assertEqual((info['meta']['note'], info['meta']['ban_reason']), ('Звонить вечером', 'Спам'))
        self.assertEqual(support_http.get(f'/api/customers/{uid}/meta').json()['tags'], ['VIP', 'опт'])
        self.assertFalse(self.send('POST', f'/api/customers/{uid}/unban').json()['banned'])

    def test_order_notes_need_manage_to_write(self):
        order = self.paid_order()
        _, support_http = self.staff_member('support')
        path = f"/api/orders/{order['id']}/notes"
        self.assertEqual(self.send('POST', path, {'text': 'Нельзя'}, http=support_http).status_code, 403)
        note = self.send('POST', path, {'text': 'Клиент просил вечером'}).json()
        self.assertEqual(note['author'], 'owner')
        self.assertEqual([n['text'] for n in support_http.get(path).json()['items']], ['Клиент просил вечером'])
        self.assertEqual(self.send('POST', path, {'text': 'x' * 1001}).status_code, 422)
        self.assertEqual(self.get('/api/orders/' + 'f' * 32 + '/notes').status_code, 404)

    def test_support_templates_routes_before_ticket_ids(self):
        _, support_http = self.staff_member('support')
        created = self.send('POST', '/api/support/templates', {'title': 'Оплата', 'text': 'Здравствуйте, {name}!'}, http=support_http).json()
        self.assertEqual(support_http.get('/api/support/templates').json()['items'][0]['title'], 'Оплата')
        updated = self.send('PUT', f"/api/support/templates/{created['id']}", {'title': 'Оплата 2', 'text': 'Текст'}, http=support_http)
        self.assertEqual(updated.json()['title'], 'Оплата 2')
        self.assertEqual(self.send('POST', '/api/support/templates', {'title': 'x' * 61, 'text': 'a'}).status_code, 422)
        self.assertTrue(self.send('DELETE', f"/api/support/templates/{created['id']}", http=support_http).json()['ok'])
        self.assertEqual(self.send('DELETE', f"/api/support/templates/{created['id']}").status_code, 404)
        no_support = self.staff_member('manager')[1]  # managers have 'support' too
        self.assertEqual(no_support.get('/api/support/templates').status_code, 200)

    def test_bulk_products(self):
        a, b = self.product(price='10'), self.product(price='3')
        _, support_http = self.staff_member('support')
        self.assertEqual(self.send('POST', '/api/products/bulk', {'ids': [a], 'action': 'hide'}, http=support_http).status_code, 403)
        self.assertEqual(self.send('POST', '/api/products/bulk', {'ids': [a, b], 'action': 'hide'}).json(),
                         {'ok': True, 'changed': 2, 'total': 2})
        result = self.send('POST', '/api/products/bulk', {'ids': [a, b], 'action': 'price_percent', 'value': '-12.5'}).json()
        self.assertEqual(result['changed'], 1)
        prices = {p['id']: p['price'] for p in self.get('/api/products').json()}
        self.assertEqual((prices[a], prices[b]), (9, 3))  # 8.75 → 9, 2.625 → 3 (whole Stars)
        for body in ({'ids': [a], 'action': 'price_percent', 'value': 600}, {'ids': [a], 'action': 'price_percent', 'value': 'abc'},
                     {'ids': [a], 'action': 'price_percent', 'value': '1.234'}, {'ids': [a], 'action': 'category', 'value': ''}):
            self.assertEqual(self.send('POST', '/api/products/bulk', body).status_code, 400, body)
        self.assertEqual(self.send('POST', '/api/products/bulk', {'ids': [], 'action': 'hide'}).status_code, 422)
        self.assertEqual(self.send('POST', '/api/products/bulk', {'ids': [a], 'action': 'nope'}).status_code, 422)
        self.send('POST', '/api/products/bulk', {'ids': [a, b], 'action': 'category', 'value': 'Массовая'})
        self.assertEqual({p['category'] for p in self.get('/api/products').json() if p['id'] in (a, b)}, {'Массовая'})
        self.assertEqual(self.send('POST', '/api/products/bulk', {'ids': [a, b], 'action': 'delete'}).json()['changed'], 2)

    def test_insights_and_goal_are_owner_only(self):
        self.paid_order(830001)
        _, manager_http = self.staff_member('manager')
        self.assertEqual(manager_http.get('/api/insights').status_code, 403)
        self.assertEqual(self.send('PUT', '/api/insights/goal', {'amount': '100'}, http=manager_http).status_code, 403)
        self.assertEqual(self.send('PUT', '/api/insights/goal', {'amount': '250'}).json()['goal'], 250)
        report = self.get('/api/insights?period=week').json()
        self.assertEqual(report['goal']['goal'], 250)
        self.assertGreaterEqual(report['heatmap']['total_orders'], 1)
        self.assertEqual(len(report['cohorts']['rows']), 12)
        self.assertEqual(self.get('/api/insights?period=bogus').status_code, 400)

    def test_live_recent_is_permission_filtered_and_tracks_seen(self):
        server.store.remember_customer(840001, 'live_new', 'Новый', '')
        order = self.paid_order(840002)
        owner = self.get('/api/live/recent?limit=50').json()
        kinds = {item['kind'] for item in owner['items']}
        self.assertTrue({'customer', 'order', 'low_stock'} <= kinds)
        paid = next(item for item in owner['items'] if item.get('order_id') == order['id'])
        self.assertEqual((paid['title'], paid['link']), ('Новый оплаченный заказ', '#orders'))
        self.assertEqual(set(paid), {'id', 'kind', 'title', 'text', 'at', 'link', 'tone', 'order_id', 'user_id'})
        _, support_http = self.staff_member('support')
        support_items = support_http.get('/api/live/recent?limit=100').json()['items']
        self.assertTrue(support_items)
        self.assertNotIn('low_stock', {item['kind'] for item in support_items})  # needs products/stock rights
        self.assertEqual(server.live_kinds({'permissions': {'support'}}), ['support.message'])
        seen = self.send('POST', '/api/live/seen', {'id': owner['items'][0]['id']}).json()
        self.assertEqual((seen['seen'], seen['unread']), (owner['items'][0]['id'], 0))
        self.assertEqual(client().get('/api/live/recent').status_code, 401)
        self.assertEqual(self.get('/api/live/recent?limit=500').status_code, 422)

    def test_live_stream_sends_hello_replay_heartbeat_and_new_events(self):
        import threading
        start = server.store.live_max_id()
        server.store.remember_customer(850001, 'stream_before', 'До', '')
        timer = threading.Timer(0.35, lambda: server.store.remember_customer(850002, 'stream_after', 'После', ''))
        with patch.multiple(server, LIVE_STREAM_SECONDS=1.2, LIVE_POLL_SECONDS=0.05, LIVE_HEARTBEAT_SECONDS=0.2):
            timer.start()
            response = self.http.get('/api/live', headers={'Last-Event-ID': str(start)})
        timer.join()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'].startswith('text/event-stream'))
        self.assertNotIn('etag', response.headers)          # PanelGuard does not hold or tag the stream
        self.assertEqual(response.headers['x-accel-buffering'], 'no')
        self.assertEqual(response.headers['x-frame-options'], 'DENY')
        body = response.text
        self.assertTrue(body.startswith('retry: 3000'))
        self.assertIn('event: hello', body)
        self.assertIn(': ping', body)
        self.assertIn('@stream_before', body)               # replayed after Last-Event-ID
        self.assertIn('@stream_after', body)                # delivered by the shared poller
        self.assertEqual(body.count('@stream_before'), 1)
        self.assertEqual(server.live_hub.subscribers, [])   # the stream unsubscribed when it ended

    def test_live_stream_filters_by_role_and_caps_streams(self):
        _, support_http = self.staff_member('support')
        start = server.store.live_max_id()
        self.paid_order(860001)  # sells the last key: a low-stock event support staff may not see
        with patch.multiple(server, LIVE_STREAM_SECONDS=0.3, LIVE_POLL_SECONDS=0.05, LIVE_HEARTBEAT_SECONDS=0.1):
            body = support_http.get('/api/live', headers={'Last-Event-ID': str(start)}).text
        self.assertIn('Новый оплаченный заказ', body)
        self.assertNotIn('Товар закончился', body)
        with patch.multiple(server, LIVE_STREAM_SECONDS=0.3, LIVE_POLL_SECONDS=0.05, LIVE_HEARTBEAT_SECONDS=0.1):
            self.assertIn('Товар закончился', self.http.get('/api/live', headers={'Last-Event-ID': str(start)}).text)
        self.assertEqual(client().get('/api/live').status_code, 401)

    def test_live_hub_drops_oldest_stream_and_slow_subscribers(self):
        import asyncio

        async def scenario():
            hub = server.LiveHub()
            subs = [hub.subscribe('owner', ['customer.new']) for _ in range(server.LIVE_MAX_STREAMS + 1)]
            self.assertEqual(len(hub.subscribers), server.LIVE_MAX_STREAMS)
            self.assertIsNone(subs[0].queue.get_nowait())       # oldest closed
            hub.publish([{'id': 1, '_kind': 'payment.accepted'}])
            self.assertTrue(all(s.queue.empty() for s in subs[1:]))  # kind not permitted
            with patch.object(server, 'LIVE_QUEUE', 1):
                slow = hub.subscribe('anna', ['customer.new'])
                slow.queue = asyncio.Queue(maxsize=1)
                hub.publish([{'id': 2, '_kind': 'customer.new'}, {'id': 3, '_kind': 'customer.new'}])
            self.assertNotIn(slow, hub.subscribers)
            await hub.stop()
            self.assertEqual(hub.subscribers, [])
        asyncio.run(scenario())
