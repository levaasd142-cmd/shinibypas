"""Local admin API. Bind to loopback; use a TLS reverse proxy for remote access.

Every request except the public login page, its assets and ``GET /healthz`` passes
:class:`PanelGuard` first: session-cookie (or, for scripts, optional Basic) authentication with
per-IP throttling, a same-origin check for mutations and a request body limit. All of this happens
before FastAPI parses a single byte of the body. Each API route then checks one staff permission.

Staff accounts come from ``staff.py``. HTTP Basic auth (the owner account from ADMIN_USER /
ADMIN_PASSWORD) is accepted only for scripts and only with ``PANEL_BASIC_AUTH=1``.
"""
import asyncio
import base64
import binascii
import csv
import hashlib
import inspect
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import warnings
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect, cookie_parser

from analytics import CATEGORIES, MSK, Analytics
from backups import BackupBusy, BackupService, read_key
from broadcasts import Broadcasts
from channel_gate import ChannelGate, inspect_channel, normalize as normalize_channel
import emoji_text
from emoji_library import WELCOME_EMOJIS, EmojiPreviews, import_pack
from insights import Insights
from excel_export import analytics_xlsx
import staff as staff_module
from money import usd_cents
from notify import Notify
from promo import Promos
from staff import Staff
from store import Store
from support import Support

log = logging.getLogger('themarket.panel')
ROOT = Path(__file__).parent
WEB = ROOT / 'web'

# ---------------------------------------------------------------- configuration

MAX_ID = 2**63 - 1
BODY_LIMIT = 1024 * 1024
UPLOAD_LIMIT = 6 * 1024 * 1024
IMAGE_LIMIT = 5 * 1024 * 1024
UPLOAD_ROUTE = re.compile(r'^/api/products/[0-9]+/(stock|image)$')
AUTH_WINDOW = 600
AUTH_FAILURES = 10
SESSION_COOKIE = 'tm_session'
SESSION_MAX_AGE = 7 * 24 * 3600
PUBLIC_PATHS = {'/healthz', '/login', '/assets/panel.css', '/assets/login.js', '/assets/favicon.svg'}
GENERIC_ERROR = 'Запрос не выполнен. Проверьте введённые данные.'
INTERNAL_ERROR = 'Внутренняя ошибка. Подробности доступны в разделе «Ошибки».'
FORBIDDEN = 'Недостаточно прав для этого действия. Обратитесь к владельцу магазина.'
BAD_LOGIN = 'Неверный логин или пароль'
SECURITY_HEADERS = [
    (b'cache-control', b'no-store'),
    (b'x-content-type-options', b'nosniff'),
    (b'x-frame-options', b'DENY'),
    (b'referrer-policy', b'no-referrer'),
    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
                                 b"frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
]
PERMISSIONS = ('orders.view', 'orders.manage', 'customers.view', 'customers.balance', 'products.manage',
               'stock.manage', 'stock.reveal', 'support', 'promo.view', 'promo.manage', 'analytics', 'payments',
               'settings', 'broadcasts', 'backups', 'staff', 'audit')
FEATURES = ('payment_issues', 'staff', 'promo', 'support', 'notify', 'images', 'orders_page', 'segments')
ROLE_LABELS = {'owner': 'Владелец', 'manager': 'Менеджер', 'support': 'Поддержка'}

password = os.getenv('ADMIN_PASSWORD', '')
if len(password) < 16:
    raise RuntimeError('ADMIN_PASSWORD must contain at least 16 characters')

store = Store()
analytics = Analytics(store)
broadcasts = Broadcasts(store)
channel_gate = ChannelGate(store)
backups = BackupService(store.path, os.environ['STOCK_KEY'])
emoji_previews = EmojiPreviews(Path(store.path).parent / 'emoji-previews')
staff = Staff(store)
promos = Promos(store)
support = Support(store)
notify = Notify(store)
insights = Insights(store)
staff.ensure_owner(os.getenv('ADMIN_USER', 'owner'), password)


def panel_origins():
    """Explicit origins from PANEL_ORIGIN (comma separated), normalised."""
    return {o.strip().rstrip('/').lower() for o in os.getenv('PANEL_ORIGIN', '').split(',') if o.strip()}


def basic_auth_allowed():
    """Basic auth is only for scripts and only on request."""
    return os.getenv('PANEL_BASIC_AUTH', '') == '1'


def images_dir():
    return Path(store.path).parent / 'product-images'


# ---------------------------------------------------------------- helpers

def has_cyrillic(text):
    return bool(re.search('[а-яА-ЯёЁ]', text))


def public_message(exc, fallback=GENERIC_ERROR):
    """Only our own (Russian) messages reach the browser; library text never does."""
    text = str(exc)
    return text if text and has_cyrillic(text) and len(text) <= 500 else fallback


def audit(actor, action, target, level='info', details=None):
    with store.tx() as db:
        store.log(db, actor, action, target, level=level, details=details)


def backup_event(actor, action, target):
    audit(actor, action, target, level='error' if action == 'backup.failed' else 'info')


def internal_amount(value):
    if store.currency()['active']:
        return usd_cents(value)
    if value != value.to_integral_value():
        raise ValueError('До включения USD суммы должны быть целыми')
    return int(value)


def paginate(items, page, page_size):
    """Server-side slice for small in-memory collections (promo codes, staff)."""
    pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    return {'items': items[(page - 1) * page_size:page * page_size], 'total': len(items), 'page': page, 'pages': pages}


CSV_DANGEROUS = ('=', '+', '-', '@', '\t', '\r', '\n', ' ', '＝', '＋', '－', '＠')


def csv_cell(value):
    """Neutralise spreadsheet formulas in text cells; numbers stay numbers."""
    if value is None:
        return ''
    if isinstance(value, str):
        value = emoji_text.plain(value)
    if isinstance(value, str) and value.startswith(CSV_DANGEROUS):
        return "'" + value
    return value


def csv_line(values):
    output = io.StringIO()
    csv.writer(output, delimiter=';').writerow([csv_cell(v) for v in values])
    return output.getvalue()


def csv_response(rows, filename):
    def stream():
        yield '﻿'
        for row in rows:
            yield csv_line(row)
    return StreamingResponse(stream(), media_type='text/csv; charset=utf-8',
                             headers={'Content-Disposition': f'attachment; filename="{filename}"'})


def attachment(content, filename, media_type='application/octet-stream'):
    return Response(content, media_type=media_type, headers={'Content-Disposition': f'attachment; filename="{filename}"'})


def iso(ts, tz=timezone.utc):
    return datetime.fromtimestamp(ts, tz).isoformat() if ts else ''


# ---------------------------------------------------------------- guard middleware

class AuthThrottle:
    """Per-IP failure counter: AUTH_FAILURES failures inside AUTH_WINDOW lock the IP."""

    def __init__(self):
        self.failures = {}
        self.lock = threading.Lock()

    def _recent(self, ip, now):
        attempts = self.failures.get(ip)
        while attempts and attempts[0] < now - AUTH_WINDOW:
            attempts.popleft()
        if attempts is not None and not attempts:
            del self.failures[ip]
            return None
        return attempts

    def retry_after(self, ip):
        now = time.monotonic()
        with self.lock:
            attempts = self._recent(ip, now)
            if attempts and len(attempts) >= AUTH_FAILURES:
                return int(attempts[0] + AUTH_WINDOW - now) + 1
        return 0

    def fail(self, ip):
        now = time.monotonic()
        with self.lock:
            if len(self.failures) > 10000:
                self.failures.clear()
            self.failures.setdefault(ip, deque(maxlen=AUTH_FAILURES)).append(now)

    def reset(self, ip):
        with self.lock:
            self.failures.pop(ip, None)


throttle = AuthThrottle()


def check_credentials(header):
    """Return the user name for a valid Basic header, else None. Both parts are always compared."""
    if not header or not header[:6].lower() == 'basic ':
        return None
    try:
        user, _, given = base64.b64decode(header[6:].strip(), validate=True).decode('utf-8').partition(':')
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    user_ok = secrets.compare_digest(user.encode(), os.getenv('ADMIN_USER', 'owner').encode())
    password_ok = secrets.compare_digest(given.encode(), password.encode())
    return user if user_ok & password_ok else None


def role_permissions(role):
    """Permission names of a role (staff.py decides; unknown roles get nothing)."""
    return set(staff_module.permissions(role) or ())


def identity_of(member, mode='staff'):
    return {'id': member.get('id'), 'username': member['username'], 'role': member.get('role', 'owner'),
            'permissions': role_permissions(member.get('role', 'owner')), 'mode': mode}


def basic_identity(user):
    return {'id': None, 'username': user, 'role': 'owner', 'permissions': set(PERMISSIONS), 'mode': 'basic'}


def session_token(headers):
    return cookie_parser(headers.get('cookie', '')).get(SESSION_COOKIE, '')


def host_key(value):
    """'Example.com:443' / 'https://example.com' → 'example.com' (scheme-agnostic, default ports dropped)."""
    netloc = urlsplit(value).netloc if '://' in value else value
    netloc = netloc.strip().lower()
    for port in (':80', ':443'):
        if netloc.endswith(port):
            netloc = netloc[:-len(port)]
    return netloc


def same_origin(headers):
    """True when a browser request demonstrably comes from the panel itself."""
    site = headers.get('sec-fetch-site')
    if site and site not in ('same-origin', 'none'):
        return False
    origin = headers.get('origin')
    if origin:
        if origin == 'null':
            return False
        allowed = panel_origins()
        if allowed:
            return origin.rstrip('/').lower() in allowed
        return host_key(origin) == host_key(headers.get('host', ''))
    return site in ('same-origin', 'none')


def json_message(status, detail, extra_headers=()):
    body = json.dumps({'detail': detail}, ensure_ascii=False).encode()
    headers = [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode()), *extra_headers]
    return status, headers, body


def etag_for(body):
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def etag_matches(header, tag):
    if not header:
        return False
    return any(part.strip().removeprefix('W/') == tag for part in header.split(','))


class PanelGuard:
    """Pure ASGI middleware: authenticate, check origin and limit body size before any parsing.
    GET JSON answers carry an ETag; a matching If-None-Match gets an empty 304."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def replay(body, original):
        """Hand the buffered body to the app once, then pass through (e.g. http.disconnect)."""
        replayed = False

        async def receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await original()
        return receive

    @staticmethod
    async def identify(headers, ip):
        """→ (identity or None, failed Basic attempt?)."""
        token = session_token(headers)
        if token and len(token) <= 200:
            member = await asyncio.to_thread(staff.session, token)
            if member and member.get('active', True):
                return identity_of(member), False
        header = headers.get('authorization')
        if header and basic_auth_allowed():
            user = check_credentials(header)
            return (basic_identity(user), False) if user else (None, True)
        return None, False

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        started = False
        headers = Headers(scope=scope)
        path, method = scope['path'], scope['method']
        cacheable = method == 'GET' and path.startswith('/api/')
        held = None

        async def secure_send(message):
            nonlocal started, held
            if message['type'] == 'http.response.start':
                present = {name.lower() for name, _ in message.get('headers', [])}
                message['headers'] = [*message.get('headers', []),
                                      *[(k, v) for k, v in SECURITY_HEADERS if k not in present]]
                content_type = dict(message['headers']).get(b'content-type', b'')
                if cacheable and message.get('status') == 200 and content_type.startswith(b'application/json'):
                    held = message
                    return
                started = True
                return await send(message)
            if held is not None and message['type'] == 'http.response.body':
                start, held = held, None
                started = True
                if message.get('more_body'):
                    await send(start)
                    return await send(message)
                body = message.get('body', b'')
                tag = etag_for(body)
                start['headers'] = [(k, v) for k, v in start['headers'] if k.lower() != b'etag'] + [(b'etag', tag.encode())]
                if etag_matches(headers.get('if-none-match'), tag):
                    start['status'] = 304
                    start['headers'] = [(k, v) for k, v in start['headers'] if k.lower() not in (b'content-length', b'content-type')]
                    await send(start)
                    return await send({'type': 'http.response.body', 'body': b''})
                await send(start)
                return await send(message)
            await send(message)

        async def reply(status, response_headers, body):
            await secure_send({'type': 'http.response.start', 'status': status, 'headers': response_headers})
            await secure_send({'type': 'http.response.body', 'body': body})

        if path in PUBLIC_PATHS and method in ('GET', 'HEAD'):
            return await self.app(scope, receive, secure_send)
        ip = (scope.get('client') or ('unknown', 0))[0]
        wait = throttle.retry_after(ip)
        if wait:
            return await reply(*json_message(429, 'Слишком много неудачных входов. Повторите позже.',
                                             [(b'retry-after', str(wait).encode())]))
        identity, failed = await self.identify(headers, ip)
        if failed:
            throttle.fail(ip)
        login = path == '/api/login' and method == 'POST'
        if not identity and not login:
            if not path.startswith('/api/') and method in ('GET', 'HEAD'):
                return await reply(303, [(b'location', b'/login'), (b'content-length', b'0')], b'')
            return await reply(*json_message(401, 'Войдите в панель заново'))
        state = scope.setdefault('state', {})
        if identity:
            state['actor'] = identity['username']
            state['identity'] = identity
        if method not in ('GET', 'HEAD', 'OPTIONS') and not same_origin(headers):
            return await reply(*json_message(403, 'Недопустимый источник запроса'))
        limit = UPLOAD_LIMIT if UPLOAD_ROUTE.match(path) else BODY_LIMIT
        length = headers.get('content-length')
        if length is not None and (not length.isdigit() or int(length) > limit):
            return await reply(*json_message(413, 'Слишком большой запрос'))
        if method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            chunks, size = [], 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                size += len(chunk)
                if size > limit:
                    return await reply(*json_message(413, 'Слишком большой запрос'))
                chunks.append(chunk)
                if not message.get('more_body'):
                    break
            receive = self.replay(b''.join(chunks), receive)
        try:
            await self.app(scope, receive, secure_send)
        except ClientDisconnect:
            return  # the browser went away mid-response (e.g. a closed live stream): nothing to report
        except Exception as exc:
            route = getattr(scope.get('route'), 'path', None) or path
            log.exception('Unhandled error in %s %s', method, route)
            try:
                audit('panel', 'server.error', 'panel', level='error',
                      details={'reason': f'{type(exc).__name__} · {method} {route}'})
            except Exception:
                log.exception('Could not record the panel error')
            if started:
                raise
            held = None
            await reply(*json_message(500, INTERNAL_ERROR))


# ---------------------------------------------------------------- application

def scheduled_backup():
    try:
        result = backups.create(only_if_due=True)
        if result:
            backup_event('system', 'backup.created', result['name'])
    except BackupBusy:
        pass
    except Exception:
        # Never log raw exceptions: paths or secret material can be included.
        if not backups.last_error:
            backup_event('system', 'backup.failed', 'backups')
        backups.last_error = 'Автоматическая копия не создана. Проверьте ключ, место на диске и доступ к папке.'


@asynccontextmanager
async def lifespan(app):
    async def worker():
        while True:
            await asyncio.to_thread(scheduled_backup)
            await asyncio.sleep(60)
    task = asyncio.create_task(worker())
    try:
        yield
    finally:
        await live_hub.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title='TheMarket', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(PanelGuard)
app.mount('/assets', StaticFiles(directory=WEB), name='assets')


def current_identity(request: Request):
    identity = request.scope.get('state', {}).get('identity')
    if not identity:
        raise HTTPException(401, 'Войдите в панель заново')
    return identity


def require(*names):
    """Dependency: the signed-in staff member must hold at least one of the permissions."""
    def dependency(request: Request):
        identity = current_identity(request)
        if names and not identity['permissions'] & set(names):
            raise HTTPException(403, FORBIDDEN)
        return identity['username']
    dependency.permissions = names or ('signed-in',)
    return dependency


def Perm(*names):
    return Annotated[str, Depends(require(*names))]


def browser_same_origin(request: Request):
    """For GETs with side effects (audit, secrets): Basic auth is sent cross-site, so check the source."""
    headers = request.headers
    if headers.get('x-themarket') == '1' or headers.get('sec-fetch-site') in ('same-origin', 'none'):
        return
    raise HTTPException(403, 'Недопустимый источник запроса')


def has(request, permission):
    identity = request.scope.get('state', {}).get('identity') or {}
    return permission in identity.get('permissions', ())


SignedIn = Perm()
SameOrigin = Depends(browser_same_origin)
Id = Annotated[int, PathParam(gt=0, le=MAX_ID)]
Page = Annotated[int, Query(ge=1, le=100000)]
PageSize = Annotated[int, Query(ge=1, le=200)]
Stamp = Annotated[float, Query(ge=0, le=10**11)]
OrderId = Annotated[str, PathParam(min_length=1, max_length=64)]
Text = Annotated[str, Query(max_length=200)]


# ---------------------------------------------------------------- errors

FIELD_LABELS = {
    'name': 'Название', 'category': 'Категория', 'price': 'Цена', 'cost_price': 'Себестоимость', 'mode': 'Выдача',
    'description': 'Описание', 'instruction': 'Инструкция', 'emoji_id': 'ID эмодзи', 'lines': 'Сток',
    'unit_cost': 'Себестоимость', 'amount': 'Сумма', 'delta': 'Сумма', 'reason': 'Причина', 'note': 'Описание',
    'occurred_date': 'Дата', 'order_id': 'Номер заказа', 'result': 'Результат', 'stars_per_usd': 'Курс',
    'wallet': 'Адрес кошелька', 'api_key': 'API-ключ', 'title': 'Название', 'text': 'Текст', 'text_en': 'Английский текст',
    'button_text': 'Название кнопки', 'url': 'Ссылка', 'product_id': 'Товар', 'channel': 'Канал',
    'join_url': 'Пригласительная ссылка', 'target': 'Назначение', 'kind': 'Вид', 'reference': 'Идентификатор',
    'username': 'Логин', 'password': 'Пароль', 'role': 'Роль', 'badge': 'Бейдж', 'old_price': 'Старая цена',
    'code': 'Код', 'value': 'Размер скидки', 'min_order': 'Минимальная сумма', 'max_uses': 'Лимит использований',
    'per_user': 'Лимит на покупателя', 'starts_at': 'Начало', 'ends_at': 'Окончание', 'product_ids': 'Товары',
    'chat_ids': 'Чаты', 'low_stock_threshold': 'Порог остатка', 'daily_summary_time': 'Время сводки',
    'scheduled_at': 'Время запуска', 'days': 'Дней', 'language': 'Язык', 'direction': 'Направление',
    'tags': 'Метки', 'ids': 'Товары', 'action': 'Действие',
}


def validation_message(error):
    kind, ctx = error.get('type', ''), error.get('ctx') or {}
    if kind == 'missing':
        return 'заполните поле'
    if kind == 'string_too_short':
        return 'заполните поле' if ctx.get('min_length') == 1 else f"не меньше {ctx.get('min_length')} символов"
    if kind == 'string_too_long':
        return f"не больше {ctx.get('max_length')} символов"
    if kind == 'string_pattern_mismatch':
        return 'неверный формат'
    if kind in ('greater_than', 'greater_than_equal'):
        bound = ctx.get('gt', ctx.get('ge'))
        return f'должно быть больше {bound}' if kind == 'greater_than' else f'должно быть не меньше {bound}'
    if kind in ('less_than', 'less_than_equal'):
        return f"должно быть не больше {ctx.get('lt', ctx.get('le'))}"
    if kind == 'decimal_max_places':
        return f"не больше {ctx.get('decimal_places')} знаков после точки"
    if kind in ('decimal_max_digits', 'decimal_whole_digits'):
        return 'слишком большое число'
    if kind in ('decimal_parsing', 'decimal_type', 'float_parsing', 'int_parsing', 'int_from_float',
                'float_type', 'int_type', 'finite_number'):
        return 'введите число'
    if kind in ('literal_error', 'enum'):
        return 'недопустимое значение'
    if kind in ('bool_parsing', 'bool_type'):
        return 'ожидается «да» или «нет»'
    if kind in ('string_type',):
        return 'ожидается текст'
    if kind in ('too_long', 'list_type'):
        return 'слишком длинный список' if kind == 'too_long' else 'ожидается список'
    return 'некорректное значение'


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    errors = []
    for error in exc.errors():
        loc = [part for part in error.get('loc', ()) if not isinstance(part, int)]
        if error.get('type') == 'json_invalid' or len(loc) < 2:
            errors.append({'field': None, 'message': 'Некорректное тело запроса'})
            continue
        source, field = loc[0], str(loc[-1])
        if source in ('path', 'query'):
            errors.append({'field': None, 'message': 'Некорректный адрес запроса'})
            continue
        label = FIELD_LABELS.get(field, 'Поле')
        errors.append({'field': field, 'message': f'{label}: {validation_message(error)}'})
    first = errors[0] if errors else {'field': None, 'message': GENERIC_ERROR}
    return JSONResponse({'detail': first['message'], 'field': first['field'], 'errors': errors}, status_code=422)


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    detail = exc.detail if isinstance(exc.detail, str) and has_cyrillic(exc.detail) else {
        404: 'Раздел не найден', 405: 'Метод не поддерживается. Обновите панель.'}.get(exc.status_code, GENERIC_ERROR)
    return JSONResponse({'detail': detail}, status_code=exc.status_code, headers=getattr(exc, 'headers', None))


@app.exception_handler(BackupBusy)
async def busy(request, exc):
    return JSONResponse({'detail': public_message(exc, 'Операция уже выполняется')}, status_code=409)


@app.exception_handler(emoji_text.FieldError)
async def field_value(request, exc):
    message = public_message(exc)
    body = {'detail': message}
    if exc.field:
        body.update(field=exc.field, errors=[{'field': exc.field, 'message': message}])
    return JSONResponse(body, status_code=422)


@app.exception_handler(ValueError)
async def bad_value(request, exc):
    message = public_message(exc)
    status = 404 if 'не найден' in message.lower() else 400
    return JSONResponse({'detail': message}, status_code=status)


# ---------------------------------------------------------------- shell, login & session

@app.get('/healthz', include_in_schema=False)
def healthz():
    return {'ok': True}


@app.get('/login', include_in_schema=False)
def login_page():
    return FileResponse(WEB / 'login.html')


@app.get('/')
def index(actor: SignedIn):
    return FileResponse(WEB / 'index.html')


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


def is_https(request):
    return request.url.scheme == 'https' or request.headers.get('x-forwarded-proto', '').split(',')[0].strip().lower() == 'https'


def client_ip(request):
    return request.client.host if request.client else 'unknown'


@app.post('/api/login')
def login(body: LoginInput, request: Request):
    """Public: exchanges a staff login for an HttpOnly session cookie. Failures feed the lockout."""
    ip = client_ip(request)
    member = staff.authenticate(body.username.strip(), body.password)
    if not member or not member.get('active', True):
        throttle.fail(ip)
        raise HTTPException(401, BAD_LOGIN)
    token = staff.create_session(member['id'], ip, request.headers.get('user-agent', '')[:200])
    audit(member['username'], 'staff.login', member['username'])
    response = JSONResponse({'ok': True, 'username': member['username'], 'role': member.get('role')})
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, path='/', httponly=True,
                        samesite='strict', secure=is_https(request))
    return response


@app.post('/api/logout')
def logout(request: Request, actor: SignedIn):
    token = session_token(request.headers)
    if token:
        staff.revoke_session(token)
    response = JSONResponse({'ok': True})
    response.delete_cookie(SESSION_COOKIE, path='/', httponly=True, samesite='strict', secure=is_https(request))
    return response


def me(identity):
    return {'username': identity['username'], 'role': identity['role'], 'role_label': ROLE_LABELS.get(identity['role'], identity['role']),
            'permissions': sorted(identity['permissions']), 'mode': identity['mode']}


@app.get('/api/me')
def who_am_i(request: Request, actor: SignedIn):
    return me(current_identity(request))


def open_payment_issues():
    return store.payment_issues(open_only=True)


def customers_page(query='', page=1, page_size=50, tag='', banned=False):
    return store.customers_page(query=query, page=page, page_size=page_size, tag=tag, banned=banned)


def support_unread():
    try:
        return support.unread_count()
    except Exception:
        log.warning('Support unread count failed', exc_info=True)
        return 0


@app.get('/api/state')
def state(request: Request, actor: SignedIn):
    """Small snapshot polled by the panel: counters only, never lists (products come from /api/products)."""
    identity = current_identity(request)
    result = {'currency': store.currency(), 'bot_configured': bool(os.getenv('BOT_TOKEN')), 'actor': actor,
              'me': me(identity),
              'features': dict.fromkeys(FEATURES, True)}
    if has(request, 'orders.view'):
        result['metrics'] = store.metrics()
        if not has(request, 'customers.balance'):
            result['metrics'].pop('wallet_total', None)
    if has(request, 'payments'):
        result['payment_issues'] = len(open_payment_issues())
    if has(request, 'support'):
        result['support_unread'] = support_unread()
    if has(request, 'customers.view'):
        result['customers_total'] = store.customers_page(page_size=1)['total']
    return result


def product_list():
    items = store.products()
    return sorted(items, key=lambda p: (p.get('category') or '', p.get('sort') or 0, p['id']))


def strip_costs(request, products):
    """Purchase prices are analytics data: other roles get the catalog without them."""
    if has(request, 'analytics'):
        return products
    return [{k: v for k, v in p.items() if k != 'cost_price'} for p in products]


def strip_balances(request, customers):
    """Wallet balances and spend need 'customers.balance' (support sees who, not how much)."""
    if has(request, 'customers.balance'):
        return customers
    return [{k: v for k, v in c.items() if k not in BALANCE_FIELDS} for c in customers]


BALANCE_FIELDS = ('balance', 'spent', 'spent_cents', 'wallet')


@app.get('/api/products')
def products(request: Request, actor: Perm('products.manage', 'orders.view', 'broadcasts', 'promo.view')):
    """The whole catalog (hundreds of rows at most); polled with If-None-Match, so usually a 304."""
    return strip_costs(request, product_list())


# ---------------------------------------------------------------- orders (server-side pages)

OrderStatus = Annotated[Literal['', 'attention', 'paid', 'pending', 'expired', 'awaiting_details', 'queued', 'processing',
                                'fulfilled', 'needs_review'], Query()]


def orders_page(status='', query='', page=1, page_size=50):
    """'attention' = paid orders still waiting for the shop (details, queue, work, review)."""
    return store.orders_page(status=status, query=query, page=page, page_size=page_size)


@app.get('/api/orders')
def orders(actor: Perm('orders.view'), status: OrderStatus = '', query: Text = '', page: Page = 1, page_size: PageSize = 25):
    return orders_page(status, query, page, page_size)


ORDER_LABELS = {'pending': 'Ожидает оплаты', 'expired': 'Истёк', 'awaiting_details': 'Ждём данные', 'queued': 'В очереди',
                'processing': 'В работе', 'fulfilled': 'Выполнен', 'needs_review': 'Нужна проверка'}


@app.get('/api/orders/export')
def export_orders(actor: Perm('orders.view'), status: OrderStatus = '', query: Text = ''):
    usd_active = store.currency()['active']
    amount = (lambda v: Decimal(v or 0) / 100) if usd_active else (lambda v: v or 0)

    def rows():
        yield ['Заказ', 'Создан UTC', 'Telegram ID', 'Товар', 'Количество', 'Сумма ' + ('USD' if usd_active else 'Stars'),
               'Статус', 'Выдача']
        page, pages = 1, 1
        while page <= pages and page <= 500:
            batch = orders_page(status, query, page, 200)
            pages = batch['pages']
            for o in batch['items']:
                yield [o['id'], iso(o.get('created')), o['user_id'], o['title'], o.get('quantity') or 1, amount(o['price']),
                       ORDER_LABELS.get(o['status'], o['status']), 'Автовыдача' if o.get('mode') == 'auto' else 'Менеджер']
            page += 1
    return csv_response(rows(), 'themarket-orders.csv')


@app.get('/api/customers')
def customers(request: Request, actor: Perm('customers.view'), query: Text = '', page: Page = 1, page_size: PageSize = 50,
              tag: Annotated[str, Query(max_length=40)] = '', banned: bool = False):
    """Items carry CRM tags and the banned flag; tag/banned filter the list."""
    result = customers_page(query, page, page_size, tag, banned)
    return {**result, 'items': strip_balances(request, result['items'])}


@app.get('/api/customers/export')
def export_customers(request: Request, actor: Perm('customers.view'), query: Text = ''):
    usd = store.currency()['active']
    amount = (lambda v: Decimal(v or 0) / 100) if usd else (lambda v: v or 0)
    unit = 'USD' if usd else 'Stars'
    money = has(request, 'customers.balance')

    def rows():
        yield (['Telegram ID', 'Username', 'Имя', 'Фамилия'] + (['Баланс ' + unit] if money else []) + ['Заказов']
               + (['Покупки ' + unit] if money else []) + ['Первый вход UTC', 'Активность UTC'])
        page, pages = 1, 1
        while page <= pages:
            batch = customers_page(query, page, 200)
            pages = batch['pages']
            for c in batch['items']:
                yield ([c['user_id'], c.get('username') or '', c.get('first_name') or '', c.get('last_name') or '']
                       + ([amount(c.get('balance'))] if money else []) + [c.get('orders') or 0]
                       + ([amount(c.get('spent'))] if money else []) + [iso(c.get('first_seen')), iso(c.get('last_seen'))])
            page += 1
    return csv_response(rows(), 'themarket-customers.csv')


# ---------------------------------------------------------------- dashboard & search

def low_stock_threshold():
    try:
        return int(notify.settings().get('low_stock_threshold') or 3)
    except Exception:
        log.warning('Notify settings unavailable', exc_info=True)
    return 5


@app.get('/api/dashboard')
def dashboard(request: Request, actor: Perm('orders.view')):
    result = analytics.daily_sales()
    threshold = low_stock_threshold()
    low = [p for p in product_list() if p['active'] and p['mode'] == 'auto' and p.get('available', 0) <= threshold]
    result['low_stock'] = {'threshold': threshold, 'total': len(low),
                           'items': [{k: p.get(k) for k in ('id', 'name', 'category', 'available')} for p in low[:6]]}
    result['recent_orders'] = orders_page('', '', 1, 8)['items']
    if has(request, 'support'):
        tickets = support.tickets(status='open', page=1, page_size=5)
        result['support'] = {'open': tickets['total'], 'unread': support_unread(), 'items': tickets['items']}
    return result


@app.get('/api/search')
def search(request: Request, actor: SignedIn, q: Annotated[str, Query(min_length=1, max_length=100)]):
    """Command palette: orders by id, customers by id/username, products by name (5–6 hits each)."""
    q = q.strip()
    result = {'orders': [], 'customers': [], 'products': []}
    if not q:
        return result
    if has(request, 'orders.view') and len(q) >= 3:
        result['orders'] = orders_page('', q.lstrip('#'), 1, 5)['items']
    if has(request, 'customers.view'):
        result['customers'] = strip_balances(request, customers_page(q, 1, 5)['items'])
    if has(request, 'products.manage') or has(request, 'orders.view'):
        needle = q.casefold()
        result['products'] = [{k: p.get(k) for k in ('id', 'name', 'category', 'price', 'available', 'mode', 'active')}
                              for p in product_list() if needle in (p['name'] + ' ' + p['category']).casefold()][:6]
    return result


# ---------------------------------------------------------------- payment issues

class ResolveInput(BaseModel):
    note: str = Field(min_length=1, max_length=300)


@app.get('/api/payment-issues')
def payment_issues(actor: Perm('payments'), open_only: bool = True):
    return {'available': True, 'items': store.payment_issues(open_only=open_only)}


@app.post('/api/payment-issues/{issue_id}/resolve')
def resolve_payment_issue(issue_id: Id, body: ResolveInput, actor: Perm('payments')):
    store.resolve_payment_issue(issue_id, body.note.strip(), actor)
    return {'ok': True}


# ---------------------------------------------------------------- bot settings

class ChannelInput(BaseModel):
    enabled: bool = False
    channel: str = Field(default='', max_length=100)
    join_url: str = Field(default='', max_length=250)


async def channel_inspection(data):
    from aiogram import Bot  # heavy import, only needed for this action
    token = os.getenv('BOT_TOKEN', '')
    if not token:
        raise ValueError('Сначала настройте токен бота в окне запуска и перезапустите панель')
    try:
        async with Bot(token) as bot:
            return await inspect_channel(bot, data.channel, data.join_url)
    except ValueError:
        raise
    except Exception:
        log.warning('Telegram channel inspection failed', exc_info=True)
        raise HTTPException(502, 'Telegram не ответил или токен бота недействителен. Настройки не изменены; '
                                 'повторите проверку позже.') from None


@app.get('/api/channel-gate')
def channel_settings(actor: Perm('settings')):
    return channel_gate.settings()


@app.post('/api/channel-gate/check')
async def check_channel(data: ChannelInput, actor: Perm('settings')):
    return await channel_inspection(data)


@app.put('/api/channel-gate')
async def save_channel(data: ChannelInput, actor: Perm('settings')):
    if data.enabled:
        settings = await channel_inspection(data)
    else:
        settings = normalize_channel(data.channel, data.join_url)
        old = channel_gate.settings()
        if settings['channel_ref'].lower() == old['channel_ref'].lower():
            settings = {**old, **settings}
    return channel_gate.save(data.enabled, settings, actor)


# ---------------------------------------------------------------- broadcasts

class Segment(BaseModel):
    type: Literal['all', 'buyers', 'non_buyers', 'language', 'product', 'inactive'] = 'all'
    language: Literal['ru', 'en'] | None = None
    product_id: int | None = Field(default=None, gt=0, le=MAX_ID)
    days: int | None = Field(default=None, ge=1, le=3650)


class BroadcastDraft(BaseModel):
    reference: str = Field(max_length=36)
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=6000)
    text_en: str = Field(default='', max_length=6000)
    button_text: str = Field(default='', max_length=40)
    target: Literal['catalog', 'product', 'url', 'none'] = 'catalog'
    url: str = Field(default='', max_length=1000)
    product_id: int | None = Field(default=None, gt=0, le=MAX_ID)
    with_banner: bool = False
    segment: Segment = Field(default_factory=Segment)
    scheduled_at: float | None = Field(default=None, ge=0, le=10**11)


class BroadcastSettings(BaseModel):
    new_product: bool
    restock: bool


CampaignId = Annotated[str, PathParam(min_length=1, max_length=64)]


def segment_dict(segment):
    data = segment.model_dump()
    if data['type'] != 'language':
        data.pop('language')
    if data['type'] != 'product':
        data.pop('product_id')
    if data['type'] != 'inactive':
        data.pop('days')
    elif not data.get('days'):
        raise ValueError('Укажите, сколько дней покупатель не заходил')
    if data['type'] == 'language' and not data.get('language'):
        raise ValueError('Выберите язык аудитории')
    if data['type'] == 'product' and not data.get('product_id'):
        raise ValueError('Выберите товар для аудитории')
    return data


def draft_data(data):
    content = data.model_dump()
    content['segment'] = segment_dict(data.segment)
    if data.scheduled_at is not None and data.scheduled_at < time.time() - 60:
        raise ValueError('Время запуска уже прошло')
    return content


@app.get('/api/broadcasts')
def broadcast_list(actor: Perm('broadcasts'), page: Page = 1):
    return broadcasts.catalog(page)


@app.put('/api/broadcasts/settings')
def broadcast_settings(data: BroadcastSettings, actor: Perm('broadcasts')):
    broadcasts.save_settings(data.new_product, data.restock, actor)
    return {'ok': True}


@app.get('/api/broadcasts/banner')
def broadcast_banner(actor: Perm('broadcasts')):
    return FileResponse(ROOT / 'assets' / 'welcome.png', media_type='image/png')


@app.get('/api/broadcasts/audience')
def broadcast_audience(actor: Perm('broadcasts'), type: Annotated[Literal['all', 'buyers', 'non_buyers', 'language', 'product', 'inactive'], Query()] = 'all',
                       language: Annotated[Literal['', 'ru', 'en'], Query()] = '',
                       product_id: Annotated[int, Query(ge=0, le=MAX_ID)] = 0, days: Annotated[int, Query(ge=0, le=3650)] = 0):
    segment = segment_dict(Segment(type=type, language=language or None, product_id=product_id or None, days=days or None))
    return broadcasts.preview(segment)


@app.post('/api/broadcasts')
def broadcast_draft(data: BroadcastDraft, actor: Perm('broadcasts')):
    return broadcasts.draft(data.reference, data.title, draft_data(data), actor)


@app.get('/api/broadcasts/{campaign_id}')
def broadcast_detail(campaign_id: CampaignId, actor: Perm('broadcasts')):
    return broadcasts.detail(campaign_id)


@app.put('/api/broadcasts/{campaign_id}')
def broadcast_edit(campaign_id: CampaignId, data: BroadcastDraft, actor: Perm('broadcasts')):
    return broadcasts.draft(campaign_id, data.title, draft_data(data), actor, replace=True)


@app.post('/api/broadcasts/{campaign_id}/{action}')
def broadcast_action(campaign_id: CampaignId, action: Literal['start', 'pause', 'resume', 'cancel'], actor: Perm('broadcasts')):
    return broadcasts.action(campaign_id, action, actor)


# ---------------------------------------------------------------- input models

Cost = Field(default=None, ge=0, le=10000000000, decimal_places=2)


class Product(BaseModel):
    cost_price: Decimal | None = Cost
    emoji_id: str | None = Field(default=None, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=2120)
    description: str = Field(default='', max_length=3000)
    price: Decimal = Field(gt=0, le=1000000, decimal_places=2)
    mode: Literal['auto', 'manual']
    instruction: str = Field(default='', max_length=2800)
    active: bool = True
    badge: str = Field(default='', max_length=16)
    old_price: Decimal | None = Field(default=None, gt=0, le=1000000, decimal_places=2)


class Stock(BaseModel):
    lines: str = Field(min_length=1, max_length=500000)
    unit_cost: Decimal | None = Cost


class CostInput(BaseModel):
    amount: Decimal = Field(ge=0, le=10000000000, decimal_places=2)
    reason: str = Field(default='', max_length=300)


class FinanceInput(BaseModel):
    reference: str = Field(pattern=r'^[a-f0-9-]{36}$')
    kind: Literal['expense', 'refund', 'replacement']
    category: Literal['advertising', 'server', 'fees', 'other', 'refund', 'replacement']
    amount: Decimal = Field(gt=0, le=10000000000, decimal_places=2)
    note: str = Field(min_length=1, max_length=300)
    occurred_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    order_id: str | None = Field(default=None, max_length=32)


class ReasonInput(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class Result(BaseModel):
    result: str = Field(min_length=1, max_length=2000)


class Adjustment(BaseModel):
    delta: Decimal = Field(ge=-100000, le=100000, decimal_places=2)
    reason: str = Field(min_length=1, max_length=300)
    reference: str = Field(pattern=r'^[a-f0-9-]{36}$')


class CurrencyRate(BaseModel):
    stars_per_usd: str = Field(min_length=1, max_length=30)


class BalanceTarget(BaseModel):
    amount: Decimal = Field(ge=0, le=10000000000, decimal_places=2)
    reason: str = Field(min_length=1, max_length=300)
    reference: str = Field(pattern=r'^[a-f0-9-]{36}$')


class EmojiPackInput(BaseModel):
    name: str = Field(min_length=1, max_length=300)


class EmojiAssignment(BaseModel):
    target: str = Field(min_length=1, max_length=200)
    emoji_id: str = Field(pattern=r'^[0-9]{0,20}$')


class PaymentMethodInput(BaseModel):
    enabled: bool
    wallet: str = Field(max_length=150)
    api_key: str = Field(default='', max_length=500)
    clear_key: bool = False


class StarsSwitch(BaseModel):
    enabled: bool


# ---------------------------------------------------------------- backups

BackupName = Annotated[str, PathParam(min_length=1, max_length=120)]


@app.get('/api/backups')
def backup_list(actor: Perm('backups')):
    return backups.catalog()


@app.post('/api/backups')
def backup_create(actor: Perm('backups')):
    result = backups.create()
    backup_event(actor, 'backup.created', result['name'])
    return result


@app.post('/api/backups/recovery-key')
def backup_recovery_key(actor: Perm('backups')):
    with backups.locked():
        key = read_key(backups.key_file) if backups.key_file.exists() else backups.recovery_key()
    backup_event(actor, 'backup.key.export', 'backups')
    return attachment(key + b'\n', 'themarket-recovery.key')


def full_verify(name):
    """Explicit check: a full restore test, even when a cheap cached verification exists."""
    if 'full' in inspect.signature(backups.verify).parameters:
        return backups.verify(name, full=True)
    return backups.verify(name)


@app.post('/api/backups/{name}/verify')
def backup_verify(name: BackupName, actor: Perm('backups')):
    result = full_verify(name)
    backup_event(actor, 'backup.verified', name)
    return result


@app.post('/api/backups/{name}/download')
def backup_download(name: BackupName, actor: Perm('backups')):
    backups.verify(name)
    backup_event(actor, 'backup.download', name)
    return FileResponse(backups.archive_path(name), media_type='application/octet-stream', filename=name)


# ---------------------------------------------------------------- payment settings & emojis

@app.get('/api/payment-methods')
def payment_methods(actor: Perm('settings')):
    return store.payment_methods()


@app.put('/api/payment-methods/stars')
def stars_switch(body: StarsSwitch, actor: Perm('settings')):
    store.set_stars_enabled(body.enabled, actor)
    return {'ok': True}


@app.put('/api/payment-methods/{network}')
def payment_method(network: Literal['bsc', 'ton', 'tron', 'aptos'], body: PaymentMethodInput, actor: Perm('settings')):
    store.save_payment_method(network, body.enabled, body.wallet, body.api_key, actor, body.clear_key)
    return {'ok': True}


@app.post('/api/currency')
def set_currency(body: CurrencyRate, actor: Perm('settings')):
    return store.set_currency(body.stars_per_usd, actor)


@app.get('/api/emojis')
def emoji_library(actor: Perm('products.manage', 'broadcasts')):
    packs = store.emoji_packs()
    for pack in packs:
        pack['items'] = [{'id': item['id'], 'emoji': item['emoji'], 'has_preview': bool(item['file_id'])}
                         for item in pack['items']]
    return {'packs': packs, 'assignments': store.emoji_assignments(),
            'defaults': {target: emoji_id for target, (emoji_id, _) in WELCOME_EMOJIS.items()}}


@app.post('/api/emojis/packs')
async def add_emoji_pack(body: EmojiPackInput, actor: Perm('products.manage')):
    try:
        name, title, items = await import_pack(os.getenv('BOT_TOKEN', ''), body.name)
    except ValueError:
        raise
    except Exception:
        log.warning('Emoji pack import failed', exc_info=True)
        raise HTTPException(502, 'Telegram не ответил. Повторите загрузку набора позже.') from None
    store.save_emoji_pack(name, title, items, actor)
    return {'ok': True, 'count': len(items)}


@app.post('/api/emojis/assign')
def assign_emoji(body: EmojiAssignment, actor: Perm('products.manage')):
    store.assign_emoji(body.target, body.emoji_id, actor)
    return {'ok': True}


@app.get('/api/emojis/{emoji_id}/preview')
async def emoji_preview(emoji_id: Annotated[str, PathParam(pattern=r'^[0-9]{1,20}$')], actor: SignedIn):
    item = next((item for pack in store.emoji_packs() for item in pack['items'] if item['id'] == emoji_id), None)
    if not item:
        raise HTTPException(404, 'Эмодзи не найден')
    try:
        path = await emoji_previews.get(os.getenv('BOT_TOKEN', ''), emoji_id, item['file_id'])
    except Exception:
        raise HTTPException(404, 'Превью недоступно') from None
    return FileResponse(path, media_type='image/png')


# ---------------------------------------------------------------- events & payments

@app.get('/api/events')
def events(actor: Perm('audit'), query: Text = '', category: Text = '', level: Text = '',
           after: Stamp = 0, before: Stamp = 0, page: Page = 1):
    return store.events(query, category, level, after, before, page)


@app.get('/api/events/export')
def export_events(actor: Perm('audit'), query: Text = '', category: Text = '', level: Text = '',
                  after: Stamp = 0, before: Stamp = 0):
    cutoff = before or time.time()
    first = store.events(query, category, level, after, cutoff, 1, 500)

    def rows():
        yield ['Время UTC', 'Уровень', 'Раздел', 'Событие', 'Кто', 'Покупатель', 'Заказ', 'Детали']
        for page in range(1, first['pages'] + 1):
            batch = first if page == 1 else store.events(query, category, level, after, cutoff, page, 500)
            for event in batch['items']:
                yield [iso(event['at']), event['level'], event['category'], event['summary'] or event['action'],
                       event['actor'], event['user_id'] or '', event['order_id'] or '', event['details']]
    return csv_response(rows(), 'themarket-events.csv')


@app.get('/api/payments')
def payments(actor: Perm('payments')):
    """Bounded by the store: the latest 300 crypto deposits and 300 Stars payments."""
    return store.payment_list()


@app.post('/api/payments/{deposit_id}/retry')
def retry_payment(deposit_id: Annotated[str, PathParam(min_length=1, max_length=64)], actor: Perm('payments')):
    store.retry_crypto_check(deposit_id, actor)
    return {'ok': True}


# ---------------------------------------------------------------- analytics

Period = Annotated[str, Query(max_length=20)]
Day = Annotated[str, Query(max_length=10)]


@app.get('/api/analytics')
def sales_analytics(actor: Perm('analytics'), period: Period = 'month', start: Day = '', end: Day = '',
                    page: Page = 1, expense_page: Page = 1):
    return analytics.report(period, start, end, page, expense_page)


def usd(value):
    return (Decimal(value) / 100).quantize(Decimal('0.01')) if value is not None else 'Не указана'


@app.get('/api/analytics/export')
def export_analytics(actor: Perm('analytics'), period: Period = 'month', start: Day = '', end: Day = ''):
    report = analytics.report(period, start, end, full=True)

    def rows():
        s = report['summary']
        yield ['Период МСК', report['start'], report['end']]
        yield ['Выручка USD', 'Себестоимость известная USD', 'Расходы USD', 'Возвраты USD', 'Прибыль USD',
               'Заказов без себестоимости']
        yield [usd(s['revenue']), usd(s['cost']), usd(s['expenses']), usd(s['refunds']), usd(s['profit']),
               s['unknown_cost_orders']]
        yield ['Дата МСК', 'Заказ', 'Товар', 'Количество', 'Продажа USD', 'Себестоимость USD']
        for sale in report['sales']['items']:
            yield [iso(sale['sold_at'], MSK), sale['id'], sale['title'], sale['quantity'], usd(sale['price']),
                   usd(sale['cost_total'])]
        yield ['Дата МСК', 'Операция', 'Категория', 'Сумма USD', 'Описание', 'Заказ', 'Отменена']
        for entry in report['entries']['items']:
            yield [datetime.fromtimestamp(entry['occurred_at'], MSK).date().isoformat(), entry['kind'],
                   CATEGORIES[entry['category']], usd(entry['amount']), entry['note'], entry['order_id'] or '',
                   'да' if entry['voided_at'] else 'нет']
    return csv_response(rows(), 'themarket-sales.csv')


@app.get('/api/analytics/export.xlsx')
def export_analytics_excel(actor: Perm('analytics'), period: Period = 'month', start: Day = '', end: Day = ''):
    report = analytics.report(period, start, end, full=True)
    filename = 'themarket-sales-' + report['start'] + '-' + report['end'] + '.xlsx'
    return attachment(analytics_xlsx(report), filename,
                      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.post('/api/analytics/entries')
def finance_entry(body: FinanceInput, actor: Perm('analytics')):
    return analytics.add_entry(body.reference, body.kind, body.category, usd_cents(body.amount), body.note,
                               body.occurred_date, body.order_id, actor)


@app.post('/api/analytics/entries/{reference}/void')
def void_finance_entry(reference: Annotated[str, PathParam(max_length=64)], body: ReasonInput, actor: Perm('analytics')):
    return analytics.void_entry(reference, body.reason, actor)


# ---------------------------------------------------------------- customers

@app.post('/api/customers/{uid}/wallet')
def adjust_wallet(uid: Id, body: Adjustment, actor: Perm('customers.balance')):
    store.adjust_wallet(uid, internal_amount(body.delta), body.reason, body.reference, actor)
    return {'ok': True, 'wallet': store.wallet(uid)}


@app.post('/api/customers/{uid}/balance')
def set_balance(uid: Id, body: BalanceTarget, actor: Perm('customers.balance')):
    store.set_wallet(uid, internal_amount(body.amount), body.reason, body.reference, actor)
    return {'ok': True, 'wallet': store.wallet(uid)}


@app.get('/api/customers/tags')
def customer_tag_list(actor: Perm('customers.view')):
    """CRM tags in use with customer counts (filter chips)."""
    return {'items': store.customer_tags()}


@app.get('/api/customers/marks')
def customer_marks(actor: Perm('customers.view'), ids: Annotated[str, Query(max_length=4200, pattern=r'^[0-9]{1,19}(,[0-9]{1,19}){0,199}$')]):
    """Tags and ban flags for the rows a customers page shows (the panel enhancer adds them)."""
    marks = store.customers_meta([int(uid) for uid in ids.split(',') if int(uid) <= MAX_ID])
    return {'items': {str(uid): value for uid, value in marks.items()}}


@app.get('/api/customers/{uid}')
def customer_info(uid: Id, request: Request, actor: Perm('customers.view')):
    """Wallet history is capped by the store; orders are the customer's latest 300. The wallet
    needs 'customers.balance'. 'meta' holds the CRM tags, internal note and ban state."""
    wallet = store.wallet(uid) if has(request, 'customers.balance') else None
    return {'user_id': uid, 'wallet': wallet, 'orders': store.orders(uid), 'meta': store.customer_meta(uid)}


# ---------------------------------------------------------------- orders

@app.get('/api/orders/{oid}', dependencies=[SameOrigin])
def order_info(oid: OrderId, request: Request, actor: Perm('orders.view')):
    """Delivered secrets (result) need 'stock.reveal', purchase costs need 'analytics'."""
    order = store.order_info(oid, actor)
    if not has(request, 'stock.reveal'):
        order['result_hidden'] = bool(order.get('result'))
        order['result'] = None
    if not has(request, 'analytics'):
        order.pop('cost_total', None)
    return order


@app.get('/api/orders/{oid}/details', dependencies=[SameOrigin])
def details(oid: OrderId, actor: Perm('orders.view')):
    return store.detail(oid, actor)


@app.put('/api/orders/{oid}/cost')
def order_cost(oid: OrderId, body: CostInput, actor: Perm('analytics')):
    return analytics.set_order_cost(oid, usd_cents(body.amount), body.reason, actor)


@app.post('/api/orders/{oid}/resend')
def resend(oid: OrderId, actor: Perm('orders.manage')):
    store.resend(oid, actor)
    return {'ok': True}


@app.post('/api/orders/{oid}/claim')
def claim(oid: OrderId, actor: Perm('orders.manage')):
    store.claim(oid, actor)
    return {'ok': True}


@app.post('/api/orders/{oid}/complete')
def complete(oid: OrderId, r: Result, actor: Perm('orders.manage')):
    store.complete(oid, r.result, actor)
    return {'ok': True}


# ---------------------------------------------------------------- products & stock

def product_data(p, costs=True):
    """costs: the caller may set purchase prices ('analytics'); otherwise cost_price is kept."""
    data = p.model_dump()
    for field, limit in (('name', 120), ('description', 1000), ('instruction', 800)):
        if len(emoji_text.plain(data[field])) > limit:
            raise emoji_text.FieldError(field, f'{FIELD_LABELS[field]}: не больше {limit} символов')
    data['price'] = internal_amount(p.price)
    data['badge'] = p.badge.strip()
    data['old_price'] = internal_amount(p.old_price) if p.old_price is not None else None
    if data['old_price'] is not None and data['old_price'] <= data['price']:
        raise ValueError('Старая цена должна быть больше текущей')
    if costs and 'cost_price' in p.model_fields_set:
        data['cost_price'] = usd_cents(p.cost_price) if p.cost_price is not None else None
    else:
        data.pop('cost_price', None)
    return data


@app.post('/api/products')
def create_product(p: Product, request: Request, actor: Perm('products.manage')):
    data = product_data(p, has(request, 'analytics'))
    data.setdefault('cost_price', None)
    return {'id': store.save_product(data, actor)}


@app.put('/api/products/{pid}')
def update_product(pid: Id, p: Product, request: Request, actor: Perm('products.manage')):
    return {'id': store.save_product(product_data(p, has(request, 'analytics')), actor, pid)}


@app.delete('/api/products/{pid}')
def delete_product(pid: Id, actor: Perm('products.manage')):
    return store.delete_product(pid, actor)


class MoveInput(BaseModel):
    direction: Literal['up', 'down']


@app.post('/api/products/{pid}/move')
def move_product(pid: Id, body: MoveInput, actor: Perm('products.manage')):
    """Swap the product with its neighbour inside its category."""
    return store.move_product(pid, body.direction, actor)


# Product photos: JPEG/PNG/WebP up to 5 MB in the raw request body; re-encoded to a clean JPEG.
IMAGE_TYPES = {'image/jpeg': 'JPEG', 'image/png': 'PNG', 'image/webp': 'WEBP'}
IMAGE_SIDE = 1280
IMAGE_PIXELS = 40_000_000


def sniff_image(data):
    if data[:3] == b'\xff\xd8\xff':
        return 'JPEG'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'PNG'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'WEBP'
    return None


def encode_image(data):
    """→ JPEG bytes ≤ IMAGE_SIDE px, EXIF and other metadata dropped, transparency flattened on white."""
    from PIL import Image, ImageOps
    kind = sniff_image(data)
    if not kind:
        raise ValueError('Загрузите фотографию в формате JPEG, PNG или WebP')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            image = Image.open(io.BytesIO(data))
            if image.format != kind or image.width * image.height > IMAGE_PIXELS:
                raise ValueError('Изображение слишком большое или повреждено')
            image.load()
            image = ImageOps.exif_transpose(image)
        except ValueError:
            raise
        except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning, EOFError):
            raise ValueError('Файл повреждён или не является изображением') from None
    if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
        image = image.convert('RGBA')
        canvas = Image.new('RGB', image.size, (255, 255, 255))
        canvas.paste(image, mask=image.getchannel('A'))
        image = canvas
    image = image.convert('RGB')
    image.thumbnail((IMAGE_SIDE, IMAGE_SIDE), Image.LANCZOS)
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=86, optimize=True, progressive=True)
    return output.getvalue(), image.size


def remove_image_file(relative):
    path = store.image_file(store.data_dir(), relative) if relative else None
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning('Could not remove an old product image', exc_info=True)


@app.post('/api/products/{pid}/image')
async def upload_image(pid: Id, request: Request, actor: Perm('products.manage')):
    if request.headers.get('content-type', '').split(';')[0].strip().lower() not in IMAGE_TYPES:
        raise ValueError('Загрузите фотографию в формате JPEG, PNG или WebP')
    data = await request.body()
    if not data:
        raise ValueError('Файл пуст')
    if len(data) > IMAGE_LIMIT:
        raise HTTPException(413, 'Фото должно быть не больше 5 МБ')
    product = store.product(pid, public=False)
    if not product:
        raise ValueError('Товар не найден')
    jpeg, (width, height) = await asyncio.to_thread(encode_image, data)
    relative = f'product-images/{pid}-{hashlib.sha256(jpeg).hexdigest()[:8]}.jpg'
    folder = images_dir()
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / (secrets.token_hex(8) + '.tmp')
    temporary.write_bytes(jpeg)
    os.replace(temporary, Path(store.path).parent / relative)
    old = product.get('image_path') or ''
    store.set_product_image(pid, relative, actor)
    if old != relative:
        remove_image_file(old)
    return {'image_path': relative, 'width': width, 'height': height, 'size': len(jpeg)}


@app.delete('/api/products/{pid}/image')
def delete_image(pid: Id, actor: Perm('products.manage')):
    product = store.product(pid, public=False)
    if not product:
        raise ValueError('Товар не найден')
    store.set_product_image(pid, '', actor)
    remove_image_file(product.get('image_path') or '')
    return {'ok': True}


@app.get('/api/products/{pid}/image')
def product_image(pid: Id, actor: Perm('products.manage', 'orders.view', 'broadcasts', 'promo.view')):
    product = store.product(pid, public=False)
    relative = (product or {}).get('image_path') or ''
    path = store.image_file(store.data_dir(), relative)
    if path is None:
        raise HTTPException(404, 'Фото не найдено')
    return FileResponse(path, media_type='image/jpeg')


@app.post('/api/products/{pid}/stock')
def stock(pid: Id, s: Stock, actor: Perm('stock.manage')):
    unit_cost = usd_cents(s.unit_cost) if s.unit_cost is not None else None
    return store.import_stock(pid, s.lines, actor, unit_cost=unit_cost)


@app.put('/api/products/{pid}/stock-cost')
def stock_cost(pid: Id, body: CostInput, actor: Perm('stock.manage')):
    return store.set_stock_cost(pid, usd_cents(body.amount), actor)


@app.get('/api/products/{pid}/stock')
def stock_items(pid: Id, actor: Perm('stock.manage'), status: Annotated[Literal['', 'free', 'reserved'], Query()] = '', page: Page = 1):
    return store.stock_items(pid, status, page)


@app.get('/api/products/{pid}/stock/{stock_id}', dependencies=[SameOrigin])
def reveal_stock(pid: Id, stock_id: Id, actor: Perm('stock.reveal')):
    return store.reveal_stock(pid, stock_id, actor)


# ---------------------------------------------------------------- promo codes

class PromoInput(BaseModel):
    """A promo code credits `amount` (dollars, or whole Stars before USD) to the wallet."""
    code: str = Field(pattern=r'^[A-Za-z0-9_-]{3,32}$')
    amount: Decimal = Field(gt=0, le=10000, decimal_places=2)
    max_uses: int = Field(default=0, ge=0, le=10**9)
    per_user: int = Field(default=1, ge=1, le=1000)
    starts_at: float | None = Field(default=None, ge=0, le=10**11)
    ends_at: float | None = Field(default=None, ge=0, le=10**11)
    active: bool = True
    note: str = Field(default='', max_length=200)


class ActiveInput(BaseModel):
    active: bool


def promo_data(body):
    amount = internal_amount(body.amount)
    if amount < 1:
        raise ValueError('Сумма начисления должна быть больше нуля')
    if body.starts_at and body.ends_at and body.ends_at <= body.starts_at:
        raise ValueError('Окончание действия должно быть позже начала')
    return {'code': body.code.upper(), 'amount_cents': amount, 'max_uses': body.max_uses, 'per_user': body.per_user,
            'starts_at': body.starts_at, 'ends_at': body.ends_at, 'active': body.active, 'note': body.note.strip()}


@app.get('/api/promos')
def promo_list(actor: Perm('promo.view', 'promo.manage'), query: Text = '',
               status: Annotated[Literal['', 'active'], Query()] = '', page: Page = 1, page_size: PageSize = 25):
    """Server-side page of codes (search by code), each with uses / credited amount / unique customers."""
    return promos.list(include_inactive=status != 'active', query=query, page=page, page_size=page_size)


@app.get('/api/promos/{code_id}/redemptions')
def promo_redemptions(code_id: Id, actor: Perm('promo.view', 'promo.manage'), page: Page = 1):
    """Who used a code and when (newest first, 20 per page)."""
    return promos.redemptions(code_id, page=page, page_size=20)


@app.post('/api/promos')
def promo_create(body: PromoInput, actor: Perm('promo.manage')):
    return promos.create(promo_data(body), actor)


@app.put('/api/promos/{code_id}')
def promo_update(code_id: Id, body: PromoInput, actor: Perm('promo.manage')):
    return promos.update(code_id, promo_data(body), actor)


@app.post('/api/promos/{code_id}/active')
def promo_active(code_id: Id, body: ActiveInput, actor: Perm('promo.manage')):
    promos.set_active(code_id, body.active, actor)
    return {'ok': True}


# ---------------------------------------------------------------- support reply templates
# Declared before /api/support/{ticket_id}, which would otherwise capture '/templates'.

class TemplateInput(BaseModel):
    title: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=3500)


@app.get('/api/support/templates')
def support_templates(actor: Perm('support')):
    return {'items': store.support_templates()}


@app.post('/api/support/templates')
def support_template_create(body: TemplateInput, actor: Perm('support')):
    return store.save_support_template(body.title, body.text, actor)


@app.put('/api/support/templates/{template_id}')
def support_template_update(template_id: Id, body: TemplateInput, actor: Perm('support')):
    return store.save_support_template(body.title, body.text, actor, template_id)


@app.delete('/api/support/templates/{template_id}')
def support_template_delete(template_id: Id, actor: Perm('support')):
    return store.delete_support_template(template_id, actor)


# ---------------------------------------------------------------- support inbox

class ReplyInput(BaseModel):
    text: str = Field(min_length=1, max_length=3500)


@app.get('/api/support')
def support_tickets(actor: Perm('support'), status: Annotated[Literal['open', 'closed', 'all'], Query()] = 'open',
                    query: Text = '', page: Page = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 30):
    result = support.tickets(status=status, query=query, page=page, page_size=page_size)
    result['unread'] = support_unread()
    return result


@app.get('/api/support/{ticket_id}')
def support_thread(ticket_id: Id, actor: Perm('support'), before: Annotated[int, Query(ge=0, le=MAX_ID)] = 0,
                   limit: Annotated[int, Query(ge=1, le=100)] = 50):
    """Newest `limit` messages (oldest first); `before=<message id>` pages back. Opening marks it read."""
    inbox = support
    thread = inbox.thread(ticket_id, before=before or None, limit=limit)
    if not before and thread['ticket'].get('unread_for_staff'):
        inbox.mark_read(ticket_id, actor)
        thread['ticket']['unread_for_staff'] = 0
    return thread


@app.post('/api/support/{ticket_id}/reply')
def support_reply(ticket_id: Id, body: ReplyInput, actor: Perm('support')):
    if not body.text.strip():
        raise ValueError('Введите текст ответа')
    return support.reply(ticket_id, body.text.strip(), actor)


@app.post('/api/support/{ticket_id}/{action}')
def support_action(ticket_id: Id, action: Literal['close', 'reopen', 'assign', 'unassign'], actor: Perm('support')):
    inbox = support
    if action == 'unassign':
        return inbox.assign(ticket_id, actor, '')
    return getattr(inbox, action)(ticket_id, actor)


# ---------------------------------------------------------------- staff notifications

ChatId = Annotated[int, Field(gt=-10**16, lt=10**16)]


class NotifyInput(BaseModel):
    admin_chat_ids: list[ChatId] = Field(default_factory=list, max_length=20)
    new_paid_order: bool = True
    manual_order_details: bool = True
    support_message: bool = True
    low_stock: bool = True
    low_stock_threshold: int = Field(default=3, ge=0, le=10000)
    low_stock_overrides: dict[Annotated[str, Field(pattern=r'^[0-9]{1,19}$')], Annotated[int, Field(ge=0, le=10000)]] = Field(default_factory=dict, max_length=1000)
    payment_issue: bool = True
    errors: bool = True
    daily_summary: bool = True
    daily_summary_time: str = Field(default='09:00', pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')


@app.get('/api/notify')
def notify_settings(actor: Perm('settings')):
    return notify.settings()


@app.put('/api/notify')
def save_notify(body: NotifyInput, actor: Perm('settings')):
    data = body.model_dump()
    if 0 in data['admin_chat_ids']:
        raise ValueError('ID чата не может быть нулём')
    return notify.save(data, actor)


@app.post('/api/notify/test')
def notify_test(actor: Perm('settings')):
    """Queues one test message per admin chat; the running bot delivers it."""
    return {'queued': notify.send_test(actor)}


# ---------------------------------------------------------------- staff accounts

class StaffCreate(BaseModel):
    username: str = Field(pattern=r'^[A-Za-z0-9_.-]{3,32}$')
    password: str = Field(min_length=10, max_length=256)
    role: Literal['owner', 'manager', 'support']


class StaffUpdate(BaseModel):
    role: Literal['owner', 'manager', 'support'] | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=256)


STAFF_FIELDS = ('id', 'username', 'role', 'active', 'env_owner', 'created', 'updated', 'last_login', 'sessions')


def public_member(member):
    return {k: member.get(k) for k in STAFF_FIELDS if k in member}


@app.get('/api/staff')
def staff_list(request: Request, actor: Perm('staff'), page: Page = 1, page_size: PageSize = 50):
    result = paginate([public_member(m) for m in staff.list()], page, page_size)
    result['roles'] = ROLE_LABELS
    result['me'] = current_identity(request)['id']
    result['permissions'] = {role: sorted(role_permissions(role)) for role in ROLE_LABELS}
    return result


@app.post('/api/staff')
def staff_create(body: StaffCreate, actor: Perm('staff')):
    member = staff.create(body.username, body.password, body.role, actor)
    return public_member(member) if isinstance(member, dict) else {'id': member}


@app.put('/api/staff/{sid}')
def staff_update(sid: Id, body: StaffUpdate, request: Request, actor: Perm('staff')):
    accounts = staff
    if sid == current_identity(request)['id'] and ((body.role and body.role != 'owner') or body.active is False):
        raise ValueError('Нельзя понизить или отключить собственную учётную запись')
    # staff.update ends the member's sessions itself when the role, access or password changes.
    return public_member(accounts.update(sid, role=body.role, active=body.active, password=body.password, actor=actor))


@app.post('/api/staff/{sid}/revoke')
def staff_revoke(sid: Id, actor: Perm('staff')):
    staff.revoke_all(sid)
    audit(actor, 'staff.revoke', str(sid))
    return {'ok': True}


# ================================================================ 3.1 features

# ---------------------------------------------------------------- live activity (Server-Sent Events)

LIVE_POLL_SECONDS = 2.0          # one shared database poll per process, at most this often
LIVE_HEARTBEAT_SECONDS = 15.0    # comment line that keeps proxies and the browser connection alive
LIVE_STREAM_SECONDS = 300.0      # then the browser reconnects (and is authenticated again)
LIVE_MAX_STREAMS = 4             # per staff member (tabs); the oldest stream is closed first
LIVE_QUEUE = 200                 # a subscriber this far behind is dropped; its browser reconnects
LIVE_REPLAY = 50                 # missed events sent after a reconnect (Last-Event-ID)
# Feed kind (live_events.kind) → public kind, permissions that may see it (any of), view to open.
LIVE_KINDS = {
    'payment.accepted': ('order', ('orders.view',), 'orders'),
    'delivery.review': ('order_review', ('orders.view',), 'tasks'),
    'payment.issue': ('payment_issue', ('payments',), 'errors'),
    'crypto.review': ('payment_issue', ('payments',), 'payments'),
    'support.message': ('support', ('support',), 'support'),
    'stock.low': ('low_stock', ('products.manage', 'stock.manage'), 'products'),
    'customer.new': ('customer', ('customers.view',), 'customers'),
    'broadcast.completed': ('broadcast', ('broadcasts',), 'broadcasts'),
}


def live_kinds(identity):
    return sorted(kind for kind, (_, perms, _) in LIVE_KINDS.items() if identity['permissions'] & set(perms))


def live_items(rows):
    """Feed rows → compact public events {id, kind, title, text, at, link, tone, order_id?, user_id?}."""
    names = store.customer_names([row['user_id'] for row in rows if row.get('user_id')])
    items = []
    for row in rows:
        spec = LIVE_KINDS.get(row['kind'])
        if not spec:
            continue
        try:
            data = json.loads(row.get('data') or '{}')
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        who = names.get(row.get('user_id')) or (f"TG {row['user_id']}" if row.get('user_id') else '')
        quantity = data.get('quantity') or 1
        kind = row['kind']
        if kind == 'payment.accepted':
            title, tone = 'Новый оплаченный заказ', 'green'
            text = f"{data.get('title') or 'Заказ'} × {quantity}" + (f" · {store.money(data['price'])}" if isinstance(data.get('price'), int) else '')
        elif kind == 'delivery.review':
            title, tone, text = 'Заказ требует проверки', 'red', f"{data.get('title') or 'Заказ'} · оплата получена, товар не выдан"
        elif kind == 'payment.issue':
            title, tone, text = 'Проблема с платежом', 'red', 'Платёж получен, но не проведён' + (f' · {who}' if who else '')
        elif kind == 'crypto.review':
            title, tone, text = 'Платёж USDT требует проверки', 'orange', (data.get('method') or 'USDT') + (f' · {who}' if who else '')
        elif kind == 'support.message':
            title, tone, text = 'Новое сообщение в поддержку', 'blue', (who or 'Покупатель') + (' · фото' if data.get('kind') == 'photo' else '')
        elif kind == 'stock.low':
            left = data.get('quantity') or 0
            title = 'Товар закончился' if not left else 'Заканчивается товар'
            tone, text = 'orange', f"{data.get('title') or 'Товар'} · осталось {left} шт."
        elif kind == 'customer.new':
            name = (data.get('name') or '').strip()
            username = data.get('username') or ''
            title, tone, text = 'Новый покупатель', 'blue', ' · '.join(filter(None, [name, '@' + username if username else '', f"TG {row['user_id']}"]))
        else:
            title, tone, text = 'Рассылка завершена', 'green', 'Итоги доставки — в разделе «Рассылки»'
        item = {'id': row['id'], 'kind': spec[0], 'title': title, 'text': text[:300], 'at': row['at'], 'link': '#' + spec[2], 'tone': tone}
        if row.get('order_id'):
            item['order_id'] = row['order_id']
        if row.get('user_id'):
            item['user_id'] = row['user_id']
        items.append(item)
    return items


class LiveSubscriber:
    __slots__ = ('username', 'kinds', 'queue', 'closed')

    def __init__(self, username, kinds):
        self.username, self.kinds, self.closed = username, frozenset(kinds), False
        self.queue = asyncio.Queue(maxsize=LIVE_QUEUE)


class LiveHub:
    """One poller task per process (and event loop) while anyone listens: reads new feed rows at
    most every LIVE_POLL_SECONDS and fans them out to the subscribers allowed to see them."""

    def __init__(self):
        self.subscribers = []
        self.task = None
        self.loop = None
        self.last_id = None

    def subscribe(self, username, kinds):
        loop = asyncio.get_running_loop()
        if self.loop is not loop:  # a new event loop (tests, reload): forget the old one's state
            self.subscribers, self.task, self.loop, self.last_id = [], None, loop, None
        mine = [s for s in self.subscribers if s.username.casefold() == username.casefold()]
        while len(mine) >= LIVE_MAX_STREAMS:
            self.close(mine.pop(0))
        subscriber = LiveSubscriber(username, kinds)
        self.subscribers.append(subscriber)
        if self.task is None or self.task.done():
            self.task = loop.create_task(self.run())
        return subscriber

    def close(self, subscriber):
        """Detach a subscriber; a None in its queue ends its stream."""
        if subscriber in self.subscribers:
            self.subscribers.remove(subscriber)
        if not subscriber.closed:
            subscriber.closed = True
            try:
                subscriber.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def unsubscribe(self, subscriber):
        subscriber.closed = True
        if subscriber in self.subscribers:
            self.subscribers.remove(subscriber)

    def publish(self, items):
        for subscriber in list(self.subscribers):
            for item in items:
                if item['_kind'] not in subscriber.kinds:
                    continue
                try:
                    subscriber.queue.put_nowait(item)
                except asyncio.QueueFull:
                    self.close(subscriber)  # far behind: drop it, the browser reconnects and replays
                    break

    async def run(self):
        try:
            if self.last_id is None:
                self.last_id = await asyncio.to_thread(store.live_max_id)
            while self.subscribers:
                await asyncio.sleep(LIVE_POLL_SECONDS)
                if not self.subscribers:
                    break
                try:
                    rows = await asyncio.to_thread(store.live_since, self.last_id, 200)
                    if rows:
                        self.last_id = rows[-1]['id']
                        items = await asyncio.to_thread(live_items, rows)
                        kinds = {row['id']: row['kind'] for row in rows}
                        self.publish([{**item, '_kind': kinds[item['id']]} for item in items])
                except Exception:
                    log.warning('Live feed poll failed', exc_info=True)
        finally:
            if self.task is asyncio.current_task():
                self.task, self.last_id = None, None

    async def stop(self):
        task, self.task = self.task, None
        for subscriber in list(self.subscribers):
            self.close(subscriber)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


live_hub = LiveHub()


def sse(item, event='live'):
    public = {k: v for k, v in item.items() if not k.startswith('_')}
    return f"id: {public['id']}\nevent: {event}\ndata: {json.dumps(public, ensure_ascii=False)}\n\n" if event == 'live' \
        else f"event: {event}\ndata: {json.dumps(public, ensure_ascii=False)}\n\n"


def live_replay(kinds, after_id, limit=LIVE_REPLAY):
    return live_items(list(reversed(store.live_recent(kinds, limit, after_id=after_id))))


def live_state(username, kinds):
    seen = store.live_seen(username)
    return {'id': 0, 'seen': seen, 'unread': store.live_unread(kinds, seen), 'last': store.live_max_id()}


@app.get('/api/live')
async def live(request: Request, actor: SignedIn):
    """Server-Sent Events: the staff member's permitted feed events as they happen, a heartbeat
    comment every 15 s and, after a reconnect (Last-Event-ID), the events missed meanwhile."""
    identity = current_identity(request)
    kinds = live_kinds(identity)
    try:
        last_event = int(request.headers.get('last-event-id') or 0)
    except ValueError:
        last_event = 0
    last_event = max(0, min(last_event, MAX_ID))

    async def stream():
        subscriber = live_hub.subscribe(identity['username'], kinds)
        began, sent = time.monotonic(), last_event
        try:
            yield 'retry: 3000\n\n'
            state = await asyncio.to_thread(live_state, identity['username'], kinds)
            yield sse(state, 'hello')
            if last_event and kinds:
                for item in await asyncio.to_thread(live_replay, kinds, last_event):
                    sent = max(sent, item['id'])
                    yield sse(item)
            while time.monotonic() - began < LIVE_STREAM_SECONDS:
                timeout = min(LIVE_HEARTBEAT_SECONDS, max(0.01, LIVE_STREAM_SECONDS - (time.monotonic() - began)))
                try:
                    item = await asyncio.wait_for(subscriber.queue.get(), timeout)
                except asyncio.TimeoutError:
                    yield ': ping\n\n'
                    continue
                if item is None:
                    break
                if item['id'] > sent:
                    sent = item['id']
                    yield sse(item)
        finally:
            live_hub.unsubscribe(subscriber)

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-store'})


LiveLimit = Annotated[int, Query(ge=1, le=100)]


@app.get('/api/live/recent')
def live_recent(request: Request, actor: SignedIn, limit: LiveLimit = 30):
    """Bell list (newest first) with the unread count; also the polling fallback of the panel."""
    identity = current_identity(request)
    kinds = live_kinds(identity)
    state = live_state(identity['username'], kinds)
    return {'items': live_items(store.live_recent(kinds, limit)), 'seen': state['seen'], 'unread': state['unread'],
            'last': state['last']}


class SeenInput(BaseModel):
    id: int = Field(ge=0, le=MAX_ID)


@app.post('/api/live/seen')
def live_seen(body: SeenInput, request: Request, actor: SignedIn):
    identity = current_identity(request)
    seen = store.set_live_seen(identity['username'], body.id)
    return {'seen': seen, 'unread': store.live_unread(live_kinds(identity), seen)}


# ---------------------------------------------------------------- customers CRM

class TagsInput(BaseModel):
    tags: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=30)


class NoteInput(BaseModel):
    note: str = Field(default='', max_length=2000)


class BanInput(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


def customer_meta_public(meta):
    return {k: meta.get(k) for k in ('user_id', 'tags', 'note', 'banned', 'ban_reason', 'banned_at', 'banned_by', 'updated')}


@app.get('/api/customers/{uid}/meta')
def customer_meta(uid: Id, actor: Perm('customers.view')):
    return customer_meta_public(store.customer_meta(uid))


@app.put('/api/customers/{uid}/tags')
def customer_tags_save(uid: Id, body: TagsInput, actor: Perm('orders.manage', 'support')):
    return customer_meta_public(store.set_customer_tags(uid, body.tags, actor))


@app.put('/api/customers/{uid}/note')
def customer_note_save(uid: Id, body: NoteInput, actor: Perm('orders.manage', 'support')):
    return customer_meta_public(store.set_customer_note(uid, body.note, actor))


@app.post('/api/customers/{uid}/ban')
def customer_ban(uid: Id, body: BanInput, actor: Perm('customers.balance')):
    return customer_meta_public(store.ban_customer(uid, body.reason, actor))


@app.post('/api/customers/{uid}/unban')
def customer_unban(uid: Id, actor: Perm('customers.balance')):
    return customer_meta_public(store.unban_customer(uid, actor))


# ---------------------------------------------------------------- order notes

class OrderNoteInput(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@app.get('/api/orders/{oid}/notes')
def order_notes(oid: OrderId, actor: Perm('orders.view')):
    return {'items': store.order_notes(oid)}


@app.post('/api/orders/{oid}/notes')
def order_note_add(oid: OrderId, body: OrderNoteInput, actor: Perm('orders.manage')):
    return store.add_order_note(oid, body.text, actor)


# ---------------------------------------------------------------- product bulk actions

class BulkInput(BaseModel):
    ids: list[Annotated[int, Field(gt=0, le=MAX_ID)]] = Field(min_length=1, max_length=500)
    action: Literal['show', 'hide', 'delete', 'category', 'price_percent']
    value: Decimal | str | None = None


@app.post('/api/products/bulk')
def products_bulk(body: BulkInput, actor: Perm('products.manage')):
    value = body.value
    if body.action == 'price_percent':
        try:
            value = Decimal(str(value))
        except Exception:
            raise ValueError('Изменение цены: от −90 % до +500 %') from None
        if not value.is_finite() or value != value.quantize(Decimal('0.01')):
            raise ValueError('Изменение цены: число с точностью до сотых')
    elif body.action == 'category':
        value = value if isinstance(value, str) else ''
    return store.bulk_products(body.ids, body.action, value, actor)


# ---------------------------------------------------------------- insights

class GoalInput(BaseModel):
    amount: Decimal = Field(ge=0, le=10000000000, decimal_places=2)


@app.get('/api/insights')
def insights_report(actor: Perm('analytics'), period: Period = 'month', start: Day = '', end: Day = ''):
    """Heatmap, funnel, top customers, monthly goal and cohorts (see insights.py)."""
    return insights.report(period, start, end)


@app.put('/api/insights/goal')
def insights_goal(body: GoalInput, actor: Perm('analytics')):
    goal = store.set_revenue_goal(internal_amount(body.amount), actor)
    insights.forget()
    return {'goal': goal}
