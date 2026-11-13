"""One polling worker per shop.

Updates are processed concurrently (one task per update, ordered per user) and the
polling offset only advances after the whole batch was handled. A failing update is
logged and skipped instead of stalling the bot. Payments are persisted synchronously
before an update is acknowledged; deliveries, alerts and crypto checks run in their
own resilient background loops.

Every outbound call goes through ratelimit.RateLimitedBot (global and per-chat limits,
interactive replies first). Database writes run in a small thread pool so SQLite commits
and WAL checkpoints never stall the event loop; hot reads are cached for a few seconds.
"""
import asyncio
import concurrent.futures
import contextvars
import functools
import inspect
import json
import logging
import os
import re
import signal
import time
from datetime import datetime
from html import escape, unescape
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramConflictError,
                                TelegramForbiddenError, TelegramRetryAfter, TelegramUnauthorizedError)
from aiogram.types import BufferedInputFile, FSInputFile, InputMediaPhoto, MessageEntity

import ratelimit
from bep20 import BscVerifier, PendingPayment
from broadcasts import Broadcasts, worker as broadcast_worker
from channel_gate import ChannelGate
from crypto_networks import NETWORKS, NetworkVerifier, normalize_hash, amount as crypto_amount, qr as crypto_qr
import emoji_text
from emoji_library import WELCOME_EMOJIS
from i18n import tr, language, error_text, order_status
from manager import InstanceLock
from money import usd_cents, cents_to_stars
import notify
import promo
from store import Store
import support

from bep20 import PaymentMismatch

log = logging.getLogger('themarket')
store = Store()
BEP20_EMOJI_ID = '5463427579966929312'
NETWORK_EMOJI_IDS = {'bsc': BEP20_EMOJI_ID, 'aptos': '5384362450432827393', 'tron': '5465394026448428029',
                     'usdt': '5242551409232069476'}
BANNER_PATH = Path(__file__).parent/'assets'/'welcome.png'
MENU_BANNER_DIR = BANNER_PATH.parent/'menu'
_banner_file_ids = {}

POLL_TIMEOUT = 25
UPDATE_TIMEOUT = 120
MAX_CONCURRENCY = 64
CATALOG_PAGE = 8
CATEGORY_PAGE = 10
TXID_ATTEMPTS = 5
PAID_STATES = ('fulfilled', 'awaiting_details', 'queued', 'processing', 'in_progress')
ACTIVE_STATES = ('awaiting_details', 'queued', 'processing', 'in_progress', 'needs_review')
HASH_SHAPE = re.compile(r'(?:0x)?[0-9a-fA-F]{64}|[A-Za-z0-9_+/=-]{43,44}')
ORDER_ID = r'[0-9a-f]{32}'

# Background verification tasks; kept referenced so they are not garbage-collected.
background_tasks = set()

# ---------------------------------------------------------------- database offloading and caches

# SQLite commits (and the WAL checkpoints they trigger) can block for hundreds of milliseconds
# on a busy disk. Writes run here; Store keeps one connection per thread, so this is safe.
_db_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='themarket-db')


async def offload(function, *args, **kwargs):
    """Run a blocking store call in the database pool, keeping the language context."""
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    return await loop.run_in_executor(_db_pool, functools.partial(context.run, function, *args, **kwargs))


CATALOG_CACHE_SECONDS = 2.0
GATE_CACHE_SECONDS = 60.0
REMEMBER_SECONDS = 600.0
_catalog_cache = {}
_gate_passed = {}
_remembered = {}
_qr_cache = {}


def public_products():
    """Public products (with availability) cached for a moment: the catalog is read on most updates."""
    now = time.monotonic()
    hit = _catalog_cache.get(store.path)
    if hit and hit[0] > now:
        return hit[1]
    items = [displayed(item) for item in store.products(public=True)]
    _catalog_cache.clear()
    _catalog_cache[store.path] = (now+CATALOG_CACHE_SECONDS, items)
    return items


def displayed(product):
    """Product with plain-text name/description (tokens → fallback emoji); raw texts kept for HTML cards."""
    if not product or 'raw_name' in product:
        return product
    product = dict(product)
    for field in ('name', 'description', 'instruction'):
        if field in product:
            product['raw_'+field] = product[field] or ''
            product[field] = emoji_text.plain(product[field])
    return product


def forget_catalog():
    _catalog_cache.clear()


def _bounded(cache, limit=100000):
    if len(cache) > limit:
        cache.clear()


async def remember_visitor(visitor):
    """Record a private visitor; skip the write when nothing changed recently."""
    key = (store.path, visitor.id)
    data = (visitor.username or '', visitor.first_name or '', visitor.last_name or '')
    seen = _remembered.get(key)
    if seen and seen[0] == data and seen[1] > time.monotonic():
        return
    await offload(store.remember_customer, visitor.id, *data)
    _bounded(_remembered)
    _remembered[key] = (data, time.monotonic()+REMEMBER_SECONDS)


async def set_mode(user_id, mode):
    """Change the input mode; the read is cheap, the write goes to the database pool."""
    if store.input_mode(user_id) != mode:
        await offload(store.input_mode, user_id, mode)


def gate_required(user_id):
    """ChannelGate.required() with a cache of users who need no check (completed or no gate)."""
    key = (store.path, user_id)
    if _gate_passed.get(key, 0) > time.monotonic():
        return None
    config = ChannelGate(store).required(user_id)
    if config is None:
        _bounded(_gate_passed)
        _gate_passed[key] = time.monotonic()+GATE_CACHE_SECONDS
    else:
        _gate_passed.pop(key, None)
    return config


async def qr_png(text):
    """QR codes are CPU work: render off the loop, and only once per wallet address."""
    png = _qr_cache.get(text)
    if png is None:
        png = await asyncio.to_thread(crypto_qr, text)
        if len(_qr_cache) > 64:
            _qr_cache.clear()
        _qr_cache[text] = png
    return png


def shop_name():
    return os.getenv('SHOP_NAME', '').strip() or 'TheMarket'


def is_admin(user_id):
    """ADMIN_CHAT_ID (one or several ids, as notify.env_chat_ids parses it) or a chat id from
    the panel's notification settings (admin_chat_ids)."""
    if type(user_id) is not int:
        return False
    if user_id in notify.env_chat_ids():
        return True
    try:
        return user_id in (notify.Notify(store).settings().get('admin_chat_ids') or [])
    except Exception:  # settings unreadable (module tables missing): env ids only
        log.debug('Notify settings unavailable for the admin check', exc_info=True)
        return False


def is_gone(exc):
    """The chat can never receive messages: blocked bot, deleted or unknown chat."""
    if isinstance(exc, TelegramForbiddenError):
        return True
    if isinstance(exc, TelegramBadRequest):
        reason = str(getattr(exc, 'message', exc)).lower()
        return any(item in reason for item in ('chat not found', 'user is deactivated', 'peer_id_invalid'))
    return False


def report(function, *args, permanent=False):
    """Record a delivery/notification result; permanent=True gives up immediately."""
    return function(*args, permanent=permanent)


def clear_crypto_context(user_id):
    store.clear_crypto_context(user_id)


def spawn(coroutine):
    task = asyncio.create_task(coroutine)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


async def wait_background():
    """Wait for background verification tasks (used by tests and shutdown)."""
    while background_tasks:
        await asyncio.gather(*list(background_tasks), return_exceptions=True)


def fits_caption(text, parse_mode=None):
    visible = unescape(re.sub(r'<[^>]*>', '', text)) if parse_mode == 'HTML' else text
    return len(visible.encode('utf-16-le'))//2 <= 1024


def utf16_len(text):
    return len(text.encode('utf-16-le'))//2


def fmt_date(timestamp, with_time=False):
    return datetime.fromtimestamp(timestamp).strftime('%d.%m.%Y %H:%M' if with_time else '%d.%m.%Y')


async def brand_screen(bot, chat, text, *, section='home', **kwargs):
    path = BANNER_PATH if section == 'home' else MENU_BANNER_DIR/(section+'.png')
    if not path.is_file() or not fits_caption(text, kwargs.get('parse_mode')):
        return await bot.send_message(chat, text, **kwargs)
    if 'entities' in kwargs:
        kwargs['caption_entities'] = kwargs.pop('entities')
    cached_id = _banner_file_ids.get(path)
    try:
        result = await bot.send_photo(chat, cached_id or FSInputFile(path), caption=text, **kwargs)
    except TelegramBadRequest as exc:
        if not cached_id or not any(reason in exc.message.lower() for reason in
                                    ('wrong file', 'file identifier', 'file_id', 'file reference')):
            raise
        _banner_file_ids.pop(path, None)
        result = await bot.send_photo(chat, FSInputFile(path), caption=text, **kwargs)
    if getattr(result, 'photo', None):
        _banner_file_ids[path] = result.photo[-1].file_id
    return result


class Navigation:
    """Edit the callback's text or photo in place; keep QR and controls together."""
    def __init__(self, bot, message):
        self.transport = bot
        self.message = message

    def __getattr__(self, name):
        return getattr(self.transport, name)

    async def send_message(self, chat_id, text, **kwargs):
        kwargs.setdefault('parse_mode', None)
        if getattr(self.message, 'photo', None):
            return await self._fresh_text(chat_id, text, kwargs)
        try:
            return await self.transport.edit_message_text(
                text=text, chat_id=chat_id, message_id=self.message.message_id, **kwargs)
        except TelegramBadRequest as exc:
            reason = exc.message.lower()
            if 'message is not modified' in reason:
                return None
            if 'there is no text in the message to edit' in reason:
                return await self._fresh_text(chat_id, text, kwargs)
            if any(item in reason for item in ('message to edit not found', "message can't be edited",
                                               'message cannot be edited')):
                # Deleted, old, or non-text messages need a fresh navigation screen.
                return await self.transport.send_message(chat_id, text, **kwargs)
            raise

    async def _fresh_text(self, chat_id, text, kwargs):
        # Telegram cannot remove a photo by editing it into a text message.
        result = await self.transport.send_message(chat_id, text, **kwargs)
        try:
            await self.transport.delete_message(chat_id, self.message.message_id)
        except Exception as exc:
            log.debug('Navigation cleanup: %s', type(exc).__name__)
        return result

    async def send_photo(self, chat_id, photo, *, caption='', parse_mode=None, caption_entities=None, reply_markup=None):
        if not fits_caption(caption, parse_mode):
            kwargs = {'parse_mode': parse_mode, 'reply_markup': reply_markup}
            if caption_entities:
                kwargs['entities'] = caption_entities
            return await self._fresh_text(chat_id, caption, kwargs)
        media = InputMediaPhoto(media=photo, caption=caption, parse_mode=parse_mode, caption_entities=caption_entities)
        try:
            return await self.transport.edit_message_media(chat_id=chat_id, message_id=self.message.message_id,
                                                           media=media, reply_markup=reply_markup)
        except TelegramBadRequest as exc:
            reason = exc.message.lower()
            if 'message is not modified' in reason:
                return None
            if 'caption is too long' in reason:
                kwargs = {'parse_mode': parse_mode, 'reply_markup': reply_markup}
                if caption_entities:
                    kwargs['entities'] = caption_entities
                return await self._fresh_text(chat_id, caption, kwargs)
            if 'there is no media in the message' in reason or 'no media' in reason:
                # A text screen cannot become a photo: send the photo screen, then drop the text.
                result = await self.transport.send_photo(chat_id, photo, caption=caption, parse_mode=parse_mode,
                                                         caption_entities=caption_entities, reply_markup=reply_markup)
                try:
                    await self.transport.delete_message(chat_id, self.message.message_id)
                except Exception as cleanup:
                    log.debug('Navigation cleanup: %s', type(cleanup).__name__)
                return result
            if not any(item in reason for item in ('message to edit not found', "message can't be edited",
                                                   'message cannot be edited')):
                raise
            return await self.transport.send_photo(chat_id, photo, caption=caption, parse_mode=parse_mode,
                                                   caption_entities=caption_entities, reply_markup=reply_markup)


def transport(bot):
    return getattr(bot, 'transport', bot)


def keyboard(rows, home=True):
    buttons = []
    assignments = store.emoji_assignments()
    for row in rows:
        rendered = []
        for item in row:
            if isinstance(item, dict):
                rendered.append(item)
                continue
            label, data, *style = item
            button = {'text': label, 'callback_data': data}
            icon = assignments.get('menu:'+data)
            if icon:
                button['icon_custom_emoji_id'] = icon
                if ' ' in label and not label[0].isalnum():
                    button['text'] = label.split(' ', 1)[1]
            if data.startswith(('net:', 'netbuy:')):
                network = data.split(':')[1]
                if network in NETWORK_EMOJI_IDS:
                    button['text'] = label.removeprefix('💵 ')
                    button['icon_custom_emoji_id'] = NETWORK_EMOJI_IDS[network]
            if style:
                button['style'] = style[0]
            rendered.append(button)
        buttons.append(rendered)
    if home:
        buttons.append([{'text': tr('🏠 Главное меню'), 'callback_data': 'home', 'style': 'primary'}])
    return {'inline_keyboard': buttons}


def network_icon(network):
    return '<tg-emoji emoji-id="'+NETWORK_EMOJI_IDS.get(network, BEP20_EMOJI_ID)+'">💵</tg-emoji>'


async def send_bep20(bot, chat, text, network='bsc', **kwargs):
    icon_id = NETWORK_EMOJI_IDS.get(network, BEP20_EMOJI_ID)
    if kwargs.get('parse_mode') == 'HTML':
        text = text.replace('💵', network_icon(network))
    else:
        kwargs['entities'] = [MessageEntity(type='custom_emoji', offset=utf16_len(text[:match.start()]),
                                            length=2, custom_emoji_id=icon_id) for match in re.finditer('💵', text)]
        kwargs['parse_mode'] = None
    await brand_screen(bot, chat, text, section='deposit', **kwargs)


# ---------------------------------------------------------------- callback parsing

class StaleButton(ValueError):
    """Callback data that does not match any current button layout."""
    def __init__(self, data=None):
        super().__init__(tr('Кнопка устарела, открой меню заново'))


NETWORK = '(' + '|'.join(NETWORKS) + ')'
SELECTION = r'([0-9]{1,9})(?::([0-9]{1,3}))?'
ROUTES = [(name, re.compile(pattern)) for name, pattern in [
    ('noop', 'noop'), ('home', 'home'), ('wallet', 'wallet'), ('deposit', 'deposit'), ('stars', 'stars'),
    ('profile', 'profile'), ('guide', 'guide'), ('terms', 'terms'), ('support', 'support'), ('about', 'about'),
    ('language', 'language'), ('news', 'news(?::(on|off))?'), ('lang', 'lang:(ru|en)'),
    ('channel', 'channel:check'),
    ('catalog', r'catalog(?::([0-9]{1,3}))?'),
    ('cat', r'cat:([0-9]{1,4})(?::([0-9]{1,3}))?'),
    ('pback', r'pback:([0-9]{1,9})'),
    ('product', 'product:'+SELECTION), ('qtyinput', 'qtyinput:'+SELECTION),
    ('checkout', 'checkout:'+SELECTION), ('wbuy', 'wbuy:'+SELECTION), ('buy', 'buy:'+SELECTION),
    ('netbuy', 'netbuy:'+NETWORK+':'+SELECTION),
    ('cbuy', 'cbuy:'+SELECTION),                       # legacy alias of netbuy:bsc
    ('wpay', 'wpay:('+ORDER_ID+')'),
    ('net', 'net:'+NETWORK), ('bsc', 'bsc'),            # legacy alias of net:bsc
    ('netamount', 'netamount:'+NETWORK+':([0-9]{1,8})'),
    ('bscamount', 'bsc:([0-9]{1,8})'),                  # legacy alias of netamount:bsc
    ('custom', 'custom:(stars|'+'|'.join(NETWORKS)+')'),
    ('usdtopup', 'usdtopup:([0-9]{1,8})'), ('topup', 'topup:([0-9]{1,5})'),
    ('bscpaid', 'bscpaid:('+ORDER_ID+')'),
    ('orders', r'orders(?::([0-9]{1,4}))?'),
    ('purchase', 'purchase:('+ORDER_ID+r')(?::([0-9]{1,4}))?'),
    ('order', 'order:('+ORDER_ID+')'),
    ('get', 'get:('+ORDER_ID+')'),
    ('details', 'details:('+ORDER_ID+')'),
    ('again', 'again:('+ORDER_ID+')'),
    ('write', 'write'), ('promo', 'promo'),
]]


def parse_callback(data):
    """Return (action, args) for known callback data; StaleButton otherwise."""
    if not data or len(data.encode()) > 64:
        raise StaleButton(data)
    for name, pattern in ROUTES:
        match = pattern.fullmatch(data)
        if match:
            args = match.groups()
            if name == 'cbuy':
                name, args = 'netbuy', ('bsc', *args)
            elif name == 'bsc':
                name, args = 'net', ('bsc',)
            elif name == 'bscamount':
                name, args = 'netamount', ('bsc', *args)
            return name, args
    raise StaleButton(data)


def selection(product_id, quantity=None):
    quantity = int(quantity) if quantity else 1
    if not 1 <= quantity <= 100:
        raise StaleButton(quantity)
    return int(product_id), quantity


# ---------------------------------------------------------------- products and catalog

def get_product(product_id):
    product = displayed(store.product(product_id, public=True))
    if not product:
        raise ValueError(tr('Товар недоступен'))
    return product


def quantity_limit(product):
    return min(100, product['available']) if product['mode'] == 'auto' else 100


def validate_quantity(product, quantity):
    if type(quantity) is not int or not 1 <= quantity <= 100:
        raise ValueError(tr('Количество: целое число от 1 до 100.'))
    if product['mode'] == 'auto' and product['available'] < 1:
        raise ValueError(tr('Товар закончился'))
    if product['mode'] == 'auto' and quantity > product['available']:
        raise ValueError(tr('Недостаточно товара в наличии. Уменьшите количество.'))


def emoji_fallback(emoji_id):
    if not emoji_id:
        return ''
    return next((item['emoji'] for pack in store.emoji_packs() for item in pack['items']
                 if item['id'] == emoji_id), '📦')


def product_icon(product):
    """(html icon, plain fallback) for a product's custom emoji."""
    emoji_id = product.get('emoji_id') or ''
    fallback = emoji_fallback(emoji_id)
    if re.fullmatch(r'[0-9]{1,20}', emoji_id):
        return '<tg-emoji emoji-id="'+emoji_id+'">'+escape(fallback)+'</tg-emoji> ', escape(fallback)+' '
    return '📦 ', '📦 '


def price_block(product):
    """'💰 Цена: <s>old</s> <b>price</b> · −20%' with the struck-through 'was' price when higher."""
    price = product['price']
    old = product.get('old_price') or 0
    if old and old > price:
        line = tr('💰 Цена: <s>{0}</s> <b>{1}</b>', store.money(old), store.money(price))
        percent = round((old-price)*100/old)
        if percent >= 1:
            line += ' · −'+str(percent)+'%'
        return line
    return tr('💰 Цена: <b>{0}</b>', store.money(price))


def stock_line(product):
    if product['mode'] != 'auto':
        return tr('🛠 Выполняет менеджер после оплаты')
    if product['available']:
        return tr('📦 В наличии: <b>{0} шт.</b>', product['available'])
    return tr('⛔️ Нет в наличии')


def product_caption(product, quantity, icon, limit_description=None, custom=True):
    """HTML card; custom=False renders inline custom emoji tokens as their plain fallbacks."""
    description = product.get('raw_description', product['description']).strip()
    if limit_description is not None and len(description) > limit_description:
        cut = emoji_text.split_safe(description, max(0, limit_description-1))
        description = description[:cut].rstrip()+'…' if limit_description > 1 else ''
    badge = (product.get('badge') or '').strip()
    name = product.get('raw_name', product['name'])
    title = '<b>'+emoji_text.html(name, custom)+'</b>'+(' · 🏷 '+escape(badge) if badge else '')
    text = icon+title
    if description:
        text += '\n\n'+emoji_text.html(description, custom)
    text += '\n\n'+price_block(product)+'\n'+stock_line(product)
    if quantity_limit(product):
        if quantity > 1:
            text += '\n'+tr('🧮 {0} шт. × {1} = <b>{2}</b>', quantity, store.money(product['price']),
                            store.money(product['price']*quantity))
        text += '\n\n'+tr('Выбери количество кнопками ниже 👇')
    return text


def data_directory():
    return Path(store.path).resolve().parent


def product_image(product):
    """(cached Telegram file_id, local path) of the product photo; either may be empty."""
    file_id = product.get('image_file_id') or ''
    path = store.image_file(data_directory(), product.get('image_path') or '')
    return file_id, path


def is_file_reference_error(exc):
    reason = str(getattr(exc, 'message', exc)).lower()
    return any(item in reason for item in ('wrong file', 'file identifier', 'file_id', 'file reference',
                                           'wrong remote file', 'failed to get http url content'))


async def cache_file_id(product, result):
    photo = getattr(result, 'photo', None)
    if not photo:
        return
    file_id = photo[-1].file_id
    if file_id and file_id != product.get('image_file_id') and product.get('image_path'):
        try:
            # Only for the image actually sent: the panel may have replaced it meanwhile.
            await offload(store.cache_image_file_id, product['id'], file_id, product['image_path'])
            forget_catalog()
        except Exception as exc:
            log.debug('Product image file_id not cached: %s', type(exc).__name__)


def has_custom(product, icon, plain):
    return icon != plain or emoji_text.has_tokens(product.get('raw_name', '')+product.get('raw_description', ''))


def fitted_caption(product, quantity, icon, custom=True):
    caption = product_caption(product, quantity, icon, custom=custom)
    if fits_caption(caption, 'HTML'):
        return caption
    overflow = utf16_len(unescape(re.sub(r'<[^>]*>', '', caption)))-1010
    raw = product.get('raw_description', product['description']).strip()
    caption = product_caption(product, quantity, icon, max(0, len(raw)-overflow), custom)
    return caption if fits_caption(caption, 'HTML') else product_caption(product, quantity, icon, 0, custom)


async def send_product_photo(bot, chat, product, quantity, markup):
    """Photo card; returns False when the product has no usable photo (the caller sends text)."""
    file_id, path = product_image(product)
    sources = ([file_id] if file_id else [])+([path] if path else [])
    if not sources:
        return False
    icon, plain = product_icon(product)
    for index, source in enumerate(sources):
        media = source if isinstance(source, str) else FSInputFile(source)
        try:
            try:
                result = await bot.send_photo(chat, media, caption=fitted_caption(product, quantity, icon),
                                              parse_mode='HTML', reply_markup=markup)
            except TelegramBadRequest as exc:
                if is_file_reference_error(exc) or not has_custom(product, icon, plain):
                    raise
                result = await bot.send_photo(chat, media, caption=fitted_caption(product, quantity, plain, False),
                                              parse_mode='HTML', reply_markup=markup)
        except TelegramBadRequest as exc:
            if index+1 < len(sources) and is_file_reference_error(exc):
                continue
            log.info('Product %s photo not shown: %s', product['id'], getattr(exc, 'message', type(exc).__name__))
            return False
        if not isinstance(source, str):
            await cache_file_id(product, result)
        return True
    return False


async def product_card(bot, chat, product, quantity=1):
    limit = quantity_limit(product)
    if limit:
        validate_quantity(product, quantity)
    else:
        quantity = 1
    pid = str(product['id'])

    def target(value):
        return 'product:'+pid+':'+str(value)
    rows = []
    if limit:
        rows.append([('−', target(quantity-1) if quantity > 1 else 'noop'),
                     (tr('{0} шт.', quantity), 'noop'),
                     ('+', target(quantity+1) if quantity < limit else 'noop')])
        rows.append([(tr('Ввести количество'), 'qtyinput:'+pid+':'+str(quantity))])
        rows.append([(tr('🛒 Купить за {0}', store.money(product['price']*quantity)), 'checkout:'+pid+':'+str(quantity), 'success')])
    else:
        rows.append([(tr('Товар закончился'), 'noop', 'danger')])
    rows.append([(tr('← Назад'), 'pback:'+pid, 'primary')])
    markup = keyboard(rows, home=False)
    if await send_product_photo(bot, chat, product, quantity, markup):
        return
    icon, plain = product_icon(product)
    try:
        await bot.send_message(chat, product_caption(product, quantity, icon), parse_mode='HTML', reply_markup=markup)
    except TelegramBadRequest:
        if not has_custom(product, icon, plain):
            raise
        await bot.send_message(chat, product_caption(product, quantity, plain, custom=False), parse_mode='HTML',
                               reply_markup=markup)


def catalog_groups():
    groups = {}
    for product in public_products():
        groups.setdefault((product.get('category') or '').strip(), []).append(product)
    return list(groups.items())


def short(text, limit=40):
    return text if len(text) <= limit else text[:limit-1]+'…'


def pager(page, pages, prefix):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(('←', prefix+str(page-1)))
    row.append((str(page+1)+' / '+str(pages), 'noop'))
    if page+1 < pages:
        row.append(('→', prefix+str(page+1)))
    return [row]


def strip_icons(rows):
    """Drop custom emoji from buttons; True if there was any (Telegram may reject them)."""
    found = False
    for row in rows:
        for button in row:
            if isinstance(button, dict) and button.pop('icon_custom_emoji_id', None):
                found = True
    return found


async def render_catalog(bot, chat, title, rows):
    try:
        await brand_screen(bot, chat, title, section='catalog', parse_mode='HTML', reply_markup=keyboard(rows))
    except TelegramBadRequest:
        if not strip_icons(rows):
            raise
        await brand_screen(bot, chat, title, section='catalog', parse_mode='HTML', reply_markup=keyboard(rows))


def product_button(product):
    stock_label = tr('📦 {0} шт.', product['available']) if product['mode'] == 'auto' else tr('Под заказ')
    badge = (product.get('badge') or '').strip()
    button = {'text': (badge+' · ' if badge else '')+short(product['name'])+' · '+store.money(product['price'])+' · '+stock_label,
              'callback_data': 'product:'+str(product['id']),
              'style': ('success' if product['available'] > 0 else 'danger') if product['mode'] == 'auto' else 'primary'}
    if product.get('emoji_id'):
        button['icon_custom_emoji_id'] = product['emoji_id']
    return button


async def catalog_screen(bot, chat, page=0, groups=None):
    groups = catalog_groups() if groups is None else groups
    if not groups:
        await brand_screen(bot, chat, tr('🛍 Скоро здесь появятся товары. Загляни чуть позже!'), section='catalog',
                           parse_mode='HTML', reply_markup=keyboard([]))
        return
    if len(groups) == 1:
        await category_screen(bot, chat, 0, 0, groups)
        return
    pages = (len(groups)+CATEGORY_PAGE-1)//CATEGORY_PAGE
    page = max(0, min(page, pages-1))
    assignments = store.emoji_assignments()
    rows = []
    for index in range(page*CATEGORY_PAGE, min(len(groups), (page+1)*CATEGORY_PAGE)):
        name, items = groups[index]
        button = {'text': short(name or tr('Другое'))+' · '+str(len(items)), 'callback_data': 'cat:'+str(index), 'style': 'primary'}
        icon = assignments.get('category:'+name)
        if icon:
            button['icon_custom_emoji_id'] = icon
        rows.append([button])
    rows += pager(page, pages, 'catalog:')
    total = sum(len(items) for _, items in groups)
    await render_catalog(bot, chat, tr('🛍 <b>Каталог</b>\n\nТоваров: <b>{0}</b> · категорий: <b>{1}</b>\nВыбери категорию 👇',
                                       total, len(groups)), rows)


async def category_screen(bot, chat, index, page=0, groups=None):
    groups = catalog_groups() if groups is None else groups
    if not 0 <= index < len(groups):
        # Categories changed since the button was sent: show the fresh catalog.
        await catalog_screen(bot, chat, 0, groups)
        return
    name, products = groups[index]
    pages = (len(products)+CATALOG_PAGE-1)//CATALOG_PAGE
    page = max(0, min(page, pages-1))
    rows = [[product_button(product)] for product in products[page*CATALOG_PAGE:(page+1)*CATALOG_PAGE]]
    rows += pager(page, pages, 'cat:'+str(index)+':')
    if len(groups) > 1:
        rows.append([(tr('← Категории'), 'catalog', 'primary')])
        title = '🛍 <b>'+escape(name or tr('Другое'))+'</b> · '+str(len(products))+'\n\n' + \
            tr('Выбери товар — цена и остаток указаны на кнопке.')
    else:
        title = tr('🛍 <b>Каталог</b>\n\nВыбери товар — цена и остаток указаны на кнопке.')
    await render_catalog(bot, chat, title, rows)


async def product_back(bot, chat, product_id):
    groups = catalog_groups()
    for index, (name, products) in enumerate(groups):
        for position, product in enumerate(products):
            if product['id'] == product_id:
                await category_screen(bot, chat, index, position//CATALOG_PAGE, groups)
                return
    await catalog_screen(bot, chat, 0, groups)


# ---------------------------------------------------------------- long texts

TELEGRAM_TEXT_LIMIT = 4096


def message_chunks(text, limit=3500):
    """Telegram counts UTF-16 units; never split a code point or truncate secrets."""
    start = 0
    units = 0
    last_break = 0
    for index, char in enumerate(text):
        size = 2 if ord(char) > 0xffff else 1
        if units+size > limit:
            end = last_break if last_break > start else index
            end = emoji_text.split_safe(text, end) if end > start and '⟦' in text else end
            end = end if end > start else index
            yield text[start:end]
            start = end
            units = utf16_len(text[start:index])
        units += size
        if char == '\n':
            last_break = index+1
    if start < len(text):
        yield text[start:]


async def info_screen(bot, chat, title, body, *, section, rows=(), footer=''):
    """A screen with operator-provided text of any length (terms, support contacts)."""
    markup = keyboard(list(rows))
    plain = title+'\n\n'+body.strip()+(('\n\n'+footer) if footer else '')
    if utf16_len(plain) <= 3500:
        html = '<b>'+escape(title)+'</b>\n\n'+escape(body.strip())+(('\n\n'+escape(footer)) if footer else '')
        await brand_screen(bot, chat, html, section=section, parse_mode='HTML', reply_markup=markup)
        return
    chunks = list(message_chunks(plain))
    for index, chunk in enumerate(chunks):
        kwargs = {'parse_mode': None}
        if index == len(chunks)-1:
            kwargs['reply_markup'] = markup
        if index == 0:
            await bot.send_message(chat, chunk, **kwargs)
        else:
            await transport(bot).send_message(chat, chunk, **kwargs)


RECEIPT = '✅ Покупка успешно завершена!\n\nТовар: {0} (x{1})\nСписано: {2}'


def receipt_headings():
    return {tr(RECEIPT, lang=code).split('\n', 1)[0] for code in ('ru', 'en')}


async def send_result(bot, chat, text, rows=()):
    chunks = list(message_chunks(text))
    selected = store.language(chat) or 'ru'
    headings = receipt_headings()
    for index, chunk in enumerate(chunks):
        kwargs = {'parse_mode': None}
        heading = chunk.split('\n', 1)[0]
        if index == 0 and heading in headings:
            kwargs['entities'] = [MessageEntity(type='bold', offset=0, length=utf16_len(heading))]
        if index == len(chunks)-1:
            kwargs['reply_markup'] = keyboard(list(rows)+[[(tr('🏠 Главное меню', lang=selected), 'home', 'primary')]], home=False)
        await bot.send_message(chat, chunk, **kwargs)


# ---------------------------------------------------------------- orders

def order_row(order_id):
    with store.read() as connection:
        row = connection.execute('SELECT id,user_id,product_id,title,price,quantity,status,created FROM orders WHERE id=?',
                                 (order_id,)).fetchone()
        return dict(row) if row else None


def customer_order(user_id, order_id):
    order = order_row(order_id)
    if not order or order['user_id'] != user_id or order['status'] in ('pending', 'expired'):
        raise ValueError(tr('Заказ не найден'))
    return order


def details_rows(order_id):
    return [[(tr('✍️ Отправить данные'), 'details:'+order_id, 'success')]]


def active_orders(user_id, limit=5):
    with store.read() as connection:
        return [dict(row) for row in connection.execute(
            'SELECT id,title,status FROM orders WHERE user_id=? AND status IN (%s) ORDER BY created DESC LIMIT ?'
            % ','.join('?'*len(ACTIVE_STATES)), (user_id, *ACTIVE_STATES, limit))]


async def purchases_menu(bot, chat, user_id, page=0):
    purchases = store.purchases(user_id, page)
    active = active_orders(user_id) if purchases['page'] == 0 else []
    rows = []
    for order in active:
        mark = '✍️ ' if order['status'] == 'awaiting_details' else '⏳ '
        rows.append([(mark+short(order['title'], 30)+' · '+order_status(order['status']), 'order:'+order['id'],
                      'primary')])
    if purchases['total'] or active:
        text = tr('📦 <b>Мои покупки</b>')
        if active:
            text += '\n\n'+tr('⏳ Активных заказов: <b>{0}</b> — они сверху.', len(active))
        if purchases['total']:
            text += '\n\n'+tr('✅ Выполнено: <b>{0}</b>\nВыбери товар, чтобы открыть покупку 👇', purchases['total'])
        for item in purchases['items']:
            rows.append([(short(item['title'], 60), 'purchase:'+item['id']+':'+str(purchases['page']), 'success')])
        rows += pager(purchases['page'], purchases['pages'], 'orders:')
    else:
        text = tr('📦 <b>Мои покупки</b>\n\nЗдесь будут твои успешные покупки.\nПосле выполнения заказа можно открыть его детали и повторно получить товар.')
        rows.append([(tr('🛒 Открыть каталог'), 'catalog', 'success')])
    await brand_screen(bot, chat, text, section='orders', parse_mode='HTML', reply_markup=keyboard(rows))


RULE = '━━━━━━━━━━━━'


async def purchase_card(bot, chat, user_id, order_id, page=0):
    item = store.purchase(user_id, order_id)
    await bot.send_message(chat, tr('✅ <b>Покупка выполнена</b>\n\n<b>{0}</b>\n{1}\n📦 Количество: <b>{2} шт.</b>\n💳 Оплачено: <b>{3}</b>\n📅 Дата: {4}\n\nТовар можно получить повторно кнопкой ниже.',
                                    escape(item['title']), RULE, item['quantity'], store.money(item['price']),
                                    fmt_date(item['created'])),
                           parse_mode='HTML', reply_markup=keyboard([
                               [(tr('📥 Получить товар повторно'), 'get:'+item['id'], 'success')],
                               [(tr('🔁 Купить снова'), 'again:'+item['id'], 'primary'), (tr('💬 Поддержка'), 'support')],
                               [(tr('← К моим покупкам'), 'orders:'+str(max(0, page)), 'primary')]]))


async def buy_again(bot, chat, user_id, order_id):
    product_id, quantity = store.repeat_order(user_id, order_id)
    product = get_product(product_id)
    if product['mode'] == 'auto':
        if product['available'] < 1:
            raise ValueError(tr('Товар закончился'))
        quantity = min(quantity, product['available'])
    await checkout_screen(bot, chat, user_id, product['id'], max(1, min(quantity, 100)), product=product)


async def order_card(bot, chat, user_id, order_id):
    order = customer_order(user_id, order_id)
    if order['status'] == 'fulfilled':
        await purchase_card(bot, chat, user_id, order_id)
        return
    hints = {
        'awaiting_details': tr('Менеджеру нужны данные для выполнения. Нажми «Отправить данные» и пришли их одним сообщением.'),
        'needs_review': tr('Оплата получена, заказ проверяет поддержка. Мы напишем тебе, как только всё будет готово.'),
    }
    hint = hints.get(order['status'], tr('Менеджер выполнит заказ и пришлёт результат в этот чат.'))
    rows = details_rows(order['id']) if order['status'] == 'awaiting_details' else []
    rows += [[(tr('💬 Поддержка'), 'support', 'primary')], [(tr('← К моим покупкам'), 'orders', 'primary')]]
    await bot.send_message(chat, tr('🧾 <b>{0}</b>\n\n📦 Количество: <b>{1} шт.</b>\n💳 Оплачено: <b>{2}</b>\n📅 Дата: {3}\n📍 Статус: <b>{4}</b>\n🆔 Заказ: <code>{5}</code>\n\n{6}',
                                    escape(order['title']), order['quantity'], store.money(order['price']), fmt_date(order['created']),
                                    order_status(order['status']), order['id'][:8], hint),
                           parse_mode='HTML', reply_markup=keyboard(rows))


async def details_prompt(bot, chat, user_id, order_id):
    order = customer_order(user_id, order_id)
    if order['status'] != 'awaiting_details':
        raise ValueError(tr('Данные по этому заказу уже получены.'))
    await set_mode(user_id, 'details:'+order_id)
    await bot.send_message(chat, tr('✍️ <b>Данные для заказа</b>\n\n{0}\n\nОтправь одним сообщением всё, что нужно менеджеру для выполнения (до 2000 символов).\nПароли отправлять не нужно.',
                                    escape(order['title'])+' × '+str(order['quantity'])),
                           parse_mode='HTML', reply_markup=keyboard([[(tr('✖️ Отмена'), 'order:'+order_id)]], home=False))


async def submit_details(bot, chat, user_id, order_id, text):
    text = text.strip()
    if not text:
        await bot.send_message(chat, tr('Отправь данные текстом одним сообщением или нажми /cancel.'))
        return
    if len(text) > 2000:
        await bot.send_message(chat, tr('Максимум 2000 символов. Сократи сообщение и отправь ещё раз.'))
        return
    await set_mode(user_id, '')
    try:
        await offload(store.details, order_id, user_id, text)
    except ValueError as exc:
        await bot.send_message(chat, error_text(str(exc)), reply_markup=keyboard([[(tr('📦 Мои покупки'), 'orders', 'primary')]]))
        return
    await bot.send_message(chat, tr('✅ Данные приняты. Заказ передан менеджеру — результат придёт в этот чат.'),
                           reply_markup=keyboard([[(tr('📦 Мои покупки'), 'orders', 'primary')]]))


# ---------------------------------------------------------------- support chat

def support_desk():
    return support.Support(store)


async def support_prompt(bot, chat, user_id):
    await set_mode(user_id, 'support')
    await brand_screen(bot, chat, tr('✍️ <b>Сообщение в поддержку</b>\n\nОпиши вопрос одним или несколькими сообщениями — можно приложить скриншот с подписью. Если вопрос по заказу, укажи его номер.\n\nОтвет придёт в этот чат.'),
                       section='support', parse_mode='HTML',
                       reply_markup=keyboard([[(tr('✖️ Отмена'), 'support')]]))


async def submit_support(bot, m):
    user_id = m.from_user.id
    chat = m.chat.id
    text = (m.text or m.caption or '').strip()
    file_id = m.photo[-1].file_id if m.photo else None
    desk = support_desk()
    if not text and not file_id:
        await bot.send_message(chat, tr('Отправь вопрос текстом или скриншотом с подписью. Выйти: /cancel'))
        return
    try:
        await offload(desk.open_or_append, user_id, text, kind='photo' if file_id else 'text', file_id=file_id)
    except ValueError as exc:
        await bot.send_message(chat, error_text(str(exc)), reply_markup=keyboard([]))
        return
    await bot.send_message(chat, tr('✅ Сообщение отправлено — ответим здесь же.\nМожешь дописать подробности следующим сообщением.'),
                           reply_markup=keyboard([[(tr('📦 Мои покупки'), 'orders', 'primary')]]))


async def deliver_support_replies(bot):
    desk = support_desk()
    for item in await offload(desk.outbox_due, 20):
        token = language.set(item.get('language') or store.language(item['user_id']) or 'ru')
        try:
            # Exactly one Telegram message per outbox item: a retry never repeats a part that
            # was already delivered. Support.reply keeps replies within one message.
            heading = tr('💬 Ответ поддержки:')
            body = heading+'\n\n'+item['text']
            if utf16_len(body) > TELEGRAM_TEXT_LIMIT:  # replies stored before the limit
                body = next(message_chunks(body, TELEGRAM_TEXT_LIMIT-1))+'…'
            await bot.send_message(item['user_id'], body, parse_mode=None,
                                   entities=[MessageEntity(type='bold', offset=0, length=utf16_len(heading))],
                                   reply_markup=keyboard([[(tr('↩️ Ответить'), 'write', 'success')]]))
            await offload(desk.outbox_result, item['id'], True)
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter:
            await offload(desk.outbox_result, item['id'], False)
            return
        except Exception as exc:
            permanent = is_gone(exc)
            log.warning('Support reply failed: id=%s (%s%s)', item['id'], type(exc).__name__, ', permanent' if permanent else '')
            await offload(report, desk.outbox_result, item['id'], False, permanent=permanent)
        finally:
            language.reset(token)


# ---------------------------------------------------------------- checkout

async def new_order(bot, chat, pid, user_id, quantity):
    """Create (or reuse) the pending order."""
    order = await offload(store.create_order, pid, user_id, quantity)
    forget_catalog()
    return order


async def checkout_screen(bot, chat, user_id, pid, quantity, product=None):
    product = product or get_product(pid)
    validate_quantity(product, quantity)
    selected = str(pid)+':'+str(quantity)
    methods = available_methods()
    lines = [tr('🧾 <b>Оформление заказа</b>'), '',
             '<b>'+escape(product['name'])+'</b>',
             tr('{0} шт. × {1}', quantity, store.money(product['price'])), RULE,
             tr('💳 <b>Итого: {0}</b>', store.money(product['price']*quantity)),
             '', tr('Выбери способ оплаты 👇')]
    rows = [[(tr('👛 Баланс магазина'), 'wbuy:'+selected, 'success')]]
    if methods['stars']:
        rows.append([('⭐ Telegram Stars', 'buy:'+selected, 'primary')])
    rows += crypto_buttons(selected)
    rows.append([(tr('← Назад'), 'product:'+selected)])
    await bot.send_message(chat, '\n'.join(lines), parse_mode='HTML', reply_markup=keyboard(rows))


# ---------------------------------------------------------------- promo codes (credit the wallet)

PROMO_SHAPE = re.compile(r'[A-Z0-9_-]{3,32}')


def promo_back():
    return keyboard([[(tr('👛 Кошелёк'), 'wallet', 'primary')]])


async def promo_prompt(bot, chat, user_id):
    await set_mode(user_id, 'promo')
    await brand_screen(bot, chat, tr('🎟 <b>Промокод</b>\n\nОтправь код одним сообщением — сумма сразу зачислится на баланс.\nМожно и командой: /promo КОД'),
                       section='wallet', parse_mode='HTML', reply_markup=promo_back())


async def promo_input(bot, chat, user_id, text, attempt_ref):
    """Activate a typed code. Input mode stays on after a mistake, so the next message is a new try."""
    code = text.strip().upper()
    if not PROMO_SHAPE.fullmatch(code):
        # Ordinary chat text is not an attempt: nothing is counted.
        await bot.send_message(chat, tr('Промокод — от 3 до 32 латинских букв, цифр, «-» или «_».')+'\n'
                               + tr('Отправь код ещё раз или вернись в кошелёк.'), reply_markup=promo_back())
        return
    try:
        result = await offload(promo.Promos(store).redeem_to_balance, code, user_id, attempt_ref)
    except ValueError as exc:
        if str(exc) == promo.TOO_MANY:
            await set_mode(user_id, '')
            await bot.send_message(chat, '⏳ '+error_text(str(exc)), reply_markup=promo_back())
        else:
            await bot.send_message(chat, '❌ '+error_text(str(exc))+'\n'+tr('Отправь код ещё раз или вернись в кошелёк.'),
                                   reply_markup=promo_back())
        return
    await set_mode(user_id, '')
    await bot.send_message(chat, tr('✅ Промокод активирован! На баланс зачислено {0}. Баланс: {1}',
                                    store.money(result['amount_cents']), store.money(result['balance'])),
                           reply_markup=keyboard([[(tr('🛒 Открыть каталог'), 'catalog', 'success'),
                                                   (tr('👛 Кошелёк'), 'wallet', 'primary')]]))


# ---------------------------------------------------------------- main screens

def menu_summary(user_id):
    """Balance and number of active orders for the main menu, in one short read."""
    with store.read() as connection:
        balance = connection.execute('SELECT coalesce(sum(delta),0) FROM wallet_ledger WHERE user_id=?',
                                     (user_id,)).fetchone()[0]
        active = connection.execute('SELECT count(*) FROM orders WHERE user_id=? AND status IN (%s)'
                                    % ','.join('?'*len(ACTIVE_STATES)), (user_id, *ACTIVE_STATES)).fetchone()[0]
    return balance, active


async def menu(bot, chat, name='', username=''):
    brand = escape(shop_name())
    display_name = escape(name)
    if username and re.fullmatch(r'[A-Za-z0-9_]{1,32}', username):
        display_name = f'<a href="https://t.me/{username}">@{username}</a>'
    greeting = tr('Привет, ')+display_name+'!' if display_name else tr('Привет!')

    assignments = store.emoji_assignments()
    chosen = {assignments[target] for target in WELCOME_EMOJIS if assignments.get(target)}
    alternatives = {}
    if chosen:
        alternatives = {item['id']: item['emoji'] for pack in store.emoji_packs()
                        for item in pack['items'] if item['id'] in chosen and item.get('emoji') not in ('◈', '', None)}
    animated, plain = [], []
    for target, (default_id, default_emoji) in WELCOME_EMOJIS.items():
        emoji_id = assignments.get(target, default_id)
        fallback = escape(alternatives.get(emoji_id, default_emoji))
        plain.append(fallback)
        animated.append(f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
                        if re.fullmatch(r'[0-9]{1,20}', emoji_id) else fallback)
    balance, active = menu_summary(chat)
    status = tr('👛 Баланс: <b>{0}</b>', store.money(balance))
    if active:
        status += '\n'+tr('⏳ Активных заказов: <b>{0}</b>', active)

    def caption(icons):
        return tr('{0} {1}\n\nДобро пожаловать в <b>{2}</b> {3}\n\n{4}\n\nВыбирай товары в каталоге {5}',
                  icons[0], greeting, brand, icons[1], status, icons[2])

    markup = keyboard([
        [(tr('🛒 Открыть каталог'), 'catalog', 'success'), (tr('🎟 Промокод'), 'promo', 'primary')],
        [(tr('👛 Кошелёк'), 'wallet', 'primary'), (tr('📦 Мои покупки'), 'orders', 'primary')],
        [(tr('💬 Поддержка'), 'support'), (tr('🌐 Язык'), 'language')],
        [(tr('ℹ️ О магазине'), 'about')],
    ], home=False)
    try:
        await brand_screen(bot, chat, caption(animated), parse_mode='HTML', reply_markup=markup)
    except TelegramBadRequest:
        if animated == plain:
            raise
        await brand_screen(bot, chat, caption(plain), parse_mode='HTML', reply_markup=markup)


def available_methods():
    """Payment methods a buyer can use right now (USD flows need the dollar currency)."""
    methods = store.payment_methods()
    usd = store.currency()['active']
    networks = [item for item in methods['networks'] if item['enabled'] and item['wallet']] if usd else []
    return {'stars': bool(methods['stars']), 'networks': networks, 'usd': usd}


def require_network(network):
    if not any(item['network'] == network for item in available_methods()['networks']):
        raise ValueError(tr('Способ оплаты недоступен'))


def require_stars():
    if not store.payment_methods()['stars']:
        raise ValueError(tr('Способ оплаты недоступен'))


def stars_rate():
    return store.currency()['stars_per_usd']


def history_line(item):
    labels = {'stars': tr('Пополнение Stars'), 'usdt': tr('Пополнение USDT'), 'order': tr('Покупка'),
              'adjust': tr('Корректировка'), 'refund': tr('Возврат'), 'promo': tr('Промокод')}
    kind = item.get('kind')
    reference = item.get('reference') or ''
    if not kind:
        if reference.startswith('topup:'):
            kind = 'stars'
        elif reference.startswith('order:'):
            kind = 'order'
        elif reference.startswith('promo:'):
            kind = 'promo'
        elif reference.split(':', 1)[0] in NETWORKS:
            kind = 'usdt'
    label = labels.get(kind, tr('Операция'))
    amount = ('＋ ' if item['delta'] > 0 else '− ')+store.money(abs(item['delta']))
    date = datetime.fromtimestamp(item['created']).strftime('%d.%m') if item.get('created') else ''
    return (date+' · ' if date else '')+label+': '+amount


async def wallet_menu(bot, chat, user_id):
    wallet = store.wallet(user_id)
    history = '\n'.join(history_line(item) for item in wallet['history'])
    methods = available_methods()
    available = (['⭐ Telegram Stars'] if methods['stars'] else []) + \
        (['USDT · '+' / '.join(item['label'] for item in methods['networks'])] if methods['networks'] else [])
    ways = '\n'.join(available) or tr('Пополнение временно недоступно. Напиши в поддержку.')
    rows = [[(tr('➕ Пополнить баланс'), 'deposit', 'success')]] if available else []
    rows += [[(tr('🎟 Промокод'), 'promo', 'primary')],
             [(tr('🛒 Открыть каталог'), 'catalog', 'primary'), (tr('🔄 Обновить'), 'wallet')],
             [(tr('💬 Помощь с оплатой'), 'support')]]
    await brand_screen(bot, chat,
                       tr('👛 <b>Твой кошелёк</b>\n\nБаланс: <b>{0}</b>\nTelegram ID: <code>{1}</code>\n\n<b>Способы пополнения</b>\n{2}\n\n<b>Последние операции</b>\n{3}',
                          store.money(wallet['balance']), user_id, escape(ways), escape(history) or tr('Операций пока нет.')),
                       section='wallet', parse_mode='HTML', reply_markup=keyboard(rows))


async def deposit_menu(bot, chat):
    methods = available_methods()
    rows = [[('⭐ Telegram Stars', 'stars', 'primary')]] if methods['stars'] else []
    rows += [[('USDT · '+item['label'], 'net:'+item['network'], 'success')] for item in methods['networks']]
    lines = []
    if methods['stars']:
        lines.append(tr('⭐ Stars — оплата прямо в Telegram.'))
    if methods['networks']:
        lines.append(tr('💵 USDT — перевод с криптокошелька, выбери сеть.'))
    if not rows:
        rows = [[(tr('💬 Поддержка'), 'support', 'primary')]]
        lines.append(tr('Пополнение временно недоступно. Напиши в поддержку.'))
    rows.append([(tr('← Кошелёк'), 'wallet', 'primary')])
    await send_bep20(bot, chat, tr('💳 Выбери способ пополнения')+'\n\n'+'\n'.join(lines), network='usdt',
                     reply_markup=keyboard(rows))


async def stars_menu(bot, chat):
    require_stars()
    rate = stars_rate()
    custom = [(tr('✏️ Своя сумма'), 'custom:stars', 'primary')]
    back = [(tr('← Способы оплаты'), 'deposit', 'primary')]
    if rate:
        def option(cents):
            return ('$'+str(cents//100)+' · '+str(cents_to_stars(cents, rate))+' ⭐', 'usdtopup:'+str(cents))
        rows = [[option(500), option(1000)], [option(2500), option(5000)], custom, back]
        text = tr('⭐ Пополнение через Stars\n\nВыбери сумму в долларах.\nКурс: {0} Stars за $1.', rate)
    else:
        rows = [[(str(n)+' ⭐', 'topup:'+str(n)) for n in (50, 100)], [(str(n)+' ⭐', 'topup:'+str(n)) for n in (250, 500)],
                custom, back]
        text = tr('⭐ Пополнение через Stars\n\nВыбери, сколько Stars зачислить на баланс.')
    await brand_screen(bot, chat, text, section='deposit', reply_markup=keyboard(rows))


async def network_menu(bot, chat, network):
    require_network(network)
    rows = [[('$5', 'netamount:'+network+':500', 'success'), ('$10', 'netamount:'+network+':1000', 'success')],
            [('$25', 'netamount:'+network+':2500', 'primary'), ('$50', 'netamount:'+network+':5000', 'primary')],
            [(tr('✏️ Своя сумма'), 'custom:'+network, 'primary')], [(tr('← Способы оплаты'), 'deposit')]]
    await send_bep20(bot, chat, '💵 USDT · '+NETWORKS[network]['label']+'\n\n'+tr('Выбери сумму пополнения. Минимум $1 · максимум $1000'),
                     network=network, reply_markup=keyboard(rows))


async def custom_amount_prompt(bot, chat, user_id, method):
    if method == 'stars':
        require_stars()
    else:
        require_network(method)
    await set_mode(user_id, method)
    text = tr('✏️ Введи сумму пополнения в долларах.\nНапример: 12.50') if method != 'stars' or stars_rate() \
        else tr('✏️ Введи, сколько Stars зачислить — целое число от 1 до 10 000.\nНапример: 250')
    await brand_screen(bot, chat, text, section='deposit',
                       reply_markup=keyboard([[(tr('← Способы оплаты'), 'deposit', 'primary')]]))


async def topup_invoice(bot, chat, user_id, amount):
    require_stars()
    tid = await offload(store.create_topup, user_id, amount)
    await bot.send_invoice(chat, title=tr('Пополнение кошелька'),
                           description=tr('К зачислению {0} за {1} Stars. Курс зафиксирован. Условия: /terms. Помощь: /paysupport.',
                                          store.money(store.topup_info(tid)['credit_cents']), amount),
                           payload=tid, currency='XTR', provider_token='',
                           prices=[{'label': tr('Пополнение баланса'), 'amount': amount}])


async def stars_amount_invoice(bot, chat, user_id, value):
    """Custom Stars amount: dollars in USD mode, whole Stars otherwise."""
    rate = stars_rate()
    if rate:
        cents = usd_cents(value.replace(',', '.').removeprefix('$'), positive=True)
        if cents < 1:
            raise ValueError(tr('Сумма должна быть больше нуля'))
        stars = cents_to_stars(cents, rate)
    else:
        if not re.fullmatch(r'[0-9]{1,5}', value):
            raise ValueError(tr('Сумма пополнения: от 1 до 10 000 Stars, целым числом.'))
        stars = int(value)
    await topup_invoice(bot, chat, user_id, stars)


def deposit_amount(d):
    return crypto_amount(d['amount_raw'], d.get('network', 'bsc'))


def crypto_buttons(selected):
    return [[('USDT · '+item['label'], 'netbuy:'+item['network']+':'+selected, 'success')]
            for item in available_methods()['networks']]


async def crypto_invoice(bot, chat, user_id, cents, order_id=None, network='bsc'):
    require_network(network)
    d = await offload(store.create_crypto_deposit, user_id, cents, order_id=order_id, network=network)
    await offload(store.crypto_context, user_id, d['id'])
    transfer_minutes = (d['expires']-d['created'])//60
    reserve_note = tr('Резерв товара действует 5 минут. После истечения резерва оплата поступит на баланс.\n') if order_id else ''
    caption = tr('💵 <b>{0} {1}</b>\n\n🟡 USDT · BEP20\n\nК переводу: <code>{2}</code> USDT\nАдрес: <code>{3}</code>\n\nНа перевод есть {4} минут.\n{5}После оплаты нажми «Я оплатил» и пришли TXID.',
                 tr('Оплата заказа') if order_id else tr('Пополнение'), store.money(d['credit_cents']), deposit_amount(d),
                 d['wallet'], transfer_minutes, reserve_note)
    caption = caption.replace('🟡 USDT · BEP20', 'USDT · '+NETWORKS[network]['label']).replace('💵', network_icon(network))
    await bot.send_photo(chat, BufferedInputFile(await qr_png(d['wallet']), filename='usdt-'+network+'.png'),
                         caption=caption, parse_mode='HTML',
                         reply_markup=keyboard([
                             [{'text': tr('📋 Скопировать адрес'), 'copy_text': {'text': d['wallet']}, 'style': 'primary'},
                              {'text': tr('📋 Сумма'), 'copy_text': {'text': deposit_amount(d)}, 'style': 'primary'}],
                             [(tr('✅ Я оплатил'), 'bscpaid:'+d['id'], 'success')],
                             [(tr('👛 Кошелёк'), 'wallet', 'primary')]]))


# ---------------------------------------------------------------- crypto verification

def crypto_failure_text(detail, remaining):
    if remaining <= 0:
        return tr('❌ Пополнение отменено: использованы все 5 попыток ввода TXID. Создай новый счёт в кошельке.')
    return '❌ '+error_text(detail)+'\n\n'+tr('Осталось попыток: {0} из 5. Пришли верный TXID.', remaining)


def crypto_check(deposit_id):
    with store.tx() as db:
        row = db.execute('SELECT txid,status,detail FROM crypto_checks WHERE deposit_id=?', (deposit_id,)).fetchone()
        return dict(row) if row else None


def looks_like_txid(network, text):
    try:
        normalize_hash(network, text)
        return True
    except ValueError:
        return False


def txid_context(user_id, text):
    """The open deposit a hash-shaped message could pay, or None."""
    if not HASH_SHAPE.fullmatch(text):
        return None
    did = store.crypto_context(user_id)
    if not did:
        return None
    try:
        deposit = store.crypto_deposit(did, user_id)
    except ValueError:
        return None
    if deposit['cancelled'] or deposit['txid'] or not looks_like_txid(deposit['network'], text):
        return None
    return deposit


def crypto_success_text(deposit, user_id):
    if deposit['order_id']:
        message = tr('✅ Заказ оплачен. Результат или инструкция придут отдельным сообщением.') if deposit['applied_order'] == 1 \
            else tr('✅ Перевод получен. Резерв заказа истёк — деньги зачислены на баланс. Выбери товар заново.')
    else:
        message = tr('✅ Зачислено {0}.', store.money(deposit['credit_cents']))
    return message+tr('\nБаланс: {0}', store.money(store.wallet(user_id)['balance']))


def wallet_rows():
    return [[(tr('👛 Кошелёк'), 'wallet', 'primary')]]


async def txid_prompt(bot, chat, user_id, deposit_id):
    deposit = store.crypto_deposit(deposit_id, user_id)
    if deposit['cancelled']:
        raise ValueError(tr('Пополнение отменено: исчерпаны 5 попыток ввода TXID.'))
    if deposit['txid']:
        raise ValueError(tr('Этот счёт уже оплачен. Создай новый.'))
    await offload(store.crypto_context, user_id, deposit['id'])
    await set_mode(user_id, 'txid')
    check = crypto_check(deposit['id'])
    note = tr('Предыдущий TXID ещё проверяется. Если ошибся — просто пришли верный.') \
        if check and check['status'] == 'pending' else tr('Осталось попыток: {0} из 5.', TXID_ATTEMPTS-deposit['failed_attempts'])
    await brand_screen(bot, chat, tr('🔎 Пришли TXID перевода одним сообщением.\nЕго можно скопировать в истории отправлений кошелька.')+'\n\n'+note,
                       section='deposit', reply_markup=keyboard([[(tr('💬 Поддержка'), 'support', 'primary'), (tr('👛 Кошелёк'), 'wallet', 'primary')]]))


async def submit_txid(bot, chat, user_id, text, attempt_ref):
    """Validate a TXID message, queue it, and verify it in the background."""
    await set_mode(user_id, '')
    did = store.crypto_context(user_id)
    if not did:
        await bot.send_message(chat, tr('Сначала создай счёт USDT в кошельке.'), reply_markup=keyboard(wallet_rows()))
        return
    deposit = store.crypto_deposit(did, user_id)
    if deposit['cancelled']:
        await bot.send_message(chat, tr('Пополнение отменено: исчерпаны 5 попыток ввода TXID.'), reply_markup=keyboard(wallet_rows()))
        return
    if not looks_like_txid(deposit['network'], text):
        # Ordinary chat text never consumes an attempt.
        await set_mode(user_id, 'txid')
        await bot.send_message(chat, tr('🤔 Это не похоже на TXID. Пришли хеш транзакции из истории кошелька — это строка из 64 символов (для BEP20 и Aptos начинается с 0x).\nПопытка не потрачена. Отменить ввод: /cancel'),
                               reply_markup=keyboard([[(tr('💬 Поддержка'), 'support', 'primary'), (tr('👛 Кошелёк'), 'wallet', 'primary')]]))
        return
    txid = normalize_hash(deposit['network'], text)
    if deposit['txid']:
        if deposit['txid'] != txid:
            raise ValueError(tr('Этот счёт уже оплачен. Создай новый.'))
        await bot.send_message(chat, crypto_success_text(deposit, user_id), reply_markup=keyboard(wallet_rows()))
        return
    check = crypto_check(did)
    if check and check['txid'] == txid:
        if check['status'] == 'pending':
            await bot.send_message(chat, tr('⏳ Этот TXID уже проверяется. Бот сообщит о зачислении автоматически.'),
                                   reply_markup=keyboard(wallet_rows()))
            return
        if check['status'] == 'review':
            await bot.send_message(chat, tr('Этот TXID уже проверен: {0}\nЕсли ошибся — пришли другой TXID или напиши в поддержку.',
                                            error_text(check['detail'] or '')),
                                   reply_markup=keyboard([[(tr('💬 Поддержка'), 'support', 'primary')]]))
            return
    await offload(store.queue_crypto_check, did, txid, attempt_ref)
    await bot.send_message(chat, tr('🔎 TXID получен, проверяю перевод… Обычно это занимает до минуты — результат пришлю сюда.'))
    spawn(verify_and_notify(transport(bot), chat, user_id, did, txid, attempt_ref))


async def verify_and_notify(bot, chat, user_id, did, txid, attempt_ref):
    try:
        deposit = store.crypto_deposit(did, user_id)
        try:
            await verify_crypto(deposit, txid, attempt_ref)
        except PendingPayment as exc:
            await bot.send_message(chat, tr('⏳ Перевод пока не подтверждён: {0}\nTXID сохранён — бот повторит проверку автоматически и сообщит о зачислении.',
                                            error_text(str(exc))), reply_markup=keyboard(wallet_rows()))
            return
        except ValueError as exc:
            deposit = store.crypto_deposit(did, user_id)
            remaining = max(0, TXID_ATTEMPTS-deposit['failed_attempts'])
            rows = wallet_rows() if deposit['cancelled'] or remaining <= 0 else \
                [[(tr('🔁 Ввести другой TXID'), 'bscpaid:'+did, 'success')]]+wallet_rows()
            if deposit['cancelled']:
                clear_crypto_context(user_id)
            await bot.send_message(chat, crypto_failure_text(str(exc), remaining), reply_markup=keyboard(rows))
            store.crypto_failure_notice_result(attempt_ref, True)
            return
        deposit = store.crypto_deposit(did, user_id)
        await bot.send_message(chat, crypto_success_text(deposit, user_id), reply_markup=keyboard(wallet_rows()))
        store.crypto_notification(did, True)
        clear_crypto_context(user_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The crypto worker re-checks and re-notifies from the persisted state.
        log.warning('Background TXID check deposit=%s: %s', did, type(exc).__name__)


def _verifier(network):
    method = next(item for item in store.payment_methods(private=True)['networks'] if item['network'] == network)
    return BscVerifier() if network == 'bsc' else NetworkVerifier(network, method['api_key'])


async def verify_crypto(deposit, txid, attempt_ref=None):
    """Verify one transfer; record the outcome. Raises PendingPayment or ValueError (definite)."""
    reference = attempt_ref or deposit.get('attempt_ref')
    try:
        proof = await _verifier(deposit.get('network', 'bsc')).verify(txid, deposit['wallet'], deposit['token'], deposit['amount_raw'])
    except PendingPayment as exc:
        store.crypto_check_result(deposit['id'], txid, True, str(exc), reference)
        raise
    except ValueError as exc:
        if PaymentMismatch is not None and not isinstance(exc, PaymentMismatch):
            # Contract item 11: only PaymentMismatch is definite; anything else is temporary.
            store.crypto_check_result(deposit['id'], txid, True, str(exc), reference)
            raise PendingPayment(str(exc)) from exc
        store.crypto_check_result(deposit['id'], txid, False, str(exc), reference)
        raise
    except Exception as exc:
        log.warning('Crypto verifier error deposit=%s: %s', deposit['id'], type(exc).__name__)
        detail = 'Узел сети временно недоступен. Проверка повторится автоматически.'
        store.crypto_check_result(deposit['id'], txid, True, detail, reference)
        raise PendingPayment(detail) from exc
    try:
        store.credit_crypto(deposit['id'], deposit['user_id'], proof)
    except ValueError as exc:
        store.crypto_check_result(deposit['id'], txid, False, str(exc), reference)
        raise


async def crypto_worker(bot):
    while True:
        try:
            await crypto_round(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Crypto worker will retry: %s', type(exc).__name__)
        await asyncio.sleep(5)


async def crypto_round(bot):
    for deposit in store.crypto_checks_due():
        try:
            await verify_crypto(deposit, deposit['submitted'])
        except ValueError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Crypto check deposit=%s failed: %s', deposit['id'], type(exc).__name__)
    for deposit in store.crypto_notifications_due():
        try:
            await check_crypto_notification(bot, deposit)
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter:
            store.crypto_notification(deposit['id'], False)
            return
        except Exception as exc:
            report(store.crypto_notification, deposit['id'], False, permanent=is_gone(exc))
    for notice in store.crypto_failure_notices():
        token = language.set(store.language(notice['user_id']) or 'ru')
        try:
            await bot.send_message(notice['user_id'], crypto_failure_text(notice['detail'], notice['remaining']),
                                   reply_markup=keyboard(wallet_rows()))
            store.crypto_failure_notice_result(notice['reference'], True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report(store.crypto_failure_notice_result, notice['reference'], False, permanent=is_gone(exc))
        finally:
            language.reset(token)


async def check_crypto_notification(bot, deposit):
    token = language.set(store.language(deposit['user_id']) or 'ru')
    try:
        await _check_crypto_notification(bot, deposit)
    finally:
        language.reset(token)


async def _check_crypto_notification(bot, deposit):
    amount = store.money(deposit['credit_cents'])
    message = tr('✅ Перевод подтверждён. Заказ оплачен.') if deposit['applied_order'] \
        else tr('✅ Перевод подтверждён. На баланс зачислено {0}.', amount)
    await bot.send_message(deposit['user_id'], message, reply_markup=keyboard(wallet_rows()))
    store.crypto_notification(deposit['id'], True)


# ---------------------------------------------------------------- language and channel gate

async def language_menu(bot, chat):
    await brand_screen(bot, chat, '<b>'+escape(shop_name())+'</b>\n\n🌐 Выбери язык / Choose your language',
                       section='language', parse_mode='HTML',
                       reply_markup=keyboard([[('🇷🇺 Русский', 'lang:ru', 'primary'),
                                               ('🇬🇧 English', 'lang:en', 'primary')]], home=False))


async def ensure_channel(bot, chat, user_id, check=False):
    config = gate_required(user_id)
    if not config:
        return True
    gate = ChannelGate(store)
    result = await gate.verify(bot, user_id, config) if check else None
    if result == 'passed':
        return True
    if result == 'changed':
        config = gate.required(user_id)
        if not config:
            return True
    notices = {'missing': tr('Подписка пока не найдена. Подпишись и нажми «Проверить подписку».'),
               'wait': tr('Подожди немного и повтори проверку.'),
               'unavailable': tr('Не удалось проверить подписку. Попробуй позже или напиши в поддержку.')}
    text = tr('📢 <b>Подпишись на канал магазина</b>')+'\n\n'+escape(config['title'])+'\n\n' + \
        tr('Чтобы открыть {0}, подпишись на наш канал и нажми кнопку проверки.', escape(shop_name()))
    if result in notices:
        text += '\n\n'+notices[result]
    await brand_screen(bot, chat, text, section='channel', parse_mode='HTML', reply_markup=keyboard([
        [{'text': tr('📢 Подписаться на канал'), 'url': config['join_url'], 'style': 'primary'}],
        [(tr('✅ Проверить подписку'), 'channel:check', 'success')],
        [(tr('🌐 Язык'), 'language'), (tr('💬 Поддержка'), 'support')]], home=False))
    return False


COMMANDS = [('start', 'Магазин'), ('language', 'Язык'), ('promo', 'Активировать промокод'),
            ('paysupport', 'Помощь с оплатой'), ('terms', 'Условия покупки'), ('cancel', 'Отменить ввод')]


async def register_commands(bot):
    for code in ('', 'ru', 'en'):
        await bot.set_my_commands([
            {'command': command, 'description': tr(description, lang=code or 'ru')}
            for command, description in COMMANDS], language_code=code)


# ---------------------------------------------------------------- update handling

class Callback:
    """Answers a callback query exactly once (silently, or with an alert)."""
    def __init__(self, bot, query):
        self.bot = bot
        self.query = query
        self.answered = False

    async def answer(self, text=None, alert=False):
        if self.answered or not self.query:
            return
        self.answered = True
        try:
            if text:
                await self.bot.answer_callback_query(self.query.id, text=text[:200], show_alert=alert)
            else:
                await self.bot.answer_callback_query(self.query.id)
        except TelegramAPIError as exc:
            # Old callback acknowledgements expire; that never blocks the update.
            log.debug('Callback answer skipped: %s', type(exc).__name__)


async def handle(bot, u):
    """Process one update. Never raises: a broken update must not stall polling."""
    callback = Callback(bot, u.callback_query) if u.callback_query else None
    token = language.set('ru')
    try:
        source = u.message or u.callback_query or u.pre_checkout_query
        user = getattr(source, 'from_user', None)
        language.set((store.language(user.id) if user else '') or 'ru')
        await _handle(bot, u, callback)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if is_gone(exc):
            log.info('Update %s: chat unavailable (%s)', u.update_id, type(exc).__name__)
        elif isinstance(exc, TelegramAPIError):
            log.warning('Update %s: Telegram error %s: %s', u.update_id, type(exc).__name__, getattr(exc, 'message', ''))
            await apologize(bot, u, callback)
        else:
            log.exception('Update %s failed', u.update_id)
            await apologize(bot, u, callback)
    finally:
        if callback:
            await callback.answer()
        language.reset(token)


async def apologize(bot, u, callback):
    text = tr('😔 Что-то пошло не так. Попробуй ещё раз или открой /start.')
    try:
        if callback and not callback.answered:
            await callback.answer(text, alert=True)
        elif u.pre_checkout_query:
            await bot.answer_pre_checkout_query(u.pre_checkout_query.id, ok=False,
                                                error_message=tr('Не удалось проверить заказ. Попробуй ещё раз.'))
        elif u.message and u.message.chat.type == 'private' and not u.message.successful_payment:
            await bot.send_message(u.message.chat.id, text)
    except Exception as exc:
        log.debug('Apology not delivered: %s', type(exc).__name__)


async def _handle(bot, u, callback=None):
    # Record private visitors before any Telegram/network operation can fail.
    visitor = None
    if u.message and u.message.chat.type == 'private':
        visitor = u.message.from_user
    elif u.callback_query and u.callback_query.message and u.callback_query.message.chat.type == 'private':
        visitor = u.callback_query.from_user
    elif u.pre_checkout_query:
        visitor = u.pre_checkout_query.from_user
    if visitor and not visitor.is_bot:
        await remember_visitor(visitor)
        if await banned_stop(bot, u, visitor.id):
            return
    if u.pre_checkout_query:
        await pre_checkout(bot, u.pre_checkout_query)
    elif u.message:
        if u.message.chat.type != 'private' or not u.message.from_user:
            return
        if u.message.successful_payment:
            await successful_payment(bot, u, u.message)
        else:
            await on_message(bot, u, u.message)
    elif u.callback_query:
        await on_callback(bot, u.callback_query, callback or Callback(bot, u.callback_query))


# ---------------------------------------------------------------- customers banned in the panel

BAN_NOTICE_SECONDS = 60
SUPPORT_COMMANDS = ('/support', '/paysupport')
SUPPORT_ACTIONS = ('support', 'write')
_ban_notices = {}


def is_banned(user_id):
    """store.is_banned (cached ≤ 5 s per process); a failing check never locks buyers out."""
    try:
        return store.is_banned(user_id)
    except Exception as exc:
        log.warning('Ban check failed: %s', type(exc).__name__)
        return False


async def banned_stop(bot, u, user_id):
    """True when the update must be ignored: the customer is banned. Payments already in flight
    (pre-checkout, successful_payment) and the way to support are never blocked. The buyer gets
    one short notice per minute at most."""
    if u.pre_checkout_query or (u.message and u.message.successful_payment) or not is_banned(user_id):
        return False
    if u.message:
        command = command_of((u.message.text or '').strip())
        if command in SUPPORT_COMMANDS or (not command and store.input_mode(user_id) == 'support'):
            return False
        chat = u.message.chat.id
    else:
        if (u.callback_query.data or '').split(':', 1)[0] in SUPPORT_ACTIONS:
            return False
        chat = u.callback_query.message.chat.id
    now = time.monotonic()
    key = (store.path, user_id)
    if now - _ban_notices.get(key, -BAN_NOTICE_SECONDS) >= BAN_NOTICE_SECONDS:
        _bounded(_ban_notices)
        _ban_notices[key] = now
        try:
            await bot.send_message(chat, tr('Доступ к магазину ограничен. Если это ошибка — напиши в поддержку.'))
        except TelegramAPIError as exc:
            log.debug('Ban notice not sent: %s', type(exc).__name__)
    return True


async def pre_checkout(bot, q):
    try:
        approve = store.approve_topup if q.invoice_payload.startswith('topup:') else store.approve
        await offload(approve, q.invoice_payload, q.from_user.id, q.total_amount, q.currency)
        answer = {'ok': True}
    except ValueError as exc:
        answer = {'ok': False, 'error_message': error_text(str(exc))}
    try:
        await bot.answer_pre_checkout_query(q.id, **answer)
    except TelegramAPIError as exc:
        # Telegram cancels the payment itself when the answer is late.
        log.warning('Pre-checkout %s not answered: %s', q.id, getattr(exc, 'message', type(exc).__name__))


def pending_payments_path():
    return Path(store.path).resolve().with_name('pending-payments.jsonl')


def save_pending_payment(record):
    """Durably remember a charge the database could not record; retried by the service worker."""
    path = pending_payments_path()
    with open(path, 'a', encoding='utf-8') as file:
        file.write(json.dumps(record, ensure_ascii=False)+'\n')
        file.flush()
        os.fsync(file.fileno())


def apply_payment(record):
    """Apply a Telegram Stars charge. Returns True when it was newly credited/paid."""
    if record['payload'].startswith('topup:'):
        return store.credit_topup(record['payload'], record['user_id'], record['amount'], record['currency'], record['charge'])
    store.paid(record['payload'], record['user_id'], record['amount'], record['currency'], record['charge'])
    return False


async def retry_pending_payments(bot):
    path = pending_payments_path()
    if not path.exists():
        return
    remaining = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            log.critical('Unreadable pending payment line kept in %s', path.name)
            remaining.append(line)
            continue
        try:
            credited = await offload(apply_payment, record)
        except ValueError:
            credited = False  # the store recorded a payment issue for the panel
        except Exception as exc:
            log.error('Pending payment %s still not recorded: %s', record.get('charge', '')[:12], type(exc).__name__)
            remaining.append(line)
            continue
        log.warning('Pending payment %s recorded after retry', record.get('charge', '')[:12])
        if credited:
            token = language.set(store.language(record['user_id']) or 'ru')
            try:
                await bot.send_message(record['chat_id'], tr('✅ Зачислено {0}.\nБаланс: {1}',
                                       store.money(store.topup_info(record['payload'])['credit_cents']),
                                       store.money(store.wallet(record['user_id'])['balance'])),
                                       reply_markup=keyboard(wallet_rows()))
            except Exception as exc:
                log.info('Top-up notice not sent: %s', type(exc).__name__)
            finally:
                language.reset(token)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(''.join(item+'\n' for item in remaining), encoding='utf-8')
    os.replace(temporary, path)
    if not remaining:
        path.unlink(missing_ok=True)


async def successful_payment(bot, u, m):
    p = m.successful_payment
    payload = p.invoice_payload
    reference = payload.removeprefix('topup:')[:8]
    user_id = m.from_user.id
    record = {'payload': payload, 'user_id': user_id, 'amount': p.total_amount, 'currency': p.currency,
              'charge': p.telegram_payment_charge_id, 'chat_id': m.chat.id, 'update_id': u.update_id}
    for attempt in range(3):
        try:
            if await offload(apply_payment, record):
                await bot.send_message(m.chat.id, tr('✅ Зачислено {0}.\nБаланс: {1}', store.money(store.topup_info(payload)['credit_cents']),
                                                     store.money(store.wallet(user_id)['balance'])),
                                       reply_markup=keyboard(wallet_rows()))
            return
        except ValueError as exc:
            # The store recorded the incident; the buyer must know the money is not lost.
            log.error('Payment issue update=%s ref=%s charge=%s: %s', u.update_id, reference,
                      p.telegram_payment_charge_id, exc)
            break
        except TelegramAPIError:
            raise
        except Exception:
            log.exception('Payment not recorded yet update=%s ref=%s charge=%s (attempt %s)',
                          u.update_id, reference, p.telegram_payment_charge_id, attempt+1)
            if attempt == 2:
                try:
                    save_pending_payment(record)
                    log.critical('Payment saved to %s for automatic retry: update=%s charge=%s',
                                 pending_payments_path().name, u.update_id, p.telegram_payment_charge_id)
                except Exception:
                    log.critical('PAYMENT NOT RECORDED: update=%s user=%s payload=%s amount=%s %s charge=%s',
                                 u.update_id, user_id, payload, p.total_amount, p.currency, p.telegram_payment_charge_id)
                break
            await asyncio.sleep(1+attempt)
    await bot.send_message(m.chat.id, tr('✅ Оплата получена, но при её обработке возникла заминка.\nНе переживай: платёж сохранён, поддержка проверит его и всё исправит. Номер для поддержки: <code>{0}</code>', escape(reference)),
                           parse_mode='HTML', reply_markup=keyboard([[(tr('💬 Поддержка'), 'support', 'primary')]]))


def command_of(text):
    if not text.startswith('/'):
        return ''
    return text.split(maxsplit=1)[0].split('@')[0].lower()


async def on_message(bot, u, m):
    user_id = m.from_user.id
    chat = m.chat.id
    text = (m.text or '').strip()
    command = command_of(text)
    mode = store.input_mode(user_id)
    if command and mode:
        await set_mode(user_id, '')
        mode = ''
    if mode == 'support':
        # Support messages (text or a screenshot with caption) go straight to the inbox.
        await submit_support(bot, m)
        return
    if command == '/language' or (command == '/start' and not store.language(user_id)):
        await language_menu(bot, chat)
        return
    payment_text = bool(HASH_SHAPE.fullmatch(text)) and (mode == 'txid' or bool(txid_context(user_id, text)))
    exempt = command in ('/support', '/paysupport', '/terms', '/details', '/cancel') or payment_text or mode.startswith('details:')
    if not exempt and not await ensure_channel(bot, chat, user_id, check=command == '/start'):
        return
    attempt_ref = 'telegram:'+str(user_id)+':'+str(u.update_id)
    if command:
        await on_command(bot, m, command, text, attempt_ref)
    elif mode == 'txid':
        await submit_txid(bot, chat, user_id, text, attempt_ref)
    elif mode.startswith('qty:'):
        await quantity_input(bot, m, mode, text)
    elif mode.startswith('details:'):
        await submit_details(bot, chat, user_id, mode.split(':', 1)[1], text)
    elif mode in NETWORKS or mode == 'stars':
        await amount_input(bot, chat, user_id, mode, text)
    elif mode == 'promo' and text:
        await promo_input(bot, chat, user_id, text, attempt_ref)
    elif text and txid_context(user_id, text):
        await submit_txid(bot, chat, user_id, text, attempt_ref)
    elif emoji_ids(m) and is_admin(user_id):
        await send_emoji_ids(bot, chat, emoji_ids(m))
    else:
        await menu(bot, chat, m.from_user.first_name or '', m.from_user.username or '')


def emoji_ids(*messages):
    found = {}
    for source in messages:
        if not source:
            continue
        for entity in (source.entities or [])+(source.caption_entities or []):
            if entity.type == 'custom_emoji' and entity.custom_emoji_id:
                found[entity.custom_emoji_id] = None
        if source.sticker and source.sticker.custom_emoji_id:
            found[source.sticker.custom_emoji_id] = None
    return list(found)


async def send_emoji_ids(bot, chat, ids):
    await bot.send_message(chat, '\n'.join(tr('ID эмодзи: <code>{0}</code>', escape(item)) for item in ids[:30]), parse_mode='HTML')


async def on_command(bot, m, command, text, attempt_ref=''):
    user_id = m.from_user.id
    chat = m.chat.id
    argument = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ''
    if command == '/emoji' and is_admin(user_id):
        ids = emoji_ids(m, m.reply_to_message)
        if ids:
            await send_emoji_ids(bot, chat, ids)
        else:
            await bot.send_message(chat, tr('Отправь кастомный эмодзи из панели эмодзи Telegram или ответь на сообщение с ним командой /emoji. У обычных эмодзи и стикеров такого ID нет.'))
    elif command == '/topup':
        try:
            if not re.fullmatch(r'[0-9]{1,5}', argument):
                raise ValueError(tr('Укажи целую сумму от 1 до 10 000 Stars. Например: /topup 250'))
            await topup_invoice(bot, chat, user_id, int(argument))
        except ValueError as exc:
            await bot.send_message(chat, error_text(str(exc)), reply_markup=keyboard(wallet_rows()))
    elif command == '/usdt' and argument:
        try:
            await crypto_invoice(bot, chat, user_id, usd_cents(argument.replace(',', '.').removeprefix('$'), positive=True))
        except ValueError as exc:
            await bot.send_message(chat, error_text(str(exc)), reply_markup=keyboard(wallet_rows()))
    elif command == '/wallet':
        await wallet_menu(bot, chat, user_id)
    elif command == '/promo':
        if argument:
            await promo_input(bot, chat, user_id, argument, attempt_ref)
        else:
            await promo_prompt(bot, chat, user_id)
    elif command == '/details':
        parts = text.split(maxsplit=2)
        if len(parts) != 3 or not re.fullmatch(ORDER_ID, parts[1].lower()):
            await bot.send_message(chat, tr('Формат: /details номер_заказа данные для выполнения\nПроще — открой «Мои покупки» и нажми «Отправить данные».'),
                                   reply_markup=keyboard([[(tr('📦 Мои покупки'), 'orders', 'primary')]]))
        else:
            await submit_details(bot, chat, user_id, parts[1].lower(), parts[2])
    elif command in ('/support', '/paysupport'):
        await support_screen(bot, chat)
    elif command == '/terms':
        await terms_screen(bot, chat)
    else:
        # /start, /cancel and anything unknown return to the main menu.
        await menu(bot, chat, m.from_user.first_name or '', m.from_user.username or '')


async def quantity_input(bot, m, mode, text):
    user_id = m.from_user.id
    parts = mode.split(':')
    if len(parts) != 4 or not all(part.isdigit() for part in parts[1:]):
        await set_mode(user_id, '')
        await menu(bot, m.chat.id, m.from_user.first_name or '', m.from_user.username or '')
        return
    _, pid, message_id, previous = parts
    navigation = Navigation(bot, m.model_copy(update={'message_id': int(message_id), 'photo': None}))
    try:
        if not re.fullmatch(r'[0-9]{1,3}', text):
            raise ValueError(tr('Количество: целое число от 1 до 100.'))
        product = get_product(int(pid))
        quantity = int(text)
        validate_quantity(product, quantity)
        await product_card(navigation, m.chat.id, product, quantity)
        await set_mode(user_id, '')
    except ValueError as exc:
        prompt = await navigation.send_message(m.chat.id, error_text(str(exc))+'\n'+tr('Введи другое количество или /cancel.'),
                                               reply_markup=keyboard([[(tr('← Назад'), 'product:'+pid+':'+previous)]], home=False))
        if getattr(prompt, 'message_id', None):
            await set_mode(user_id, f'qty:{pid}:{prompt.message_id}:{previous}')


async def amount_input(bot, chat, user_id, mode, text):
    try:
        if not text:
            raise ValueError(tr('Сумма должна быть больше нуля'))
        if mode == 'stars':
            await stars_amount_invoice(bot, chat, user_id, text)
        else:
            await crypto_invoice(bot, chat, user_id, usd_cents(text.replace(',', '.').removeprefix('$'), positive=True), network=mode)
        await set_mode(user_id, '')
    except ValueError as exc:
        await bot.send_message(chat, error_text(str(exc))+tr('\nВведи другую сумму или /cancel.'),
                               reply_markup=keyboard([[(tr('← Способы оплаты'), 'deposit', 'primary')]]))


async def support_screen(bot, chat):
    rows = [[(tr('✍️ Написать в поддержку'), 'write', 'success')]]
    await info_screen(bot, chat, tr('💬 Поддержка'), os.getenv('SUPPORT_CONTACT', ''), section='support', rows=rows,
                      footer=tr('Если вопрос по заказу — укажи его номер, так мы поможем быстрее.'))


async def terms_screen(bot, chat):
    await info_screen(bot, chat, tr('📋 Условия покупки'), os.getenv('SHOP_TERMS', ''), section='terms',
                      rows=[[(tr('← О магазине'), 'about')]])


GATE_FREE = {'language', 'lang', 'support', 'write', 'terms', 'orders', 'purchase', 'order', 'get', 'details', 'news',
             'channel'}
# Actions that set an input mode themselves (every other button leaves the current mode).
MODE_ACTIONS = ('qtyinput', 'details', 'custom', 'bscpaid', 'write', 'promo')


async def on_callback(bot, q, callback):
    if not q.message or q.message.chat.type != 'private':
        return
    try:
        action, args = parse_callback(q.data or '')
    except StaleButton as exc:
        await callback.answer(str(exc), alert=True)
        return
    if action == 'noop':
        return
    user = q.from_user
    chat = q.message.chat.id
    if action not in MODE_ACTIONS:
        await set_mode(user.id, '')
    nav = Navigation(bot, q.message)
    if action == 'channel':
        await callback.answer()
        if not store.language(user.id):
            await language_menu(nav, chat)
        elif await ensure_channel(nav, chat, user.id, check=True):
            await menu(nav, chat, user.first_name, user.username)
        return
    if action not in GATE_FREE and not await ensure_channel(nav, chat, user.id):
        return
    try:
        await route(nav, chat, user, q, action, args, callback)
    except ValueError as exc:
        text = error_text(str(exc)) or tr('Открой каталог заново.')
        if not callback.answered and utf16_len(text) <= 190:
            await callback.answer(text, alert=True)
        else:
            await transport(nav).send_message(chat, text, reply_markup=keyboard([]))


async def route(bot, chat, user, q, action, args, callback):
    uid = user.id
    if action == 'language':
        await language_menu(bot, chat)
    elif action == 'lang':
        await offload(store.set_language, uid, args[0])
        language.set(args[0])
        await callback.answer()
        if await ensure_channel(bot, chat, uid, check=True):
            await menu(bot, chat, user.first_name, user.username)
    elif action == 'home':
        await menu(bot, chat, user.first_name, user.username)
    elif action == 'wallet':
        await wallet_menu(bot, chat, uid)
    elif action == 'deposit':
        await deposit_menu(bot, chat)
    elif action == 'net':
        await network_menu(bot, chat, args[0])
    elif action == 'netamount':
        await crypto_invoice(bot, chat, uid, int(args[1]), network=args[0])
    elif action == 'custom':
        await custom_amount_prompt(bot, chat, uid, args[0])
    elif action == 'stars':
        await stars_menu(bot, chat)
    elif action == 'usdtopup':
        rate = stars_rate()
        if not rate:
            raise StaleButton()
        cents = int(args[0])
        if cents < 1:
            raise ValueError(tr('Сумма должна быть больше нуля'))
        await topup_invoice(bot, chat, uid, cents_to_stars(cents, rate))
    elif action == 'topup':
        await topup_invoice(bot, chat, uid, int(args[0]))
    elif action == 'bscpaid':
        await txid_prompt(bot, chat, uid, args[0])
    elif action == 'catalog':
        await catalog_screen(bot, chat, int(args[0] or 0))
    elif action == 'cat':
        await category_screen(bot, chat, int(args[0]), int(args[1] or 0))
    elif action == 'pback':
        await product_back(bot, chat, int(args[0]))
    elif action == 'product':
        pid, quantity = selection(*args)
        await product_card(bot, chat, get_product(pid), quantity)
    elif action == 'qtyinput':
        pid, quantity = selection(*args)
        product = get_product(pid)
        if not quantity_limit(product):
            raise ValueError(tr('Товар закончился'))
        prompt = await bot.send_message(chat, tr('Введи количество от 1 до {0}.', quantity_limit(product)),
                                        reply_markup=keyboard([[(tr('← Назад'), 'product:'+str(pid)+':'+str(quantity))]], home=False))
        message_id = getattr(prompt, 'message_id', q.message.message_id)
        await set_mode(uid, f'qty:{pid}:{message_id}:{quantity}')
    elif action == 'checkout':
        pid, quantity = selection(*args)
        await checkout_screen(bot, chat, uid, pid, quantity)
    elif action == 'promo':
        await promo_prompt(bot, chat, uid)
    elif action == 'again':
        await buy_again(bot, chat, uid, args[0])
    elif action == 'write':
        await support_prompt(bot, chat, uid)
    elif action == 'netbuy':
        network = args[0]
        pid, quantity = selection(*args[1:])
        require_network(network)
        order = await new_order(bot, chat, pid, uid, quantity)
        await crypto_invoice(bot, chat, uid, order['price'], order_id=order['id'], network=network)
    elif action == 'wbuy':
        pid, quantity = selection(*args)
        order = await new_order(bot, chat, pid, uid, quantity)
        methods = available_methods()
        rows = [[(tr('✅ Подтвердить оплату'), 'wpay:'+order['id'], 'success')]]
        if methods['stars'] or methods['networks']:
            rows.append([(tr('➕ Пополнить'), 'deposit', 'primary')])
        rows.append([(tr('← Назад'), 'checkout:'+str(pid)+':'+str(quantity))])
        balance = menu_summary(uid)[0]
        text = tr('👛 <b>Оплата с баланса</b>\n\n{0}\nК оплате: <b>{1}</b>\nТвой баланс: {2}\n\nРезерв товара действует 5 минут.',
                  escape(order['title'])+' × '+str(quantity), store.money(order['price']), store.money(balance))
        if balance < order['price']:
            text += '\n\n'+tr('⚠️ Не хватает {0} — пополни баланс или выбери другой способ.', store.money(order['price']-balance))
        await bot.send_message(chat, text,
                               parse_mode='HTML', reply_markup=keyboard(rows))
    elif action == 'wpay':
        await offload(store.pay_wallet, args[0], uid)
        await bot.send_message(chat, tr('✅ Заказ оплачен с баланса. Результат придёт отдельным сообщением.'),
                               reply_markup=keyboard([[(tr('📦 Мои покупки'), 'orders', 'primary')]]))
    elif action == 'buy':
        require_stars()
        pid, quantity = selection(*args)
        order = await new_order(bot, chat, pid, uid, quantity)
        await bot.send_invoice(chat, title=order['title'][:32],
                               description=tr('Заказ ')+order['id'][:8]+' · '+tr('{0} шт.', quantity)+tr('. Условия: /terms. Поддержка: /paysupport.'),
                               payload=order['id'], currency='XTR', provider_token='',
                               prices=[{'label': order['title'][:32], 'amount': order['payment_stars']}])
    elif action == 'profile':
        orders = store.orders(uid)
        paid = [o for o in orders if o['status'] in PAID_STATES]
        await brand_screen(bot, chat,
                           tr('👤 <b>Твой профиль</b>\n\nTelegram ID: <code>{0}</code>\n\nОплаченных заказов: <b>{1}</b>\nВыполнено: <b>{2}</b>\nПокупки на сумму: <b>{3}</b>',
                              uid, len(paid), sum(o['status'] == 'fulfilled' for o in paid), store.money(sum(o['price'] for o in paid))),
                           parse_mode='HTML', section='profile',
                           reply_markup=keyboard([[(tr('📦 Мои покупки'), 'orders', 'success'), (tr('👛 Кошелёк'), 'wallet', 'primary')],
                                                  [(tr('← О магазине'), 'about')]]))
    elif action == 'guide':
        await brand_screen(bot, chat,
                           tr('📖 Как сделать покупку\n\n1️⃣ Открой каталог и выбери товар.\n2️⃣ Прочитай описание и выбери количество.\n3️⃣ Оплати с баланса магазина, в Telegram Stars или USDT.\n4️⃣ Товар из наличия придёт сразу. Для услуг бот попросит данные для менеджера.\n\n📦 Покупки всегда доступны в «Моих покупках».\n💬 Если нужна помощь — поддержка рядом.'),
                           section='guide', reply_markup=keyboard([[(tr('🛒 Перейти в каталог'), 'catalog', 'success'), (tr('💬 Помощь'), 'support', 'primary')],
                                                                   [(tr('← О магазине'), 'about')]]))
    elif action == 'terms':
        await terms_screen(bot, chat)
    elif action == 'news':
        enabled = Broadcasts(store).preferences(uid, None if not args[0] else args[0] == 'on')
        await brand_screen(bot, chat, tr('🔔 Новости магазина включены.\n\nБудем сообщать о новых товарах и пополнении стока.') if enabled
                           else tr('🔕 Новости магазина отключены.\n\nСообщения об оплате и покупках продолжат приходить.'),
                           section='news', reply_markup=keyboard(([[(tr('🔕 Отключить новости'), 'news:off')]] if enabled
                                                                  else [[(tr('🔔 Включить новости'), 'news:on', 'primary')]])
                                                                 + [[(tr('← О магазине'), 'about')]]))
    elif action == 'about':
        await brand_screen(bot, chat,
                           '<b>'+escape(shop_name())+'</b>'+tr('\n\nПодписки, аккаунты и цифровые товары.\n\nТовары из наличия выдаются после оплаты. Сроки услуг и инструкции указаны в карточке товара.\n\nЗдесь — информация о магазине и твоём профиле.'),
                           parse_mode='HTML', section='about',
                           reply_markup=keyboard([[(tr('📖 Как купить'), 'guide', 'primary'), (tr('👤 Профиль'), 'profile', 'primary')],
                                                  [(tr('📋 Условия покупки'), 'terms'), (tr('💬 Поддержка'), 'support')],
                                                  [(tr('🔔 Уведомления'), 'news')]]))
    elif action == 'orders':
        await purchases_menu(bot, chat, uid, int(args[0] or 0))
    elif action == 'purchase':
        await purchase_card(bot, chat, uid, args[0], int(args[1] or 0))
    elif action == 'order':
        await order_card(bot, chat, uid, args[0])
    elif action == 'details':
        await details_prompt(bot, chat, uid, args[0])
    elif action == 'get':
        body = store.purchase_delivery(uid, args[0])
        await callback.answer()
        await send_result(transport(bot), chat, body)
    elif action == 'support':
        await support_screen(bot, chat)


# ---------------------------------------------------------------- background services

async def deliver(bot):
    for item in store.deliveries():
        token = language.set(store.language(item['user_id']) or 'ru')
        try:
            order = order_row(item['order_id']) if item.get('order_id') else None
            rows = details_rows(order['id']) if order and order['status'] == 'awaiting_details' else []
            await send_result(bot, item['user_id'], store.decrypt(item['body']), rows)
            await offload(store.delivery_result, item['id'], True)
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter:
            await offload(store.delivery_result, item['id'], False)
            return
        except Exception as exc:
            # Do not log exception text: Telegram errors may contain request content.
            permanent = is_gone(exc)
            log.warning('Delivery failed: outbox_id=%s (%s%s)', item['id'], type(exc).__name__, ', permanent' if permanent else '')
            await offload(report, store.delivery_result, item['id'], False, permanent=permanent)
        finally:
            language.reset(token)


ADMIN_ALERTS = {'paused_until': 0.0}
ERROR_ALERT_CURSOR = 'admin_alert_cursor'
# Events that already reach staff as their own notification kind (when that kind is on).
ERROR_ALERTS_COVERED = {'payment.issue': 'payment_issue', 'delivery.review': 'new_paid_order'}


def queue_error_alerts(limit=20):
    """Turn new warning/error audit events into 'errors' staff notifications; notify.py
    applies the chats (ADMIN_CHAT_ID by default), the toggle, dedupe and the hourly limit.
    The first run only sets the cursor, so old history is never announced."""
    with store.read() as db:
        cursor = db.execute('SELECT value FROM meta WHERE key=?', (ERROR_ALERT_CURSOR,)).fetchone()
        events = [] if cursor is None else [dict(row) for row in db.execute(
            "SELECT * FROM audit WHERE level IN ('warning','error') AND id>? ORDER BY id LIMIT ?", (int(cursor[0]), limit))]
    if cursor is None:
        with store.tx(invalidate=False) as db:
            latest = db.execute('SELECT coalesce(max(id),0) FROM audit').fetchone()[0]
            db.execute('INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)', (ERROR_ALERT_CURSOR, str(latest)))
        return 0
    if not events:
        return 0
    queue = notify.Notify(store)
    queued = 0
    with store.tx(invalidate=False) as db:
        # Another process may have announced these already: only the one that moves the cursor emits.
        if not db.execute('UPDATE meta SET value=? WHERE key=? AND value=?',
                          (str(events[-1]['id']), ERROR_ALERT_CURSOR, cursor[0])).rowcount:
            return 0
        settings = queue.settings(db)
        for event in events:
            if settings.get(ERROR_ALERTS_COVERED.get(event['action'], ''), False):
                continue
            text = ('⚠️ '+(event['summary'] or event['action']) +
                    ('\nЗаказ: '+event['order_id'][:8] if event['order_id'] else '')+'\nПодробности — в разделе «Ошибки» панели.')
            queued += queue.emit(db, 'errors', text, key=event['action']+':'+str(event['target']))
    return queued


async def send_staff_notifications(bot):
    """Send the staff notification queue (new orders, support, low stock, daily summary)."""
    if time.monotonic() < ADMIN_ALERTS['paused_until']:
        return
    queue = notify.Notify(store)
    for item in await offload(queue.due, 20):
        try:
            await bot.send_message(item['chat_id'], item['text'], parse_mode=None)
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            ADMIN_ALERTS['paused_until'] = time.monotonic()+exc.retry_after
            await offload(queue.result, item['id'], False)
            return
        except Exception as exc:
            permanent = is_gone(exc)
            log.warning('Staff notification %s not delivered (%s%s)', item['id'], type(exc).__name__,
                        ', permanent' if permanent else '')
            await offload(report, queue.result, item['id'], False, permanent=permanent)
            continue
        await offload(queue.result, item['id'], True)


async def service_worker(bot, interval=2):
    """Deliveries, admin alerts and housekeeping; each job fails independently."""
    last_housekeeping = 0
    last_error = {}

    async def run(name, job):
        try:
            result = job()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if time.monotonic()-last_error.get(name, -3600) > 60:
                log.error('%s failed: %s', name, type(exc).__name__)
                last_error[name] = time.monotonic()
    while True:
        await run('delivery', lambda: deliver(bot))
        await run('support replies', lambda: deliver_support_replies(bot))
        await run('error alerts', lambda: offload(queue_error_alerts))
        await run('staff notifications', lambda: send_staff_notifications(bot))
        if time.monotonic()-last_housekeeping >= 30:
            last_housekeeping = time.monotonic()
            await run('notify tick', lambda: offload(notify.Notify(store).tick, time.time()))
            await run('housekeeping', lambda: offload(store.housekeeping))
            await run('pending payments', lambda: retry_pending_payments(bot))
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- polling

def update_user(u):
    source = u.message or u.callback_query or u.pre_checkout_query
    user = getattr(source, 'from_user', None)
    return user.id if user else None


class Dispatcher:
    """Runs a batch concurrently: per-user order is kept, global concurrency is bounded."""
    def __init__(self, bot, limit=MAX_CONCURRENCY):
        self.bot = bot
        self.slots = asyncio.Semaphore(limit)
        self.locks = {}
        self.users = {}

    async def run(self, u):
        key = update_user(u)
        lock = self.locks.setdefault(key, asyncio.Lock())
        self.users[key] = self.users.get(key, 0)+1
        # Replies to a customer's own update go before deliveries and broadcasts.
        ratelimit.priority.set(ratelimit.INTERACTIVE)
        try:
            async with lock, self.slots:
                try:
                    await asyncio.wait_for(handle(self.bot, u), UPDATE_TIMEOUT)
                except asyncio.TimeoutError:
                    log.error('Update %s timed out and was skipped', u.update_id)
        finally:
            self.users[key] -= 1
            if not self.users[key]:
                del self.users[key]
                del self.locks[key]

    async def process(self, updates):
        # Tasks start in update order, so each user's lock is taken in that order too.
        await asyncio.gather(*(asyncio.create_task(self.run(u)) for u in updates))


async def poll(bot, me):
    dispatcher = Dispatcher(bot)
    offline = False
    offset = store.offset()
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=POLL_TIMEOUT, request_timeout=POLL_TIMEOUT+15,
                                            allowed_updates=['message', 'callback_query', 'pre_checkout_query'])
        except (TelegramConflictError, TelegramUnauthorizedError):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not offline:
                try:
                    with store.tx() as db:
                        store.log(db, 'bot', 'bot.offline', 'telegram', level='error')
                except Exception:
                    pass
                log.warning('Telegram unavailable: %s', type(exc).__name__)
            print('BOT_OFFLINE '+type(exc).__name__+tr(': проверьте сеть; событие будет обработано повторно.'), flush=True)
            offline = True
            await asyncio.sleep(3)
            continue
        if offline:
            print('BOT_READY @'+me.username, flush=True)
            try:
                with store.tx() as db:
                    store.log(db, 'bot', 'bot.online', 'telegram')
            except Exception:
                pass
            offline = False
        if not updates:
            continue
        await dispatcher.process(updates)
        offset = max(u.update_id for u in updates)+1
        try:
            await offload(store.offset, offset)
        except Exception as exc:
            # The in-memory offset still moves on; only a restart could replay this batch.
            log.error('Offset not saved: %s', type(exc).__name__)


def configure_logging():
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
        return
    for handler in root.handlers:
        if handler.formatter is None or '%(asctime)' not in (handler.formatter._fmt or ''):
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))


def install_shutdown(task):
    """SIGTERM/SIGINT cancel the main task so every finally-block runs."""
    loop = asyncio.get_running_loop()
    state = {'stopping': False}

    def stop(*_):
        state['stopping'] = True
        loop.call_soon_threadsafe(task.cancel)
    for name in ('SIGTERM', 'SIGINT'):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(sig, stop)
            except (ValueError, OSError):
                pass
    return state


async def main():
    configure_logging()
    for name in ('BOT_TOKEN', 'SUPPORT_CONTACT', 'SHOP_TERMS'):
        if not os.getenv(name):
            raise RuntimeError('Set '+name+' before starting the bot')
    instance = InstanceLock(Path(store.path).resolve().with_suffix('.bot.lock')).acquire()
    bot = None
    tasks = []
    shutdown = install_shutdown(asyncio.current_task())
    try:
        # Every send/edit waits for the shared outbound limiter (see ratelimit.py).
        bot = ratelimit.RateLimitedBot(Bot(os.environ['BOT_TOKEN']))
        me = await bot.get_me()
        await bot.delete_webhook(drop_pending_updates=False)
        try:
            await register_commands(bot)
        except TelegramAPIError as exc:
            log.warning('Commands not registered: %s', type(exc).__name__)
        print('BOT_READY @'+me.username, flush=True)
        tasks = [asyncio.create_task(crypto_worker(bot)), asyncio.create_task(service_worker(bot)),
                 asyncio.create_task(broadcast_worker(bot.bulk(), store))]
        await poll(bot, me)
    except asyncio.CancelledError:
        if not shutdown['stopping']:
            raise
        log.info('Shutdown requested')
    finally:
        pending = tasks+list(background_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if bot:
            await bot.session.close()
        instance.release()


if __name__ == '__main__':
    asyncio.run(main())
