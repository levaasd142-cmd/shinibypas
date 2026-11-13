"""Transactional shop core. SQLite WAL: one host, multiple local processes."""
import copy
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
import weakref
from contextlib import contextmanager, closing, nullcontext
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import emoji_text
from money import rate_value, stars_to_cents, credit_cents, cents_to_stars, format_usd
from i18n import tr

log_ = logging.getLogger('themarket.store')

# Bumped when a one-off data migration is added; every step is also idempotent.
#   1: crypto_deposits without UNIQUE(amount)/UNIQUE(txid), credited_transfers backfill done
#   2: stock fingerprints are keyed HMAC ('h1:' prefix)
#   3: BSC receiving wallet is stored explicitly for databases that already used it
#   4: customer registry backfilled from activity, cached wallet balances (wallet_balances)
#   5: 3.1 panel features — customer CRM (tags, notes, bans), order notes, support templates,
#      live activity feed (live_events filled by triggers) and per-staff "seen" markers
SCHEMA_VERSION = 5

RESERVATION_SECONDS = 5*60
CHECKOUT_SECONDS = 60*60           # approved Stars checkout keeps its reservation this long
CRYPTO_INVOICE_SECONDS = 15*60
AMOUNT_REUSE_SECONDS = 48*3600     # an invoice amount tail is free again this long after expiry
TOPUP_SECONDS = 60*60
TOPUP_OPEN_LIMIT = 5
RETRY_CAP_SECONDS = 3600
RETRY_GIVE_UP_ATTEMPTS = 29        # 1+2+4+…+32 minutes, then hourly: about 24 hours in total
CACHE_SECONDS = 3
OUTBOX_KEEP_SECONDS = 7*86400
INPUT_KEEP_SECONDS = 7*86400
FINGERPRINT_PREFIX = 'h1:'
# Receiving wallet that older releases used implicitly for BEP20. Only databases that already
# have BSC invoices keep it (migration 3); new shops must configure their own wallet.
LEGACY_BSC_WALLET = '0x4AB3C61e666BA119deA95e0AD78819B3681DF53b'
PENDING_LIMIT_MESSAGE = 'У вас уже есть три неоплаченных заказа. Дождитесь истечения резерва.'
AUDIT_RETENTION_DEFAULT_DAYS = 180
AUDIT_PRUNE_BATCH = 500            # audit rows examined per housekeeping run (bounded lock time)
# Info events of these categories are pruned after the retention period; warnings, errors,
# money movements (payments, balance, finance) and payment acceptance are kept forever.
AUDIT_PRUNABLE_CATEGORIES = ('orders','products','system','settings')
USER_STATE_MAX_VALUE = 1000
USER_STATE_KEEP_SECONDS = 7*86400
WALLET_VERIFY_SECONDS = 86400      # housekeeping re-checks cached balances against the ledger daily
PRODUCT_IMAGE_RE = re.compile(r'product-images/[0-9]{1,12}-[0-9a-f]{8}\.jpg')
BADGE_MAX = 16
LOW_STOCK_THRESHOLD = 3            # fallback when notify.py has no own low-stock check
# Optional modules that own tables in this database (each initialize(db) is idempotent).
OPTIONAL_MODULES = ('promo','support','staff','notify')


def audit_retention_days():
    """AUDIT_RETENTION_DAYS (default 180, minimum 30); 0 disables pruning."""
    try:value = int(os.getenv('AUDIT_RETENTION_DAYS', AUDIT_RETENTION_DEFAULT_DAYS))
    except ValueError:return AUDIT_RETENTION_DEFAULT_DAYS
    return 0 if value<=0 else max(30,value)


def _statements(script):
    """Split an SQL script into complete statements (trigger bodies contain semicolons)."""
    statements, current = [], ''
    for line in script.splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            statements.append(current.strip());current = ''
    return [statement for statement in statements if statement]


def _optional(name):
    """Import an optional sibling module; None only when that module itself is absent."""
    try:
        return __import__(name)
    except ModuleNotFoundError as exc:
        if exc.name==name:return None
        raise


def retry_delay(attempts):
    """Exponential backoff for notifications: 1, 2, 4 … minutes, capped at one hour."""
    return min(60*2**min(max(attempts-1,0),12),RETRY_CAP_SECONDS)


def fingerprint_key(stock_key):
    raw=stock_key.encode() if isinstance(stock_key,str) else stock_key
    return HKDF(algorithm=hashes.SHA256(),length=32,salt=None,info=b'themarket stock fingerprint v1').derive(raw)


def stock_fingerprint(stock_key, item, derived=None):
    """Duplicate-detection fingerprint of a stock line, keyed by STOCK_KEY (not guessable)."""
    key=derived or fingerprint_key(stock_key)
    text=item if isinstance(item,str) else item.decode()
    return FINGERPRINT_PREFIX+hmac.new(key,text.encode(),hashlib.sha256).hexdigest()


class _Connection(sqlite3.Connection):
    """Plain connection that supports weak references."""


def _close_connections(connections):
    for db in list(connections):
        try:db.close()
        except Exception:pass


def _networks():
    from crypto_networks import NETWORKS
    return NETWORKS


def wallet_kind(reference):
    prefix=reference.split(':',1)[0]
    kinds={'topup':'stars','order':'order','admin':'adjust','set':'adjust','refund':'refund','promo':'promo'}
    if prefix in kinds:return kinds[prefix]
    return 'usdt' if prefix in _networks() else 'other'


def _escape_like(text):
    return text.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')


AUDIT_LABELS = {
    'product.save':'Товар сохранён','product.delete':'Товар удалён','stock.import':'Загружен сток',
    'stock.reveal':'Просмотрено содержимое позиции стока',
    'order.created':'Заказ создан · резерв 5 минут','order.expired':'Резерв заказа истёк',
    'order.checkout':'Оплата Stars подтверждена к проведению','payment.accepted':'Оплата получена',
    'payment.issue':'Платёж получен, но не проведён · требуется проверка',
    'payment.issue.resolved':'Проблема с платежом закрыта',
    'order.details':'Получены данные для выполнения','order.claim':'Заказ взят в работу',
    'order.complete':'Заказ выполнен','order.view':'Открыта карточка заказа',
    'order.details.view':'Просмотрены данные заказа','order.resend':'Запрошена повторная выдача',
    'delivery.sent':'Сообщение покупателю доставлено',
    'delivery.retry':'Ошибка доставки · повторим с увеличивающимся интервалом',
    'delivery.failed':'Сообщение покупателю не доставлено · повторы прекращены',
    'delivery.review':'Оплата получена, товар не выдан · требуется проверка',
    'wallet.adjust':'Баланс скорректирован','wallet.set':'Установлен баланс',
    'wallet.credit':'Зачислено пополнение Stars','wallet.credit_usdt':'Зачислено пополнение USDT',
    'crypto.created':'Создан счёт USDT · оплата 15 минут','crypto.pending':'Ожидаются подтверждения USDT',
    'crypto.review':'Платёж USDT требует проверки','crypto.retry':'Запрошена проверка платежа',
    'crypto.invalid_txid':'Неверный TXID · попытка использована','crypto.cancelled':'Пополнение отменено · исчерпаны 5 попыток TXID',
    'crypto.notify_failed':'Не удалось уведомить покупателя о зачислении USDT',
    'crypto.notice_failed':'Не удалось уведомить покупателя о неверном TXID',
    'bot.offline':'Бот потерял связь · подключение будет повторено','bot.online':'Связь с Telegram восстановлена',
    'server.error':'Внутренняя ошибка панели',
    'backup.created':'Создана и проверена резервная копия','backup.verified':'Проверено восстановление резервной копии',
    'backup.failed':'Ошибка автоматического резервного копирования',
    'backup.key.export':'Скачан ключ восстановления','backup.download':'Скачана зашифрованная копия',
    'finance.entry':'Добавлена финансовая операция','finance.void':'Финансовая операция отменена',
    'finance.order_cost':'Исправлена себестоимость заказа','stock.cost':'Задана себестоимость свободного стока',
    'broadcast.settings':'Изменены авторассылки','broadcast.queued':'Авторассылка поставлена в очередь',
    'broadcast.draft':'Подготовлен черновик рассылки','broadcast.edit':'Изменён черновик рассылки','broadcast.start':'Рассылка запущена',
    'broadcast.pause':'Рассылка приостановлена','broadcast.resume':'Рассылка продолжена',
    'broadcast.cancel':'Рассылка отменена','broadcast.completed':'Рассылка завершена',
    'channel.settings':'Изменена обязательная подписка на канал',
    'channel.check.failed':'Не удалось проверить подписку · проверьте права бота в канале',
    'payment.method':'Изменены способы оплаты',
    'currency.rate':'Изменён курс Stars','emoji.pack.import':'Загружен набор эмодзи','emoji.assign':'Назначен эмодзи',
    'product.image':'Изменено фото товара','product.sort':'Изменён порядок товаров',
    'product.move':'Товар перемещён в списке',
    'promo.overused':'Промокод использован сверх лимита · оплата проведена',
    'wallet.repair':'Кэш балансов расходился с журналом · пересчитан',
    'promo.create':'Создан промокод','promo.update':'Изменён промокод','promo.enable':'Промокод включён',
    'promo.disable':'Промокод выключен','promo.redeemed':'Активирован промокод · сумма зачислена на баланс',
    'support.open':'Новое обращение в поддержку','support.reply':'Ответ поддержки','support.close':'Обращение закрыто',
    'support.reopen':'Обращение открыто снова','support.assign':'Назначен ответственный за обращение',
    'support.delivery_failed':'Ответ поддержки не доставлен',
    'staff.create':'Создан сотрудник','staff.update':'Изменён сотрудник',
    'notify.settings':'Изменены уведомления','notify.test':'Отправлено тестовое уведомление',
    'customer.tags':'Изменены метки покупателя','customer.note':'Изменена заметка о покупателе',
    'customer.ban':'Покупатель заблокирован','customer.unban':'Покупатель разблокирован',
    'order.note':'Добавлена внутренняя заметка к заказу','product.bulk':'Массовое изменение товара',
    'support.template.create':'Создан шаблон ответа','support.template.update':'Изменён шаблон ответа',
    'support.template.delete':'Удалён шаблон ответа','insights.goal':'Изменена цель по выручке'}
# Event details are constructed from business metadata, never payment payloads or stock.
AUDIT_DETAIL_KEYS = {'title','price','quantity','amount','before','after','reason','added','duplicates','attempt','method','state',
                     'discount','code'}


def audit_category(code):
    if code.startswith(('finance.','insights.')):return 'finance'
    if code.startswith('customer.'):return 'support'
    if code=='promo.overused':return 'payments'
    if code in ('wallet.repair','promo.redeemed'):return 'balance'
    if code.startswith('support.'):return 'support'
    if code in ('wallet.adjust','wallet.set'):return 'balance'
    if code=='payment.method':return 'settings'
    if code.startswith(('wallet.credit','crypto.','payment.issue')):return 'payments'
    if code.startswith(('order.','payment.','delivery.')):return 'orders'
    if code.startswith(('product.','stock.')):return 'products'
    if code.startswith(('bot.','server.','backup.','broadcast.')) or code=='channel.check.failed':return 'system'
    return 'settings'


PRODUCT_TEXT_FIELDS = (('category','категория',100,True),('name','название',200,True),
                       ('description','описание',4000,False),('instruction','инструкция',4000,False))

SCHEMA = '''
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, category TEXT NOT NULL, name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', price INTEGER NOT NULL CHECK(price>0),
  mode TEXT NOT NULL CHECK(mode IN ('auto','manual')),
  instruction TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL REFERENCES products(id),
  title TEXT NOT NULL, price INTEGER NOT NULL, mode TEXT NOT NULL,
  instruction TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  charge TEXT UNIQUE, details BLOB, result BLOB, created REAL NOT NULL,
  expires REAL NOT NULL, manager TEXT, checkout INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS stock(
  id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
  fingerprint TEXT NOT NULL, secret BLOB NOT NULL, order_id TEXT REFERENCES orders(id),
  UNIQUE(product_id,fingerprint));
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
  user_id INTEGER NOT NULL, body BLOB NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_languages(
  user_id INTEGER PRIMARY KEY,language TEXT NOT NULL CHECK(language IN ('ru','en')));
CREATE TABLE IF NOT EXISTS payment_methods(network TEXT PRIMARY KEY,enabled INTEGER NOT NULL,wallet TEXT NOT NULL,api_key BLOB);
CREATE TABLE IF NOT EXISTS credited_transfers(network TEXT NOT NULL,transfer_id TEXT NOT NULL,deposit_id TEXT NOT NULL,PRIMARY KEY(network,transfer_id));
CREATE TABLE IF NOT EXISTS crypto_invalid_attempts(
  reference TEXT PRIMARY KEY,deposit_id TEXT NOT NULL,remaining INTEGER NOT NULL,
  detail TEXT NOT NULL,sent INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS emoji_packs(
  name TEXT PRIMARY KEY,title TEXT NOT NULL,items TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS emoji_assignments(
  target TEXT PRIMARY KEY,emoji_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_users(
  user_id INTEGER PRIMARY KEY,username TEXT NOT NULL DEFAULT '',
  first_name TEXT NOT NULL DEFAULT '',last_name TEXT NOT NULL DEFAULT '',
  first_seen REAL NOT NULL,last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS topups(
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
  charge TEXT UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS wallet_ledger(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, delta INTEGER NOT NULL,
  reference TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS crypto_deposits(
  id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,wallet TEXT NOT NULL,token TEXT NOT NULL,
  amount_raw TEXT NOT NULL,credit_cents INTEGER NOT NULL CHECK(credit_cents>0),
  created INTEGER NOT NULL,expires INTEGER NOT NULL,txid TEXT,
  block_number INTEGER,block_hash TEXT);
CREATE INDEX IF NOT EXISTS wallet_user ON wallet_ledger(user_id,id);
CREATE TABLE IF NOT EXISTS crypto_checks(
  deposit_id TEXT PRIMARY KEY REFERENCES crypto_deposits(id), txid TEXT NOT NULL,
  status TEXT NOT NULL, detail TEXT NOT NULL, next_try REAL NOT NULL,
  started REAL NOT NULL);
CREATE TABLE IF NOT EXISTS payment_issues(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, user_id INTEGER,
  amount INTEGER, currency TEXT NOT NULL DEFAULT '', charge TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL, created REAL NOT NULL, resolved_at REAL,
  resolution TEXT NOT NULL DEFAULT '', resolved_by TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS stock_available ON stock(product_id,order_id);
CREATE INDEX IF NOT EXISTS order_user ON orders(user_id,created);
'''

# Full definition used when an old crypto_deposits table is rebuilt; same column order as a
# fresh database gets from SCHEMA plus the ALTER TABLE migrations below.
CRYPTO_DEPOSITS_TABLE = '''CREATE TABLE crypto_deposits_new(
  id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,wallet TEXT NOT NULL,token TEXT NOT NULL,
  amount_raw TEXT NOT NULL,credit_cents INTEGER NOT NULL CHECK(credit_cents>0),
  created INTEGER NOT NULL,expires INTEGER NOT NULL,txid TEXT,
  block_number INTEGER,block_hash TEXT,network TEXT NOT NULL DEFAULT 'bsc',
  failed_attempts INTEGER NOT NULL DEFAULT 0,cancelled REAL,order_id TEXT,applied_order INTEGER,
  amount_active INTEGER NOT NULL DEFAULT 1)'''

COLUMN_MIGRATIONS = [
    ('crypto_deposits','network',"TEXT NOT NULL DEFAULT 'bsc'"),
    ('crypto_deposits','failed_attempts','INTEGER NOT NULL DEFAULT 0'),
    ('crypto_deposits','cancelled','REAL'),
    ('crypto_checks','attempt_ref','TEXT'),
    ('audit','level',"TEXT NOT NULL DEFAULT 'info'"),('audit','category',"TEXT NOT NULL DEFAULT 'settings'"),
    ('audit','summary',"TEXT NOT NULL DEFAULT ''"),('audit','details',"TEXT NOT NULL DEFAULT '{}'"),
    ('audit','user_id','INTEGER'),('audit','order_id','TEXT'),
    ('orders','quantity','INTEGER NOT NULL DEFAULT 1 CHECK(quantity BETWEEN 1 AND 100)'),
]
LATE_COLUMN_MIGRATIONS = [
    ('orders','payment_stars','INTEGER'),('topups','credit_cents','INTEGER'),
    ('crypto_deposits','order_id','TEXT'),('crypto_deposits','applied_order','INTEGER'),
    ('products','emoji_id',"TEXT NOT NULL DEFAULT ''"),('products','deleted','INTEGER NOT NULL DEFAULT 0'),
    ('products','cost_price','INTEGER'),('stock','unit_cost','INTEGER'),
    ('orders','cost_total','INTEGER'),('orders','cost_locked','INTEGER NOT NULL DEFAULT 0'),
    ('orders','paid_at','REAL'),('orders','fulfilled_at','REAL'),
    ('crypto_deposits','amount_active','INTEGER NOT NULL DEFAULT 1'),
    ('outbox','failed','REAL'),('outbox','sent_at','REAL'),('outbox','purged','INTEGER NOT NULL DEFAULT 0'),
    ('crypto_checks','notify_attempts','INTEGER NOT NULL DEFAULT 0'),
    ('crypto_invalid_attempts','attempts','INTEGER NOT NULL DEFAULT 0'),
    ('topups','expires','REAL'),
    # 3.0: promo codes on orders, richer catalog cards
    ('orders','subtotal','INTEGER'),('orders','discount_cents','INTEGER NOT NULL DEFAULT 0'),('orders','promo_id','INTEGER'),
    ('products','image_path',"TEXT NOT NULL DEFAULT ''"),('products','image_file_id',"TEXT NOT NULL DEFAULT ''"),
    ('products','sort','INTEGER NOT NULL DEFAULT 0'),('products','badge',"TEXT NOT NULL DEFAULT ''"),
    ('products','old_price','INTEGER'),
]
INDEXES = [
    'CREATE INDEX IF NOT EXISTS audit_order ON audit(order_id,id)',
    'CREATE INDEX IF NOT EXISTS audit_target ON audit(target,action,at)',
    'CREATE INDEX IF NOT EXISTS audit_category ON audit(category,id)',
    'CREATE INDEX IF NOT EXISTS audit_level ON audit(level,id)',
    'CREATE INDEX IF NOT EXISTS orders_fulfilled ON orders(fulfilled_at)',
    "CREATE INDEX IF NOT EXISTS orders_pending ON orders(expires) WHERE status='pending'",
    'CREATE INDEX IF NOT EXISTS stock_order ON stock(order_id)',
    'CREATE INDEX IF NOT EXISTS outbox_due ON outbox(sent,next_try)',
    'CREATE INDEX IF NOT EXISTS outbox_purge ON outbox(purged,sent)',
    'CREATE INDEX IF NOT EXISTS crypto_attempts_due ON crypto_invalid_attempts(sent,next_try)',
    'CREATE INDEX IF NOT EXISTS crypto_checks_due ON crypto_checks(status,next_try)',
    'CREATE INDEX IF NOT EXISTS topup_user ON topups(user_id,created)',
    "CREATE UNIQUE INDEX IF NOT EXISTS payment_issue_charge ON payment_issues(kind,charge) WHERE charge<>''",
    'CREATE INDEX IF NOT EXISTS payment_issue_open ON payment_issues(resolved_at,id)',
    # 3.0 performance: every bot-path and panel query below is index-driven at 100k+ orders.
    'CREATE INDEX IF NOT EXISTS orders_created ON orders(created)',
    'CREATE INDEX IF NOT EXISTS orders_paid_at ON orders(paid_at)',
    'CREATE INDEX IF NOT EXISTS orders_status ON orders(status,created)',
    "CREATE INDEX IF NOT EXISTS orders_user_pending ON orders(user_id) WHERE status='pending'",
    "CREATE INDEX IF NOT EXISTS orders_purchases ON orders(user_id,created,id) WHERE status='fulfilled' AND charge IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS orders_sold ON orders(coalesce(fulfilled_at,paid_at,created)) WHERE status='fulfilled' AND charge IS NOT NULL",
    'CREATE INDEX IF NOT EXISTS wallet_created ON wallet_ledger(created)',
    'CREATE INDEX IF NOT EXISTS topup_created ON topups(created)',
    'CREATE INDEX IF NOT EXISTS audit_at ON audit(at)',
    'CREATE INDEX IF NOT EXISTS audit_user ON audit(user_id,id)',
    'CREATE INDEX IF NOT EXISTS customer_first_seen ON telegram_users(first_seen,user_id)',
    'CREATE INDEX IF NOT EXISTS user_state_expires ON user_state(expires)',
    'CREATE INDEX IF NOT EXISTS user_state_updated ON user_state(updated)',
]

# Derived data kept in step with its source by triggers, inside the writer's transaction:
# wallet_balances caches SUM(wallet_ledger.delta) per user (the ledger stays the source of
# truth, see verify_wallet_balances), telegram_users is the registry of every customer with
# any activity, so customer lists never aggregate the whole order/ledger history.
DERIVED_SCHEMA = '''
CREATE TABLE IF NOT EXISTS wallet_balances(user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_state(
  user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated REAL NOT NULL, expires REAL,
  PRIMARY KEY(user_id,key));
CREATE TRIGGER IF NOT EXISTS wallet_balance_insert AFTER INSERT ON wallet_ledger BEGIN
  INSERT INTO wallet_balances(user_id,balance) VALUES(new.user_id,new.delta)
    ON CONFLICT(user_id) DO UPDATE SET balance=balance+excluded.balance;
  INSERT INTO telegram_users(user_id,first_seen,last_seen) VALUES(new.user_id,new.created,new.created)
    ON CONFLICT(user_id) DO UPDATE SET first_seen=min(first_seen,excluded.first_seen),last_seen=max(last_seen,excluded.last_seen);
END;
CREATE TRIGGER IF NOT EXISTS wallet_balance_update AFTER UPDATE OF user_id,delta ON wallet_ledger BEGIN
  UPDATE wallet_balances SET balance=balance-old.delta WHERE user_id=old.user_id;
  INSERT INTO wallet_balances(user_id,balance) VALUES(new.user_id,new.delta)
    ON CONFLICT(user_id) DO UPDATE SET balance=balance+excluded.balance;
END;
CREATE TRIGGER IF NOT EXISTS wallet_balance_delete AFTER DELETE ON wallet_ledger BEGIN
  UPDATE wallet_balances SET balance=balance-old.delta WHERE user_id=old.user_id;
END;
CREATE TRIGGER IF NOT EXISTS customer_from_order AFTER INSERT ON orders BEGIN
  INSERT INTO telegram_users(user_id,first_seen,last_seen) VALUES(new.user_id,new.created,new.created)
    ON CONFLICT(user_id) DO UPDATE SET first_seen=min(first_seen,excluded.first_seen),last_seen=max(last_seen,excluded.last_seen);
END;
CREATE TRIGGER IF NOT EXISTS customer_from_topup AFTER INSERT ON topups BEGIN
  INSERT INTO telegram_users(user_id,first_seen,last_seen) VALUES(new.user_id,new.created,new.created)
    ON CONFLICT(user_id) DO UPDATE SET first_seen=min(first_seen,excluded.first_seen),last_seen=max(last_seen,excluded.last_seen);
END;
CREATE TRIGGER IF NOT EXISTS customer_from_deposit AFTER INSERT ON crypto_deposits BEGIN
  INSERT INTO telegram_users(user_id,first_seen,last_seen) VALUES(new.user_id,new.created,new.created)
    ON CONFLICT(user_id) DO UPDATE SET first_seen=min(first_seen,excluded.first_seen),last_seen=max(last_seen,excluded.last_seen);
END;
'''
ACTIVITY_SQL = '''SELECT user_id,created FROM orders UNION ALL SELECT user_id,created FROM wallet_ledger
  UNION ALL SELECT user_id,created FROM topups UNION ALL SELECT user_id,created FROM crypto_deposits'''
CRYPTO_INDEXES = [
    'CREATE INDEX IF NOT EXISTS crypto_user ON crypto_deposits(user_id,expires)',
    'CREATE INDEX IF NOT EXISTS crypto_order ON crypto_deposits(order_id)',
    'CREATE INDEX IF NOT EXISTS crypto_txid ON crypto_deposits(txid)',
    'CREATE INDEX IF NOT EXISTS crypto_created ON crypto_deposits(created)',
    # Only invoices that still hold their amount tail must be unique (see AMOUNT_REUSE_SECONDS).
    'CREATE UNIQUE INDEX IF NOT EXISTS crypto_amount_active ON crypto_deposits(wallet,token,amount_raw) WHERE amount_active=1',
]

# 3.1 panel features. Every statement is idempotent; created by _migrate after the optional modules.
FEATURE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS customer_meta(
  user_id INTEGER PRIMARY KEY, note TEXT NOT NULL DEFAULT '', banned INTEGER NOT NULL DEFAULT 0,
  ban_reason TEXT NOT NULL DEFAULT '', banned_at REAL, banned_by TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS customer_banned ON customer_meta(user_id) WHERE banned=1;
CREATE TABLE IF NOT EXISTS customer_tags(
  user_id INTEGER NOT NULL, tag TEXT NOT NULL COLLATE NOCASE, position INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id,tag)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS customer_tag_users ON customer_tags(tag,user_id);
CREATE TABLE IF NOT EXISTS order_notes(
  id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, author TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS order_notes_order ON order_notes(order_id,id);
CREATE TABLE IF NOT EXISTS support_templates(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS live_events(
  id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
  user_id INTEGER, order_id TEXT, data TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS live_events_at ON live_events(at);
-- Insights (insights.py): covering index of paid orders per customer (funnel, LTV, cohorts).
CREATE INDEX IF NOT EXISTS orders_paid_customer ON orders(user_id,created,price) WHERE charge IS NOT NULL;
CREATE TABLE IF NOT EXISTS live_seen(username TEXT PRIMARY KEY COLLATE NOCASE, seen_id INTEGER NOT NULL, updated REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS live_from_audit AFTER INSERT ON audit
  WHEN new.action IN ('payment.accepted','payment.issue','crypto.review','delivery.review','broadcast.completed') BEGIN
  INSERT INTO live_events(at,kind,ref,user_id,order_id,data) VALUES(new.at,new.action,new.target,new.user_id,new.order_id,new.details);
END;
CREATE TRIGGER IF NOT EXISTS live_from_customer AFTER INSERT ON telegram_users BEGIN
  INSERT INTO live_events(at,kind,ref,user_id,data) VALUES(new.first_seen,'customer.new',CAST(new.user_id AS TEXT),new.user_id,
    json_object('username',new.username,'name',trim(new.first_name||' '||new.last_name)));
END;
'''
# support_messages belongs to support.py (optional module): its trigger only exists with that table.
SUPPORT_LIVE_TRIGGER = '''CREATE TRIGGER IF NOT EXISTS live_from_support AFTER INSERT ON support_messages
  WHEN new.author='customer' BEGIN
  INSERT INTO live_events(at,kind,ref,user_id,data) VALUES(new.created,'support.message',CAST(new.ticket_id AS TEXT),new.user_id,
    json_object('kind',new.kind));
END'''
CUSTOMER_TAGS_MAX = 10
CUSTOMER_TAG_LENGTH = 24
CUSTOMER_NOTE_LENGTH = 2000
BAN_REASON_LENGTH = 300
ORDER_NOTE_LENGTH = 1000
ORDER_NOTES_MAX = 200
TEMPLATE_TITLE_LENGTH = 60
TEMPLATE_TEXT_LENGTH = 3500
TEMPLATES_MAX = 200
BULK_MAX = 500
BAN_CACHE_SECONDS = 5.0
LIVE_KEEP_SECONDS = 14*86400
LIVE_PRUNE_BATCH = 1000
LIVE_LOW_STOCK_SECONDS = 6*3600
REVENUE_GOAL_KEY = 'insights.revenue_goal'
_BANNED = {}
_BANNED_LOCK = threading.Lock()


# In-process cache shared by every Store opened on the same file (bot and panel use one each).
_CACHES = {}
_CACHE_LOCK = threading.Lock()

# telegram_users is the customer registry: remember_customer() plus triggers on every table
# that records customer activity (see DERIVED_SCHEMA), backfilled once by migration 4.
CUSTOMER_COLUMNS = '''t.user_id,t.username,t.first_name,t.last_name,t.first_seen,t.last_seen'''
CUSTOMER_TOTALS = '''coalesce((SELECT balance FROM wallet_balances w WHERE w.user_id=c.user_id),0) balance,
  (SELECT count(*) FROM orders o WHERE o.user_id=c.user_id) orders,
  (SELECT coalesce(sum(price),0) FROM orders o WHERE o.user_id=c.user_id AND charge IS NOT NULL) spent,
  (SELECT max(created) FROM orders o WHERE o.user_id=c.user_id) last_order'''

# Free stock of product p: unreserved, or held by a pending order whose reservation already
# lapsed (housekeeping releases it later; reads must not wait for that). The second term is
# driven by the few lapsed pending orders (CROSS JOIN fixes the loop order), never by the
# product's sold stock.
AVAILABLE_SQL = '''((SELECT count(*) FROM stock s WHERE s.product_id=p.id AND s.order_id IS NULL)
  +(SELECT count(*) FROM orders o CROSS JOIN stock s ON s.order_id=o.id
    WHERE o.status='pending' AND o.expires<? AND s.product_id=p.id)) AS available'''
PRODUCT_ORDER = 'ORDER BY p.category,p.sort,p.id'
# Same value for whole lists: lapsed reservations are counted once per call, not per product.
LIST_AVAILABLE_COLUMNS = '''p.*,(SELECT count(*) FROM stock s WHERE s.product_id=p.id AND s.order_id IS NULL)
  +coalesce(l.held,0) AS available'''
LIST_AVAILABLE_JOIN = ''' LEFT JOIN (SELECT s.product_id,count(*) held FROM orders o CROSS JOIN stock s ON s.order_id=o.id
  WHERE o.status='pending' AND o.expires<? GROUP BY s.product_id) l ON l.product_id=p.id'''


class Store:
    def __init__(self, path=None, key=None):
        self.path = str(path or os.getenv('DATABASE_PATH', 'data/shop.sqlite3'))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        key = key or os.getenv('STOCK_KEY')
        if not key:
            raise RuntimeError('Set STOCK_KEY using: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
        key = key.encode() if isinstance(key, str) else key
        self.cipher = Fernet(key)
        self._fingerprint_key = fingerprint_key(key)
        self._local = threading.local()
        # Per-thread connections are closed together when the Store is collected or at exit.
        self._connections = weakref.WeakSet()
        weakref.finalize(self, _close_connections, self._connections)
        self._cache_id = os.path.abspath(self.path)
        self._initialize()

    # ------------------------------------------------------------------ connections

    def _open(self):
        # check_same_thread=False only so the finalizer may close it; each connection is
        # still used by one thread at a time (thread-local or temporary).
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False,
                             factory=_Connection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('PRAGMA synchronous=NORMAL')
        return db

    def _acquire(self):
        """One reusable connection per thread; nested use gets a temporary connection."""
        local = self._local
        pid = os.getpid()
        db = getattr(local, 'db', None)
        if db is not None and local.pid == pid and not local.busy:
            local.busy = True
            return db, True
        fresh = self._open()
        if db is None or local.pid != pid:
            local.db, local.pid, local.busy = fresh, pid, True
            self._connections.add(fresh)
            return fresh, True
        return fresh, False

    def _release(self, db, cached, broken=False):
        if not cached:
            db.close()
            return
        if broken:
            try:db.close()
            except sqlite3.Error:pass
            self._local.db = None
        self._local.busy = False

    @contextmanager
    def _transaction(self, begin):
        db, cached = self._acquire()
        broken = False
        try:
            if db.in_transaction:db.rollback()
            db.execute(begin)
            try:
                yield db
                db.commit()
            except BaseException:
                try:db.rollback()
                except sqlite3.Error:broken = True
                raise
        finally:
            self._release(db, cached, broken)

    @contextmanager
    def tx(self, invalidate=True):
        """Write transaction (BEGIN IMMEDIATE). invalidate=False for writes that never touch
        cached settings (currency, emoji, payment methods)."""
        with self._transaction('BEGIN IMMEDIATE') as db:
            yield db
        if invalidate:self._invalidate()

    @contextmanager
    def read(self):
        """Read-only snapshot (deferred BEGIN): never takes the write lock."""
        with self._transaction('BEGIN') as db:
            yield db

    def close(self):
        db = getattr(self._local, 'db', None)
        if db is not None and not self._local.busy:
            db.close()
            self._local.db = None

    # ------------------------------------------------------------------ cache

    def _cache(self):
        with _CACHE_LOCK:
            return _CACHES.setdefault(self._cache_id, {'generation':0,'items':{}})

    def _invalidate(self):
        cache = self._cache()
        with _CACHE_LOCK:
            cache['generation'] += 1
            cache['items'].clear()

    def _cached(self, name, build):
        cache = self._cache()
        now = time.monotonic()
        with _CACHE_LOCK:
            hit = cache['items'].get(name)
            generation = cache['generation']
            if hit and hit[0] > now:
                return copy.deepcopy(hit[1])
        value = build()
        with _CACHE_LOCK:
            if cache['generation'] == generation:
                cache['items'][name] = (now+CACHE_SECONDS, value)
        return copy.deepcopy(value)

    # ------------------------------------------------------------------ schema

    def _initialize(self):
        with closing(self._open()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            db.execute('BEGIN IMMEDIATE')
            try:
                self._migrate(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise
            self._rebuild_crypto_deposits(db)
            # Rebuilding a table drops its triggers: (re)create the derived-data triggers last.
            for statement in _statements(DERIVED_SCHEMA):
                db.execute(statement)
            # Cheap when statistics are current; refreshes planner stats after new indexes.
            db.execute('PRAGMA optimize')

    @staticmethod
    def _columns(db, table):
        return {row['name'] for row in db.execute('PRAGMA table_info('+table+')')}

    def _add_columns(self, db, migrations):
        for table, column, definition in migrations:
            if column in self._columns(db, table):continue
            db.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' '+definition)
            if column == 'paid_at':
                db.execute("""UPDATE orders SET paid_at=(SELECT min(at) FROM audit
                    WHERE target=orders.id AND action='payment.accepted') WHERE charge IS NOT NULL""")
            elif column == 'fulfilled_at':
                db.execute("""UPDATE orders SET fulfilled_at=CASE WHEN mode='auto' THEN paid_at ELSE
                    (SELECT min(at) FROM audit WHERE target=orders.id AND action='order.complete') END WHERE status='fulfilled'""")

    def _migrate(self, db):
        version = db.execute('PRAGMA user_version').fetchone()[0]
        self._add_columns(db, COLUMN_MIGRATIONS)
        if version < 1:
            # Legacy deposits were deduplicated by txid; later ones use explicit transfer ids.
            db.execute('INSERT OR IGNORE INTO credited_transfers SELECT network,txid,id FROM crypto_deposits WHERE txid IS NOT NULL')
        self._split_legacy_stock(db)
        self._add_columns(db, LATE_COLUMN_MIGRATIONS)
        db.execute('''CREATE TABLE IF NOT EXISTS finance_entries(
              id TEXT PRIMARY KEY,kind TEXT NOT NULL,category TEXT NOT NULL,amount INTEGER NOT NULL CHECK(amount>0),
              note TEXT NOT NULL,occurred_at REAL NOT NULL,order_id TEXT REFERENCES orders(id),
              created_at REAL NOT NULL,actor TEXT NOT NULL,voided_at REAL,void_reason TEXT)''')
        db.execute('CREATE INDEX IF NOT EXISTS finance_date ON finance_entries(occurred_at)')
        for statement in _statements(DERIVED_SCHEMA):
            db.execute(statement)
        for statement in INDEXES+CRYPTO_INDEXES:
            db.execute(statement)
        from broadcasts import initialize
        initialize(db)
        from channel_gate import initialize as initialize_channel
        initialize_channel(db)
        for name in OPTIONAL_MODULES:
            module = _optional(name)
            if module is not None and hasattr(module,'initialize'):
                module.initialize(db)
        if version < 4:
            self._backfill_derived(db)
        # After the backfill: registering historical customers must not flood the live feed.
        for statement in _statements(FEATURE_SCHEMA):
            db.execute(statement)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='support_messages'").fetchone():
            db.execute(SUPPORT_LIVE_TRIGGER)
        fingerprints_ok = True
        if version < 2:
            fingerprints_ok = self._migrate_fingerprints(db)
        if version < 3 and not db.execute("SELECT 1 FROM payment_methods WHERE network='bsc'").fetchone() \
                and db.execute("SELECT 1 FROM crypto_deposits WHERE network='bsc' LIMIT 1").fetchone():
            wallet = os.getenv('BSC_WALLET') or LEGACY_BSC_WALLET
            db.execute("INSERT INTO payment_methods(network,enabled,wallet,api_key) VALUES('bsc',1,?,NULL)",(wallet,))
        # An unreadable stock row (wrong STOCK_KEY?) leaves the fingerprint step for the next start.
        target = max(version, SCHEMA_VERSION if fingerprints_ok else 1)
        if target != version:
            db.execute('PRAGMA user_version='+str(target))

    @staticmethod
    def _split_legacy_stock(db):
        unique_order_index = any(index['unique'] and
            [row['name'] for row in db.execute('PRAGMA index_info("'+index['name'].replace('"','""')+'")')]==['order_id']
            for index in db.execute('PRAGMA index_list(stock)').fetchall())
        if not unique_order_index:return
        db.execute('''CREATE TABLE stock_multi(
          id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id),
          fingerprint TEXT NOT NULL,secret BLOB NOT NULL,order_id TEXT REFERENCES orders(id),
          UNIQUE(product_id,fingerprint))''')
        db.execute('INSERT INTO stock_multi SELECT id,product_id,fingerprint,secret,order_id FROM stock')
        db.execute('DROP TABLE stock')
        db.execute('ALTER TABLE stock_multi RENAME TO stock')
        db.execute('CREATE INDEX stock_available ON stock(product_id,order_id)')

    def _migrate_fingerprints(self, db):
        """Replace unsalted sha256 fingerprints by keyed HMAC; False if a row could not be read."""
        ok = True
        rows = db.execute("SELECT id,secret FROM stock WHERE substr(fingerprint,1,3)<>?",(FINGERPRINT_PREFIX,)).fetchall()
        for row in rows:
            try:
                plain = self.cipher.decrypt(row['secret']).decode()
            except (InvalidToken, ValueError, TypeError):
                ok = False
                continue
            db.execute('UPDATE stock SET fingerprint=? WHERE id=?',(self.fingerprint(plain),row['id']))
        return ok

    @staticmethod
    def _backfill_derived(db):
        """Migration 4: register every customer that only exists in activity tables and build
        the cached balances from the ledger. Runs once, inside the migration transaction."""
        db.execute('''INSERT INTO telegram_users(user_id,first_seen,last_seen)
            SELECT user_id,min(created),max(created) FROM ('''+ACTIVITY_SQL+''') WHERE true GROUP BY user_id
            ON CONFLICT(user_id) DO UPDATE SET first_seen=min(first_seen,excluded.first_seen),
            last_seen=max(last_seen,excluded.last_seen)''')
        db.execute('DELETE FROM wallet_balances')
        db.execute('INSERT INTO wallet_balances(user_id,balance) SELECT user_id,sum(delta) FROM wallet_ledger GROUP BY user_id')

    WALLET_MISMATCH_SQL = '''SELECT l.user_id,l.total ledger,coalesce(b.balance,0) cached FROM
        (SELECT user_id,sum(delta) total FROM wallet_ledger GROUP BY user_id) l
        LEFT JOIN wallet_balances b ON b.user_id=l.user_id WHERE l.total<>coalesce(b.balance,0)
        UNION ALL SELECT b.user_id,0,b.balance FROM wallet_balances b WHERE b.balance<>0
        AND NOT EXISTS(SELECT 1 FROM wallet_ledger w WHERE w.user_id=b.user_id)'''

    def verify_wallet_balances(self, repair=False, actor='system', _db=None):
        """Compare cached balances with the ledger (the source of truth). With repair=True the
        cache of mismatching users is rebuilt and a warning is logged. Returns the mismatches."""
        if _db is None:
            with (self.tx(invalidate=False) if repair else self.read()) as db:
                return self.verify_wallet_balances(repair,actor,_db=db)
        db = _db
        mismatches = [dict(row) for row in db.execute(self.WALLET_MISMATCH_SQL)]
        if repair and mismatches:
            for row in mismatches:
                db.execute('''INSERT INTO wallet_balances(user_id,balance) VALUES(?,?)
                    ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance''',(row['user_id'],row['ledger']))
            self.log(db,actor,'wallet.repair','wallets',level='warning',details={'quantity':len(mismatches)})
        return mismatches

    @staticmethod
    def _balance(db, user_id):
        row = db.execute('SELECT balance FROM wallet_balances WHERE user_id=?',(user_id,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _rebuild_crypto_deposits(db):
        """Drop the legacy UNIQUE(wallet,token,amount_raw) and UNIQUE(txid) constraints."""
        legacy = "SELECT 1 FROM pragma_index_list('crypto_deposits') WHERE origin='u'"
        if not db.execute(legacy).fetchone():return
        db.execute('PRAGMA foreign_keys=OFF')  # crypto_checks references this table
        try:
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute(legacy).fetchone():
                    old = [row['name'] for row in db.execute('PRAGMA table_info(crypto_deposits)')]
                    db.execute(CRYPTO_DEPOSITS_TABLE)
                    new = {row['name'] for row in db.execute('PRAGMA table_info(crypto_deposits_new)')}
                    columns = ','.join(column for column in old if column in new)
                    db.execute('INSERT INTO crypto_deposits_new('+columns+') SELECT '+columns+' FROM crypto_deposits')
                    db.execute('DROP TABLE crypto_deposits')
                    db.execute('ALTER TABLE crypto_deposits_new RENAME TO crypto_deposits')
                    for statement in CRYPTO_INDEXES:
                        db.execute(statement)
                    if db.execute('PRAGMA foreign_key_check(crypto_checks)').fetchone():
                        raise RuntimeError('crypto_deposits migration broke crypto_checks references')
                db.commit()
            except BaseException:
                db.rollback()
                raise
        finally:
            db.execute('PRAGMA foreign_keys=ON')

    def fingerprint(self, item):
        return stock_fingerprint(None, item, self._fingerprint_key)

    # ------------------------------------------------------------------ settings & cache-backed reads

    def currency(self):
        def build():
            with self.read() as db:
                row = db.execute("SELECT value FROM meta WHERE key='stars_per_usd'").fetchone()
            return {'code':'USD' if row else 'XTR','stars_per_usd':row[0] if row else None,'active':bool(row)}
        return self._cached('currency', build)

    def set_currency(self, stars_per_usd, actor):
        rate = str(rate_value(stars_per_usd))
        with self.read() as db:
            first = not db.execute("SELECT 1 FROM meta WHERE key='stars_per_usd'").fetchone()
        backup = None
        if first:
            # Online backup is a separate consistent snapshot; the conversion itself is atomic.
            backup = Path(self.path).with_name(Path(self.path).name+'.before-usd-'+uuid.uuid4().hex[:8]+'.bak')
            with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
        with self.tx() as db:
            if not db.execute("SELECT value FROM meta WHERE key='stars_per_usd'").fetchone():
                self._convert_to_usd(db, rate)
            db.execute("INSERT INTO meta(key,value) VALUES('stars_per_usd',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(rate,))
            self.log(db,actor,'currency.rate:'+rate,'USD')
        return {'ok':True,'backup':backup.name if backup else None,'currency':self.currency()}

    @staticmethod
    def _convert_to_usd(db, rate):
        for row in db.execute('SELECT id,price,cost_price FROM products').fetchall():
            amount = stars_to_cents(row['price'],rate)
            if amount < 1:raise ValueError('Цена товара после перевода меньше $0.01. Скорректируйте курс или цену.')
            cost = None if row['cost_price'] is None else stars_to_cents(row['cost_price'],rate)
            db.execute('UPDATE products SET price=?,cost_price=? WHERE id=?',(amount,cost,row['id']))
        for row in db.execute('SELECT id,price,cost_total,subtotal,discount_cents FROM orders').fetchall():
            cost = None if row['cost_total'] is None else stars_to_cents(row['cost_total'],rate)
            price = stars_to_cents(row['price'],rate)
            subtotal = None if row['subtotal'] is None else stars_to_cents(row['subtotal'],rate)
            discount = max(0,subtotal-price) if subtotal is not None and row['discount_cents'] else 0
            db.execute('UPDATE orders SET payment_stars=price,price=?,cost_total=?,subtotal=?,discount_cents=? WHERE id=?',
                       (price,cost,subtotal,discount,row['id']))
        for row in db.execute('SELECT id,old_price FROM products WHERE old_price IS NOT NULL').fetchall():
            db.execute('UPDATE products SET old_price=? WHERE id=?',(max(1,stars_to_cents(row['old_price'],rate)),row['id']))
        promo = _optional('promo')
        if promo is not None and hasattr(promo,'convert_to_usd'):
            promo.convert_to_usd(db,rate)
        for row in db.execute('SELECT id,unit_cost FROM stock WHERE unit_cost IS NOT NULL').fetchall():
            db.execute('UPDATE stock SET unit_cost=? WHERE id=?',(stars_to_cents(row['unit_cost'],rate),row['id']))
        for row in db.execute('SELECT id,amount FROM finance_entries').fetchall():
            db.execute('UPDATE finance_entries SET amount=? WHERE id=?',(max(1,stars_to_cents(row['amount'],rate)),row['id']))
        for row in db.execute('SELECT id,amount FROM topups').fetchall():
            db.execute('UPDATE topups SET credit_cents=? WHERE id=?',(credit_cents(row['amount'],rate),row['id']))
        # Convert cumulative per-user balances so rounding cannot create overdrafts.
        totals = {}
        for row in db.execute('SELECT id,user_id,delta FROM wallet_ledger ORDER BY id').fetchall():
            old = totals.get(row['user_id'],0);new = old+row['delta'];totals[row['user_id']] = new
            delta = credit_cents(new,rate)-credit_cents(old,rate)
            db.execute('UPDATE wallet_ledger SET delta=? WHERE id=?',(delta,row['id']))

    def money(self, amount):
        return format_usd(amount) if self.currency()['active'] else str(amount)+' Stars'

    def save_emoji_pack(self, name, title, items, actor):
        with self.tx() as db:
            db.execute("""INSERT INTO emoji_packs VALUES(?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET title=excluded.title,items=excluded.items,updated=excluded.updated""",
                (name,title,json.dumps(items,ensure_ascii=False),time.time()))
            self.log(db,actor,'emoji.pack.import',name)

    def emoji_packs(self):
        def build():
            with self.read() as db:
                return [dict(name=r['name'],title=r['title'],items=json.loads(r['items']),updated=r['updated'])
                        for r in db.execute('SELECT * FROM emoji_packs ORDER BY updated DESC')]
        return self._cached('emoji_packs', build)

    def emoji_assignments(self):
        def build():
            with self.read() as db:
                return dict(db.execute('SELECT target,emoji_id FROM emoji_assignments').fetchall())
        return self._cached('emoji_assignments', build)

    def assign_emoji(self, target, emoji_id, actor):
        from emoji_library import WELCOME_EMOJIS
        if not isinstance(target, str) or not target:raise ValueError('Место для иконки не найдено')
        emoji_id = emoji_id or ''
        if not isinstance(emoji_id, str):raise ValueError('Выберите эмодзи из загруженного набора')
        if emoji_id and not any(item['id']==emoji_id for pack in self.emoji_packs() for item in pack['items']):
            raise ValueError('Выберите эмодзи из загруженного набора')
        with self.tx() as db:
            if target.startswith('product:'):
                if not db.execute('UPDATE products SET emoji_id=? WHERE id=? AND deleted=0',(emoji_id,target[8:])).rowcount:
                    raise ValueError('Товар не найден')
            else:
                allowed = {'menu:'+key for key in ('catalog','wallet','orders','profile','guide','support','about','terms','language')}
                allowed.update(WELCOME_EMOJIS)
                categories = {r[0] for r in db.execute('SELECT DISTINCT category FROM products')}
                if target not in allowed and not (target.startswith('category:') and target[9:] in categories):
                    raise ValueError('Место для иконки не найдено')
                db.execute("""INSERT INTO emoji_assignments VALUES(?,?) ON CONFLICT(target)
                    DO UPDATE SET emoji_id=excluded.emoji_id""",(target,emoji_id))
            self.log(db,actor,'emoji.assign',target)

    def payment_methods(self, private=False):
        def build():
            with self.read() as db:
                saved = {row['network']:dict(row) for row in db.execute('SELECT * FROM payment_methods')}
                stars = db.execute("SELECT value FROM meta WHERE key='payment.stars'").fetchone()
            result = []
            for network, info in _networks().items():
                row = saved.get(network, {})
                # No built-in receiving wallet: BSC uses BSC_WALLET until one is saved.
                default_wallet = os.getenv('BSC_WALLET','') if network=='bsc' else ''
                wallet = row.get('wallet', default_wallet)
                item = dict(network=network,label=info['label'],enabled=bool(row.get('enabled',network=='bsc' and bool(wallet))),
                    wallet=wallet,token=info['token'],decimals=info['decimals'],has_api_key=bool(row.get('api_key')))
                if private:item['api_key'] = self.decrypt(row.get('api_key'))
                result.append(item)
            return {'networks':result,'stars':not stars or stars[0]=='1'}
        return self._cached('payment_methods:'+('private' if private else 'public'), build)

    def save_payment_method(self, network, enabled, wallet, api_key, actor, clear_key=False):
        from crypto_networks import NETWORKS,normalize_address,tron_friendly
        if network not in NETWORKS:raise ValueError('Неизвестная сеть')
        if not isinstance(wallet, str) or not isinstance(api_key, str):raise ValueError('Некорректные данные способа оплаты')
        wallet = wallet.strip()
        if network=='aptos' and wallet:
            if not re.fullmatch(r'0x[0-9a-fA-F]{64}',wallet):raise ValueError('Нужен полный адрес Aptos: 0x и 64 символа')
        if wallet:
            canonical = normalize_address(network,wallet)
            wallet = tron_friendly(canonical) if network=='tron' else canonical
            if network=='aptos' and len(wallet)!=66:raise ValueError('Нужен полный адрес Aptos')
        if enabled and not wallet:raise ValueError('Для включения сети укажите адрес получения')
        if len(api_key)>500 or '\n' in api_key or '\r' in api_key:raise ValueError('Некорректный API-ключ')
        with self.tx() as db:
            previous = db.execute('SELECT api_key FROM payment_methods WHERE network=?',(network,)).fetchone()
            if clear_key:secret = None
            elif api_key.strip():secret = self.encrypt(api_key.strip())
            else:secret = previous['api_key'] if previous else None
            db.execute('''INSERT INTO payment_methods VALUES(?,?,?,?) ON CONFLICT(network)
                DO UPDATE SET enabled=excluded.enabled,wallet=excluded.wallet,api_key=excluded.api_key''',
                (network,int(enabled),wallet,secret))
            self.log(db,actor,'payment.method',network)

    def set_stars_enabled(self, enabled, actor):
        with self.tx() as db:
            db.execute("INSERT INTO meta VALUES('payment.stars',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",('1' if enabled else '0',))
            self.log(db,actor,'payment.method','Stars')

    # ------------------------------------------------------------------ crypto (USDT)

    def create_crypto_deposit(self, user_id, cents, order_id=None, network='bsc'):
        if not self.currency()['active']:
            raise ValueError('Сначала включите долларовую валюту магазина')
        if type(cents) is not int or not (1<=cents<=100000000 if order_id else 100<=cents<=100000):
            raise ValueError('Пополнение USDT: от $1 до $1000')
        networks = _networks()
        method = next((item for item in self.payment_methods()['networks'] if item['network']==network),None)
        if network not in networks or not method or not method['enabled'] or not method['wallet']:
            raise ValueError('Способ оплаты недоступен')
        wallet = method['wallet'];token = method['token'];decimals = networks[network]['decimals']
        if network=='bsc':
            from bep20 import address
            wallet = address(wallet)
        with self.tx(invalidate=False) as db:
            now = int(time.time())
            if order_id:
                order = db.execute("""SELECT * FROM orders WHERE id=? AND user_id=? AND status='pending'
                    AND checkout=0 AND price=? AND expires>?""",(order_id,user_id,cents,now)).fetchone()
                if not order:raise ValueError('Заказ недоступен для оплаты')
            old = db.execute('''SELECT * FROM crypto_deposits WHERE user_id=? AND credit_cents=? AND wallet=? AND token=?
                AND cancelled IS NULL AND txid IS NULL AND expires>? AND coalesce(order_id,'')=? ORDER BY created DESC LIMIT 1''',
                (user_id,cents,wallet,token,now,order_id or '')).fetchone()
            if old:
                if order_id:self._hold_order(db,order_id,old['expires'])
                return dict(old)
            if db.execute('SELECT count(*) FROM crypto_deposits WHERE user_id=? AND cancelled IS NULL AND txid IS NULL AND expires>?',(user_id,now)).fetchone()[0]>=3:
                raise ValueError('Уже есть три активных счёта USDT. Дождитесь истечения срока.')
            raw = self._allocate_amount(db,wallet,token,cents,decimals,now)
            did = uuid.uuid4().hex
            expires = now+CRYPTO_INVOICE_SECONDS
            db.execute('''INSERT INTO crypto_deposits(id,user_id,wallet,token,amount_raw,credit_cents,created,expires,network,order_id)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(did,user_id,wallet,token,raw,cents,now,expires,network,order_id))
            if order_id:self._hold_order(db,order_id,expires)
            self.log(db,'telegram','crypto.created',did)
            return dict(db.execute('SELECT * FROM crypto_deposits WHERE id=?',(did,)).fetchone())

    @staticmethod
    def _hold_order(db, order_id, until):
        """An invoice for an order keeps the stock reserved for as long as the invoice is payable."""
        db.execute("UPDATE orders SET expires=max(expires,?) WHERE id=? AND status='pending'",(until,order_id))

    @staticmethod
    def _allocate_amount(db, wallet, token, cents, decimals, now):
        # Only 99 non-zero four-decimal tails fit below one cent; a tail is reused once the
        # earlier invoice expired AMOUNT_REUSE_SECONDS ago (credit_crypto checks the time window).
        base = cents*10**(decimals-2);step = 10**(decimals-4)
        candidates = [str(base+suffix*step) for suffix in range(1,100)]
        marks = ','.join('?'*len(candidates))
        db.execute('UPDATE crypto_deposits SET amount_active=0 WHERE wallet=? AND token=? AND amount_active=1 AND expires<? AND amount_raw IN ('+marks+')',
                   [wallet,token,now-AMOUNT_REUSE_SECONDS,*candidates])
        used = {row[0] for row in db.execute('SELECT amount_raw FROM crypto_deposits WHERE wallet=? AND token=? AND amount_active=1 AND amount_raw IN ('+marks+')',
                                             [wallet,token,*candidates])}
        free = [raw for raw in candidates if raw not in used]
        if not free:raise ValueError('Не удалось выделить сумму. Выберите другую сумму пополнения.')
        return secrets.choice(free)

    def crypto_deposit(self, did, user_id):
        with self.read() as db:
            row = db.execute('SELECT * FROM crypto_deposits WHERE id=? AND user_id=?',(did,user_id)).fetchone()
            if not row:raise ValueError('Счёт не найден или принадлежит другому покупателю')
            return dict(row)

    def crypto_context(self, user_id, did=None):
        key = 'crypto_input:'+str(user_id)
        if did is None:
            with self.read() as db:
                row = db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
                return row[0] if row else None
        with self.tx(invalidate=False) as db:
            if not db.execute('SELECT 1 FROM crypto_deposits WHERE id=? AND user_id=?',(did,user_id)).fetchone():
                raise ValueError('Счёт не найден')
            db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value<>excluded.value',(key,did))
            return did

    def clear_crypto_context(self, user_id, _db=None, did=None):
        """Forget which invoice the user's next TXID belongs to (only if it is `did`, when given)."""
        key = 'crypto_input:'+str(user_id)
        with (nullcontext(_db) if _db is not None else self.tx(invalidate=False)) as db:
            if did is None:db.execute('DELETE FROM meta WHERE key=?',(key,))
            else:db.execute('DELETE FROM meta WHERE key=? AND value=?',(key,did))

    def credit_crypto(self, did, user_id, proof):
        with self.tx(invalidate=False) as db:
            row = db.execute('SELECT * FROM crypto_deposits WHERE id=? AND user_id=?',(did,user_id)).fetchone()
            if not row:raise ValueError('Счёт не найден')
            if proof.network!=row['network']:raise ValueError('Сеть перевода не соответствует счёту')
            if row['txid']:
                if row['txid']!=proof.txid:raise ValueError('Счёт уже оплачен другой транзакцией')
                return False
            if row['cancelled']:raise ValueError('Пополнение отменено: исчерпаны 5 попыток ввода TXID.')
            if (proof.wallet!=row['wallet'] or proof.token!=row['token'] or proof.amount_raw!=row['amount_raw']
                or not row['created']<=proof.block_time<=row['expires']):
                raise ValueError('Перевод не соответствует счёту или поступил вне срока. Обратитесь в поддержку.')
            # One transaction may carry several transfers (one per invoice): dedupe per transfer,
            # and treat a legacy whole-transaction credit as covering all of its transfers.
            transfer_id = proof.transfer_id or proof.txid
            keys = {transfer_id, proof.txid}
            if any(db.execute('SELECT 1 FROM credited_transfers WHERE network=? AND transfer_id=?',(row['network'],key)).fetchone() for key in keys):
                raise ValueError('Эта транзакция уже зачислена')
            db.execute('INSERT INTO credited_transfers VALUES(?,?,?)',(row['network'],transfer_id,did))
            db.execute('UPDATE crypto_deposits SET txid=?,block_number=?,block_hash=? WHERE id=?',
                       (proof.txid,proof.block_number,proof.block_hash,did))
            db.execute('INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(?,?,?,?)',
                       (user_id,row['credit_cents'],row['network']+':'+transfer_id,time.time()))
            self.log(db,row['network'],'wallet.credit_usdt:'+str(row['credit_cents']),did)
            if row['order_id']:
                order = db.execute('SELECT * FROM orders WHERE id=?',(row['order_id'],)).fetchone()
                # An approved Stars checkout may still be paid: keep the USDT on the balance then.
                if (order and order['status']=='pending' and not order['checkout'] and order['expires']>=time.time()
                        and order['user_id']==user_id and order['price']==row['credit_cents']):
                    self._paid(db,order['id'],user_id,order['price'],'XTR','wallet:'+order['id'],wallet=True)
                    db.execute('UPDATE crypto_deposits SET applied_order=1 WHERE id=?',(did,))
            db.execute("UPDATE crypto_checks SET status='paid', detail='Перевод подтверждён' WHERE deposit_id=? AND txid=?",(did,proof.txid))
            self.clear_crypto_context(user_id,_db=db,did=did)
            return True

    def queue_crypto_check(self, deposit_id, txid, attempt_ref=None):
        now = time.time()
        attempt_ref = attempt_ref or uuid.uuid4().hex
        with self.tx(invalidate=False) as db:
            deposit = db.execute('SELECT txid,cancelled FROM crypto_deposits WHERE id=?',(deposit_id,)).fetchone()
            if not deposit or deposit['cancelled']:raise ValueError('Пополнение отменено: исчерпаны 5 попыток ввода TXID.')
            if deposit['txid']:raise ValueError('Счёт уже оплачен')
            if db.execute('SELECT 1 FROM crypto_invalid_attempts WHERE reference=?',(attempt_ref,)).fetchone():return attempt_ref
            db.execute("""INSERT INTO crypto_checks(deposit_id,txid,status,detail,next_try,started,attempt_ref) VALUES(?,?,'pending','Ожидает проверки',?,?,?)
                ON CONFLICT(deposit_id) DO UPDATE SET txid=excluded.txid,status='pending',
                detail=excluded.detail,next_try=excluded.next_try,started=excluded.started,attempt_ref=excluded.attempt_ref
                WHERE crypto_checks.status NOT IN ('paid','notified','notify_failed','cancelled')""",(deposit_id,txid,now+30,now,attempt_ref))
        return attempt_ref

    def crypto_input_failure(self, deposit_id, user_id, reference, detail, _db=None):
        with (nullcontext(_db) if _db is not None else self.tx(invalidate=False)) as db:
            deposit = db.execute('SELECT * FROM crypto_deposits WHERE id=? AND user_id=?',(deposit_id,user_id)).fetchone()
            if not deposit:raise ValueError('Счёт не найден')
            previous = db.execute('SELECT 1 FROM crypto_invalid_attempts WHERE reference=?',(reference,)).fetchone()
            if not previous and not deposit['txid'] and not deposit['cancelled']:
                failed = deposit['failed_attempts']+1
                cancelled = time.time() if failed>=5 else None
                db.execute('UPDATE crypto_deposits SET failed_attempts=?,cancelled=? WHERE id=?',(failed,cancelled,deposit_id))
                db.execute('UPDATE crypto_invalid_attempts SET sent=1 WHERE deposit_id=? AND sent=0',(deposit_id,))
                db.execute('INSERT INTO crypto_invalid_attempts(reference,deposit_id,remaining,detail) VALUES(?,?,?,?)',(reference,deposit_id,max(0,5-failed),str(detail)[:1000]))
                self.log(db,'telegram','crypto.invalid_txid',deposit_id,details={'attempt':failed})
                if cancelled:
                    db.execute("UPDATE crypto_checks SET status='cancelled',detail='Пополнение отменено: исчерпаны 5 попыток TXID' WHERE deposit_id=?",(deposit_id,))
                    self.clear_crypto_context(user_id,_db=db,did=deposit_id)
                    self.log(db,'system','crypto.cancelled',deposit_id,level='warning')
            return dict(db.execute('SELECT * FROM crypto_deposits WHERE id=?',(deposit_id,)).fetchone())

    def crypto_failure_notices(self):
        with self.read() as db:
            return [dict(row) for row in db.execute('''SELECT a.*,d.user_id FROM crypto_invalid_attempts a
                JOIN crypto_deposits d ON d.id=a.deposit_id WHERE a.sent=0 AND a.next_try<=? AND d.txid IS NULL
                ORDER BY a.next_try,a.rowid LIMIT 10''',(time.time(),))]

    def crypto_failure_notice_result(self, reference, sent, permanent=False):
        with self.tx(invalidate=False) as db:
            row = db.execute('SELECT deposit_id,sent,attempts FROM crypto_invalid_attempts WHERE reference=?',(reference,)).fetchone()
            if not row or row['sent']:return
            attempts = row['attempts']+1
            if sent:
                db.execute('UPDATE crypto_invalid_attempts SET sent=1,attempts=? WHERE reference=?',(attempts,reference))
            elif permanent or attempts>=RETRY_GIVE_UP_ATTEMPTS:
                db.execute('UPDATE crypto_invalid_attempts SET sent=1,attempts=? WHERE reference=?',(attempts,reference))
                self.log(db,'bot','crypto.notice_failed',row['deposit_id'],level='warning',
                         details={'attempt':attempts,'state':'blocked' if permanent else 'gave_up'})
            else:
                db.execute('UPDATE crypto_invalid_attempts SET attempts=?,next_try=? WHERE reference=?',
                           (attempts,time.time()+retry_delay(attempts),reference))

    def crypto_check_result(self, deposit_id, txid, pending, detail, attempt_ref=None):
        with self.tx(invalidate=False) as db:
            old = db.execute('SELECT * FROM crypto_checks WHERE deposit_id=?',(deposit_id,)).fetchone()
            deposit = db.execute('SELECT * FROM crypto_deposits WHERE id=?',(deposit_id,)).fetchone()
            if (not old or not deposit or deposit['cancelled'] or deposit['txid'] or old['txid']!=txid
                    or (attempt_ref and old['attempt_ref']!=attempt_ref)):
                return
            db.execute("""UPDATE crypto_checks SET status=CASE WHEN ? AND started>? THEN 'pending' ELSE 'review' END,
                detail=?,next_try=? WHERE deposit_id=? AND txid=? AND status!='paid'""",
                (pending,time.time()-86400,detail,time.time()+45,deposit_id,txid))
            if not pending:
                reference = attempt_ref or old['attempt_ref'] or 'legacy:'+deposit_id+':'+txid
                self.crypto_input_failure(deposit_id,deposit['user_id'],reference,detail,_db=db)
            new = db.execute('SELECT status FROM crypto_checks WHERE deposit_id=?',(deposit_id,)).fetchone()
            if new and new['status']=='review' and old['status']!='review':
                self.log(db,deposit['network'],'crypto.review',deposit_id,level='warning')

    def crypto_checks_due(self):
        with self.read() as db:
            return [dict(r) for r in db.execute("""SELECT d.*,c.txid AS submitted,c.attempt_ref FROM crypto_checks c
                JOIN crypto_deposits d ON d.id=c.deposit_id WHERE c.status='pending'
                AND c.next_try<=? AND d.txid IS NULL AND d.cancelled IS NULL ORDER BY c.next_try LIMIT 5""",(time.time(),))]

    def crypto_notifications_due(self):
        with self.read() as db:
            return [dict(r) for r in db.execute("""SELECT d.* FROM crypto_checks c JOIN crypto_deposits d
                ON d.id=c.deposit_id WHERE d.txid IS NOT NULL AND c.status NOT IN ('notified','notify_failed')
                AND c.next_try<=? ORDER BY c.next_try LIMIT 5""",(time.time(),))]

    def crypto_notification(self, deposit_id, sent, permanent=False):
        with self.tx(invalidate=False) as db:
            row = db.execute('SELECT status,notify_attempts FROM crypto_checks WHERE deposit_id=?',(deposit_id,)).fetchone()
            if not row or row['status'] in ('notified','notify_failed'):return
            attempts = row['notify_attempts']+1
            if sent:
                db.execute("UPDATE crypto_checks SET status='notified',notify_attempts=? WHERE deposit_id=?",(attempts,deposit_id))
            elif permanent or attempts>=RETRY_GIVE_UP_ATTEMPTS:
                db.execute("UPDATE crypto_checks SET status='notify_failed',notify_attempts=? WHERE deposit_id=?",(attempts,deposit_id))
                self.log(db,'bot','crypto.notify_failed',deposit_id,level='warning',
                         details={'attempt':attempts,'state':'blocked' if permanent else 'gave_up'})
            else:
                db.execute('UPDATE crypto_checks SET notify_attempts=?,next_try=? WHERE deposit_id=?',
                           (attempts,time.time()+retry_delay(attempts),deposit_id))

    def retry_crypto_check(self, deposit_id, actor):
        with self.tx(invalidate=False) as db:
            if not db.execute("""UPDATE crypto_checks SET status='pending',detail='Запрошена повторная проверка',
                next_try=0,started=? WHERE deposit_id=? AND status!='paid'
                AND EXISTS(SELECT 1 FROM crypto_deposits WHERE id=? AND txid IS NULL AND cancelled IS NULL)""",
                (time.time(),deposit_id,deposit_id)).rowcount:
                raise ValueError('Для проверки нужен TXID неоплаченного счёта')
            self.log(db,actor,'crypto.retry',deposit_id)

    def payment_list(self):
        with self.read() as db:
            return {
                'crypto': [dict(r) for r in db.execute("""SELECT d.*,c.txid AS submitted,c.status,c.detail
                    FROM (SELECT * FROM crypto_deposits ORDER BY created DESC LIMIT 300) d
                    LEFT JOIN crypto_checks c ON c.deposit_id=d.id ORDER BY d.created DESC""")],
                # Each branch walks its created index for the newest 300 before the merge.
                'stars': [dict(r) for r in db.execute("""SELECT * FROM (SELECT id,user_id,amount,credit_cents,charge,created,'wallet' AS destination
                    FROM topups ORDER BY created DESC LIMIT 300) UNION ALL SELECT * FROM (
                    SELECT id,user_id,coalesce(payment_stars,price),price,charge,created,'order'
                    FROM orders WHERE charge IS NOT NULL AND charge NOT LIKE 'wallet:%' ORDER BY created DESC LIMIT 300)
                    ORDER BY created DESC LIMIT 300""")],
            }

    # ------------------------------------------------------------------ payment issues

    def _record_payment_issue(self, kind, payload, user_id, amount, currency, charge, detail):
        """Persist a real charge the shop could not apply. Runs in its own transaction so it
        survives the caller's rollback; never masks the caller's exception."""
        try:
            with self.tx(invalidate=False) as db:
                cursor = db.execute('''INSERT OR IGNORE INTO payment_issues(kind,payload,user_id,amount,currency,charge,detail,created)
                    VALUES(?,?,?,?,?,?,?,?)''',(kind,str(payload)[:200],user_id if type(user_id) is int else None,
                    amount if type(amount) is int else None,str(currency or '')[:16],str(charge or '')[:200],
                    str(detail)[:1000],time.time()))
                if cursor.rowcount:
                    self.log(db,'telegram','payment.issue',cursor.lastrowid,level='error',
                             user_id=user_id if type(user_id) is int else None,
                             order_id=str(payload) if kind=='order' else None,
                             details={'amount':amount,'method':'Stars','state':kind,'reason':str(detail)[:300]})
                    self._notify(db,'payment_issue','⚠️ Платёж получен, но не проведён ('+('заказ' if kind=='order' else 'пополнение')
                                 +' '+str(payload)[:16]+', '+str(amount)+' '+str(currency or '')+').\nПричина: '+str(detail)[:300]
                                 +'\nПроверьте «Ошибки → Проблемные платежи».',key=str(charge or payload)[:200])
        except Exception:
            log_.exception('Could not record a payment issue for %s', kind)

    def payment_issues(self, open_only=True):
        with self.read() as db:
            where = ' WHERE resolved_at IS NULL' if open_only else ''
            return [dict(row) for row in db.execute('SELECT * FROM payment_issues'+where+' ORDER BY id DESC LIMIT 500')]

    def resolve_payment_issue(self, issue_id, note, actor):
        if type(issue_id) is not int:raise ValueError('Проблема с платежом не найдена')
        if not isinstance(note, str) or not note.strip() or len(note)>500:
            raise ValueError('Опишите решение (до 500 символов)')
        with self.tx(invalidate=False) as db:
            row = db.execute('SELECT * FROM payment_issues WHERE id=?',(issue_id,)).fetchone()
            if not row:raise ValueError('Проблема с платежом не найдена')
            if row['resolved_at'] is not None:raise ValueError('Проблема с платежом уже закрыта')
            db.execute('UPDATE payment_issues SET resolved_at=?,resolution=?,resolved_by=? WHERE id=?',
                       (time.time(),note.strip(),str(actor),issue_id))
            self.log(db,actor,'payment.issue.resolved',issue_id,user_id=row['user_id'],
                     order_id=row['payload'] if row['kind']=='order' else None,details={'reason':note.strip()})

    # ------------------------------------------------------------------ misc state

    def input_mode(self, user_id, mode=None):
        key = 'input_mode:'+str(user_id)
        with self.read() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        current = row[0] if row else ''
        if mode is None or mode==current:return current
        if not isinstance(mode, str):raise ValueError('Некорректный режим ввода')
        with self.tx(invalidate=False) as db:
            if mode:db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,mode))
            else:db.execute('DELETE FROM meta WHERE key=?',(key,))
        return mode

    def user_state(self, user_id, key, value=None, ttl=None):
        """Small per-user key-value state for the bot.
        value=None reads (None when missing or expired); '' deletes; a string stores it, for
        `ttl` seconds when given. Writes only on change. Housekeeping removes expired entries
        and entries untouched for USER_STATE_KEEP_SECONDS."""
        if type(user_id) is not int or not isinstance(key, str) or not 1<=len(key)<=64:
            raise ValueError('Некорректное состояние пользователя')
        now = time.time()
        with self.read() as db:
            row = db.execute('SELECT value,expires FROM user_state WHERE user_id=? AND key=?',(user_id,key)).fetchone()
        current = row['value'] if row and (row['expires'] is None or row['expires']>now) else None
        if value is None:return current
        if not isinstance(value, str) or len(value)>USER_STATE_MAX_VALUE:raise ValueError('Некорректное состояние пользователя')
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0<ttl<=30*86400):
            raise ValueError('Некорректный срок хранения состояния')
        expires = now+ttl if ttl is not None else None
        if not value:
            if row is None:return None
            with self.tx(invalidate=False) as db:
                db.execute('DELETE FROM user_state WHERE user_id=? AND key=?',(user_id,key))
            return None
        if current==value and ttl is None and row['expires'] is None:return value
        with self.tx(invalidate=False) as db:
            db.execute('''INSERT INTO user_state(user_id,key,value,updated,expires) VALUES(?,?,?,?,?)
                ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value,updated=excluded.updated,expires=excluded.expires''',
                (user_id,key,value,now,expires))
        return value

    def encrypt(self, value):
        return self.cipher.encrypt(value.encode())

    def decrypt(self, value):
        if not value:return ''
        try:
            return self.cipher.decrypt(value).decode()
        except (InvalidToken, TypeError, UnicodeError):
            raise ValueError('Не удалось расшифровать данные: STOCK_KEY не подходит или данные повреждены') from None

    @staticmethod
    def log(db, actor, action, target, *, level='info', details=None, user_id=None, order_id=None):
        code = action.split(':',1)[0]
        category = audit_category(code)
        info = dict(details or {})
        if category=='orders':
            order_id = order_id or str(target)
        if order_id:
            row = db.execute('SELECT user_id,title,price,quantity FROM orders WHERE id=?',(order_id,)).fetchone()
            if row:
                user_id = row['user_id'];info.update(dict(row))
        elif category=='products':
            row = db.execute('SELECT name FROM products WHERE id=?',(str(target),)).fetchone()
            if row:info['title'] = row['name']
        elif code.startswith('crypto.') or code=='wallet.credit_usdt':
            row = db.execute('SELECT user_id,order_id,credit_cents,network FROM crypto_deposits WHERE id=?',(str(target),)).fetchone()
            if row:
                user_id = row['user_id'];order_id = row['order_id'];info['amount'] = row['credit_cents']
                network = _networks().get(row['network'])
                info['method'] = 'USDT '+(network['label'] if network else str(row['network']))
        elif code=='wallet.credit':
            row = db.execute('SELECT user_id,credit_cents FROM topups WHERE id=?',(str(target),)).fetchone()
            if row:user_id = row['user_id'];info['amount'] = row['credit_cents']
        if category=='balance':
            try:user_id = int(target)
            except (TypeError,ValueError):pass
        info = {key:value for key,value in info.items() if key in AUDIT_DETAIL_KEYS}
        def redact(text):
            for key in ('BOT_TOKEN','ADMIN_PASSWORD','STOCK_KEY'):
                secret = os.getenv(key)
                if secret:text = text.replace(secret,'[скрыто]')
            return text
        db.execute('''INSERT INTO audit(at,actor,action,target,level,category,summary,details,user_id,order_id)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',(time.time(),redact(str(actor)),redact(action),str(target),level,category,
            AUDIT_LABELS.get(code,code),redact(json.dumps(info,ensure_ascii=False)),user_id,order_id))

    # ------------------------------------------------------------------ catalog & stock

    def products(self, public=False):
        with self.read() as db:
            return [dict(r) for r in db.execute('SELECT '+LIST_AVAILABLE_COLUMNS+' FROM products p'+LIST_AVAILABLE_JOIN+' WHERE p.deleted=0 '
                + ('AND p.active=1 ' if public else '') + PRODUCT_ORDER,(time.time(),))]

    def product(self, product_id, public=True):
        with self.read() as db:
            row = db.execute('SELECT p.*,'+AVAILABLE_SQL+' FROM products p WHERE p.id=? AND p.deleted=0'
                + (' AND p.active=1' if public else ''),(time.time(),product_id)).fetchone()
            return dict(row) if row else None

    def categories(self):
        """Public categories in catalog order with the number of visible products (cached)."""
        def build():
            with self.read() as db:
                return [dict(name=row[0],count=row[1]) for row in db.execute(
                    'SELECT category,count(*) FROM products WHERE deleted=0 AND active=1 GROUP BY category ORDER BY category')]
        return self._cached('catalog:categories', build)

    @staticmethod
    def image_file(data_dir, relative):
        """Absolute path of a stored product image (product-images/<id>-<hash8>.jpg inside
        data_dir), or None when the value is not such a path, escapes the directory (symlinks
        included) or the file is missing. Never trust image_path blindly: it is read, sent and
        deleted."""
        if not isinstance(relative, str) or not PRODUCT_IMAGE_RE.fullmatch(relative):return None
        base = (Path(data_dir).resolve()/'product-images')
        try:
            path = (Path(data_dir).resolve()/relative).resolve()
        except (OSError, RuntimeError):
            return None
        return path if path.parent==base and path.is_file() else None

    def product_image(self, product_id):
        """Absolute path of a product's image file, or None (no image or file missing)."""
        with self.read() as db:
            row = db.execute('SELECT image_path FROM products WHERE id=? AND deleted=0',(product_id,)).fetchone()
        if not row or not row['image_path']:return None
        return self.image_file(self.data_dir(),row['image_path'])

    def data_dir(self):
        return Path(self.path).resolve().parent

    def set_product_image(self, product_id, path, actor):
        """Attach an image saved by the panel (relative to the data directory:
        product-images/<id>-<hash8>.jpg) or remove it with ''. The Telegram file_id cache is
        reset; the previous file is deleted once nothing references it."""
        if not isinstance(path, str):raise ValueError('Некорректный путь к фото')
        path = path.replace('\\','/')
        if path and (not PRODUCT_IMAGE_RE.fullmatch(path) or not path.startswith('product-images/'+str(product_id)+'-')):
            raise ValueError('Некорректный путь к фото товара')
        with self.tx() as db:
            row = db.execute('SELECT image_path FROM products WHERE id=? AND deleted=0',(product_id,)).fetchone()
            if not row:raise ValueError('Товар не найден')
            previous = row['image_path']
            if previous==path:return {'ok':True,'image_path':path}
            db.execute("UPDATE products SET image_path=?,image_file_id='' WHERE id=?",(path,product_id))
            self.log(db,actor,'product.image',product_id,details={'state':'set' if path else 'removed'})
        old = self.image_file(self.data_dir(),previous) if previous else None
        if old is not None:
            try:
                old.unlink()
            except OSError:
                log_.warning('Could not remove old product image %s', previous)
        return {'ok':True,'image_path':path}

    def cache_image_file_id(self, product_id, file_id, image_path):
        """Remember Telegram's file_id for the image that was sent (bot: send once, reuse after).
        image_path is the path the bot uploaded: if the panel replaced the photo meanwhile, the
        stale file_id is not stored for the new one. Returns True when stored."""
        if not isinstance(file_id, str) or len(file_id)>300:raise ValueError('Некорректный file_id')
        if not isinstance(image_path, str) or not image_path:return False
        with self.tx(invalidate=False) as db:
            return bool(db.execute('UPDATE products SET image_file_id=? WHERE id=? AND image_path=?',
                                   (file_id,product_id,image_path)).rowcount)

    def reorder_products(self, product_ids, actor):
        """Panel drag-and-drop / ↑↓: sets `sort` by position (10, 20, …) for the given ids."""
        if (not isinstance(product_ids, list) or not 1<=len(product_ids)<=1000
                or any(type(pid) is not int for pid in product_ids) or len(set(product_ids))!=len(product_ids)):
            raise ValueError('Некорректный порядок товаров')
        with self.tx() as db:
            found = db.execute('SELECT count(*) FROM products WHERE deleted=0 AND id IN ('+','.join('?'*len(product_ids))+')',
                               product_ids).fetchone()[0]
            if found!=len(product_ids):raise ValueError('Товар не найден')
            db.executemany('UPDATE products SET sort=? WHERE id=?',[((index+1)*10,pid) for index,pid in enumerate(product_ids)])
            self.log(db,actor,'product.sort',product_ids[0],details={'quantity':len(product_ids)})
        return {'ok':True}

    def move_product(self, product_id, direction, actor):
        """Panel ↑/↓: swap the product with its neighbour inside its category (order: sort, id).
        Swaps the two `sort` values when the category's values are distinct, otherwise renumbers
        the category 10, 20, … first so ties never make the move a no-op. → {'ok', 'moved'}."""
        if direction not in ('up','down'):raise ValueError('Некорректное направление')
        with self.tx() as db:
            row = db.execute('SELECT category FROM products WHERE id=? AND deleted=0',(product_id,)).fetchone()
            if not row:raise ValueError('Товар не найден')
            siblings = [tuple(r) for r in db.execute('SELECT id,sort FROM products WHERE deleted=0 AND category=? ORDER BY sort,id',
                                                     (row['category'],))]
            index = next(i for i,(pid,_) in enumerate(siblings) if pid==product_id)
            other = index-1 if direction=='up' else index+1
            if not 0<=other<len(siblings):return {'ok':True,'moved':False}
            sorts = [sort for _,sort in siblings]
            if len(set(sorts))!=len(sorts):
                sorts = [(i+1)*10 for i in range(len(siblings))]
            ids = [pid for pid,_ in siblings]
            ids[index], ids[other] = ids[other], ids[index]
            db.executemany('UPDATE products SET sort=? WHERE id=?',
                           [(sort,pid) for pid,sort,(old_id,old_sort) in zip(ids,sorts,siblings) if (pid,sort)!=(old_id,old_sort)])
            self.log(db,actor,'product.move',product_id,details={'state':direction})
        return {'ok':True,'moved':True}

    def repeat_order(self, user_id, order_id):
        """«Купить снова»: (product_id, quantity) of the user's paid order if the product is
        still on sale. Raises ValueError with a customer-facing message otherwise."""
        with self.read() as db:
            row = db.execute('SELECT product_id,quantity FROM orders WHERE id=? AND user_id=? AND charge IS NOT NULL',
                             (order_id,user_id)).fetchone()
            if not row:raise ValueError('Покупка не найдена')
            if not db.execute('SELECT 1 FROM products WHERE id=? AND active=1 AND deleted=0',(row['product_id'],)).fetchone():
                raise ValueError('Этот товар больше не продаётся')
        return row['product_id'], row['quantity']

    @staticmethod
    def _product_values(p):
        if not isinstance(p, dict):raise ValueError('Некорректные данные товара')
        values = {}
        for field, label, limit, required in PRODUCT_TEXT_FIELDS:
            value = p.get(field, None if required else '')
            if value is None and not required:value = ''
            if not isinstance(value, str) or (required and not value.strip()):
                raise ValueError('Заполните поле товара: '+label)
            if len(emoji_text.plain(value))>limit or len(value)>limit+40*emoji_text.MAX_PER_FIELD:
                raise emoji_text.FieldError(field, 'Поле «'+label+'» длиннее '+str(limit)+' символов')
            emoji_text.validate(value, label, field=field)
            values[field] = value
        price = p.get('price')
        if type(price) is not int or not 0<price<=10**10:raise ValueError('Цена должна быть больше нуля')
        if p.get('mode') not in ('auto','manual'):raise ValueError('Выберите режим выдачи: auto или manual')
        values['price'] = price;values['mode'] = p['mode'];values['active'] = int(bool(p.get('active',True)))
        emoji_id = p.get('emoji_id')
        if emoji_id is not None:
            if not isinstance(emoji_id, str):raise ValueError('Некорректный эмодзи товара')
            emoji_id = emoji_text.normalize_id(emoji_id)
        return values, emoji_id

    @staticmethod
    def _card_values(p, price):
        """Optional catalog-card fields; only keys present in p are changed."""
        values = {}
        if 'sort' in p:
            if type(p['sort']) is not int or not -10**6<=p['sort']<=10**6:raise ValueError('Порядок: целое число от -1000000 до 1000000')
            values['sort'] = p['sort']
        if 'badge' in p:
            badge = p['badge'] if p['badge'] is not None else ''
            if not isinstance(badge, str) or len(badge.strip())>BADGE_MAX or '\n' in badge:
                raise ValueError('Метка товара: не длиннее '+str(BADGE_MAX)+' символов')
            values['badge'] = badge.strip()
        if 'old_price' in p:
            old = p['old_price']
            if old is not None and (type(old) is not int or not 0<old<=10**10):raise ValueError('Некорректная старая цена')
            if old is not None and old<=price:raise ValueError('Старая цена должна быть больше текущей')
            values['old_price'] = old
        return values

    def save_product(self, p, actor='admin', product_id=None):
        fields = ('category','name','description','price','mode','instruction','active','emoji_id')
        values, emoji_id = self._product_values(p)
        known = {item['id'] for pack in self.emoji_packs() for item in pack['items']}
        for field, label, _, _ in PRODUCT_TEXT_FIELDS:
            emoji_text.validate(values[field], label, known, field)
        card = self._card_values(p, values['price'])
        values = [values[k] for k in fields[:-1]]
        if 'cost_price' in p:self.validate_cost(p['cost_price'])
        with self.tx() as db:  # invalidates the cached catalog (categories)
            if product_id:
                old = db.execute('SELECT * FROM products WHERE id=? AND deleted=0', (product_id,)).fetchone()
                if not old:
                    raise ValueError('Товар не найден')
                if old['mode'] != p['mode']:
                    raise ValueError('Режим существующего товара менять нельзя; создайте новый вариант')
                db.execute('UPDATE products SET ' + ','.join(k+'=?' for k in fields) + ' WHERE id=?',
                           values+[old['emoji_id'] if emoji_id is None else emoji_id,product_id])
            else:
                product_id = db.execute('INSERT INTO products('+','.join(fields)+') VALUES(?,?,?,?,?,?,?,?)',
                                        values+[emoji_id or '']).lastrowid
                db.execute('UPDATE products SET news_announced=0 WHERE id=?',(product_id,))
            if 'cost_price' in p:
                db.execute('UPDATE products SET cost_price=? WHERE id=?',(p['cost_price'],product_id))
            if 'old_price' not in card:
                # A price raised above the stored "was" price would show a fake discount.
                db.execute('UPDATE products SET old_price=NULL WHERE id=? AND old_price<=price',(product_id,))
            if card:
                db.execute('UPDATE products SET '+','.join(k+'=?' for k in card)+' WHERE id=?',[*card.values(),product_id])
            self.log(db, actor, 'product.save', product_id)
            from broadcasts import Broadcasts
            Broadcasts(self).auto(db,product_id,actor=actor)
            return product_id

    def delete_product(self, product_id, actor='admin'):
        # Keep the row and stock referenced by historical or already invoiced orders.
        with self.tx() as db:
            product = db.execute('SELECT deleted FROM products WHERE id=?',(product_id,)).fetchone()
            if not product:
                raise ValueError('Товар не найден')
            if not product['deleted']:
                db.execute('UPDATE products SET deleted=1,active=0 WHERE id=?',(product_id,))
                self.log(db,actor,'product.delete',product_id)
        return {'ok':True}

    @staticmethod
    def validate_cost(amount):
        if amount is not None and (type(amount) is not int or not 0<=amount<=10**12):
            raise ValueError('Себестоимость должна быть положительной суммой или нулём, не более двух знаков после точки')

    def import_stock(self, product_id, lines, actor='admin', unit_cost=None):
        self.validate_cost(unit_cost)
        if not isinstance(lines, str):raise ValueError('Загрузите позиции текстом, по одной в строке')
        items = list(dict.fromkeys(x.strip() for x in lines.splitlines() if x.strip()))
        if any(len(x) > 2000 for x in items):
            raise ValueError('Максимум 2000 символов в одной позиции')
        added = 0
        with self.tx(invalidate=False) as db:
            p = db.execute('SELECT mode,cost_price FROM products WHERE id=? AND deleted=0', (product_id,)).fetchone()
            if not p or p['mode'] != 'auto':
                raise ValueError('Нужен товар с автоматической выдачей')
            cost = p['cost_price'] if unit_cost is None else unit_cost
            for item in items:
                added += db.execute('INSERT OR IGNORE INTO stock(product_id,fingerprint,secret,unit_cost) VALUES(?,?,?,?)',
                                    (product_id,self.fingerprint(item),self.encrypt(item),cost)).rowcount
            self.log(db, actor, 'stock.import:'+str(added), product_id,details={'added':added,'duplicates':len(items)-added})
            if added:
                from broadcasts import Broadcasts
                Broadcasts(self).auto(db,product_id,added,actor)
        return {'added': added, 'duplicates': len(items)-added}

    def set_stock_cost(self, product_id, unit_cost, actor):
        self.validate_cost(unit_cost)
        if unit_cost is None:raise ValueError('Укажите себестоимость единицы')
        with self.tx(invalidate=False) as db:
            self.expire(db,_store=self)
            if not db.execute("SELECT 1 FROM products WHERE id=? AND deleted=0 AND mode='auto'",(product_id,)).fetchone():
                raise ValueError('Товар с автовыдачей не найден')
            count = db.execute('UPDATE stock SET unit_cost=? WHERE product_id=? AND order_id IS NULL',(unit_cost,product_id)).rowcount
            self.log(db,actor,'stock.cost',product_id,details={'amount':unit_cost,'quantity':count})
            return {'updated':count}

    @staticmethod
    def _page(page, minimum=1):
        if isinstance(page, bool):raise ValueError('Некорректный номер страницы')
        try:return max(minimum,int(page))
        except (TypeError,ValueError):raise ValueError('Некорректный номер страницы') from None

    def stock_items(self, product_id, status='', page=1):
        if status not in ('','free','reserved'):raise ValueError('Некорректный фильтр стока')
        page = self._page(page)
        now = time.time()
        with self.read() as db:
            product = db.execute('SELECT name,mode FROM products WHERE id=? AND deleted=0',(product_id,)).fetchone()
            if not product or product['mode']!='auto':raise ValueError('Товар с автовыдачей не найден')
            # A lapsed reservation counts as free even before housekeeping releases it.
            free = '(s.order_id IS NULL OR o.expires<:now)'
            # Sold rows are skipped by an index-only test against the (few) pending orders.
            base = (" FROM stock s LEFT JOIN orders o ON o.id=s.order_id WHERE s.product_id=:pid"
                    " AND (s.order_id IS NULL OR s.order_id IN (SELECT id FROM orders WHERE status='pending'))")
            params = {'pid':product_id,'now':now}
            counts = db.execute('SELECT count(*) total,sum(CASE WHEN '+free+' THEN 1 ELSE 0 END) free'+base,params).fetchone()
            condition = ' AND '+free if status=='free' else ' AND NOT '+free if status=='reserved' else ''
            total = db.execute('SELECT count(*)'+base+condition,params).fetchone()[0]
            pages = max(1,(total+24)//25);page = min(page,pages)
            items = [dict(row) for row in db.execute(
                'SELECT s.id,CASE WHEN '+free+' THEN NULL ELSE s.order_id END order_id,s.unit_cost,'
                'CASE WHEN '+free+' THEN NULL ELSE o.expires END expires,'
                "CASE WHEN "+free+" THEN 'free' ELSE 'reserved' END status"+base+condition+' ORDER BY s.id DESC LIMIT 25 OFFSET :offset',
                dict(params,offset=(page-1)*25))]
            free_count = counts['free'] or 0
            return dict(name=product['name'],items=items,total=total,page=page,pages=pages,free=free_count,reserved=counts['total']-free_count)

    def reveal_stock(self, product_id, stock_id, actor):
        with self.tx(invalidate=False) as db:
            row = db.execute("""SELECT s.secret FROM stock s JOIN products p ON p.id=s.product_id
                LEFT JOIN orders o ON o.id=s.order_id WHERE s.product_id=? AND s.id=? AND p.deleted=0
                AND (s.order_id IS NULL OR o.status='pending')""",(product_id,stock_id)).fetchone()
            if not row:raise ValueError('Позиция уже продана или недоступна. Обновите сток.')
            self.log(db,actor,'stock.reveal',product_id)
            return {'content':self.decrypt(row['secret'])}

    # ------------------------------------------------------------------ orders

    @staticmethod
    def expire(db, now=None, _store=None):
        """Release reservations of pending orders whose time ran out (approved Stars checkouts
        get CHECKOUT_SECONDS from approval). Returns the number of expired orders."""
        now = time.time() if now is None else now
        expired = [row[0] for row in db.execute("SELECT id FROM orders WHERE status='pending' AND expires<?",(now,))]
        for start in range(0,len(expired),500):
            chunk = expired[start:start+500]
            marks = ','.join('?'*len(chunk))
            db.execute('UPDATE stock SET order_id=NULL WHERE order_id IN ('+marks+')',chunk)
            db.execute("UPDATE orders SET status='expired' WHERE status='pending' AND id IN ("+marks+')',chunk)
        for oid in expired:Store.log(db,'system','order.expired',oid)
        return len(expired)

    def housekeeping(self):
        """Periodic maintenance (the bot worker calls it every ~30 s); each step is bounded."""
        now = time.time()
        with self.tx(invalidate=False) as db:
            expired = self.expire(db,now,self)
            stale = now-INPUT_KEEP_SECONDS
            inputs = db.execute("""DELETE FROM meta WHERE key>='input_mode:' AND key<'input_mode;' AND (value=''
                OR EXISTS(SELECT 1 FROM telegram_users t WHERE t.user_id=CAST(substr(meta.key,12) AS INTEGER) AND t.last_seen<?))""",
                (stale,)).rowcount
            inputs += db.execute("""DELETE FROM meta WHERE key>='crypto_input:' AND key<'crypto_input;' AND (
                EXISTS(SELECT 1 FROM crypto_deposits d WHERE d.id=meta.value AND (d.txid IS NOT NULL OR d.cancelled IS NOT NULL OR d.expires<?))
                OR NOT EXISTS(SELECT 1 FROM crypto_deposits d WHERE d.id=meta.value))""",(now-86400,)).rowcount
            # Exact-balance idempotency now lives in wallet_ledger.reference; drop the old copies.
            inputs += db.execute("DELETE FROM meta WHERE key>='set:' AND key<'set;'").rowcount
            rows = db.execute("""SELECT o.id FROM outbox o LEFT JOIN orders r ON r.id=o.order_id
                WHERE o.purged=0 AND (o.sent=1 OR o.failed IS NOT NULL)
                AND coalesce(o.sent_at,o.failed,r.fulfilled_at,r.paid_at,r.created,0)<? LIMIT 200""",(now-OUTBOX_KEEP_SECONDS,)).fetchall()
            # resend() rebuilds the message from orders.result, so the copy is not needed.
            for row in rows:
                db.execute('UPDATE outbox SET body=?,purged=1 WHERE id=?',(self.encrypt(''),row[0]))
            inputs += db.execute('DELETE FROM user_state WHERE expires<?',(now,)).rowcount
            inputs += db.execute('DELETE FROM user_state WHERE updated<?',(now-USER_STATE_KEEP_SECONDS,)).rowcount
            audit = self._prune_audit(db,now)
            self._prune_live(db,now)
            verify = db.execute("SELECT value FROM meta WHERE key='wallet.verified_at'").fetchone()
            verify = not verify or now-float(verify[0])>=WALLET_VERIFY_SECONDS
            if verify:
                db.execute("INSERT INTO meta VALUES('wallet.verified_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(now),))
        repaired = 0
        if verify and self.verify_wallet_balances():
            # Snapshot read found a difference: re-check and repair under the write lock.
            repaired = len(self.verify_wallet_balances(repair=True))
        return {'expired':expired,'inputs':inputs,'outbox':len(rows),'audit':audit,'wallet_repaired':repaired}

    @staticmethod
    def _prune_audit(db, now):
        """Delete info events of prunable categories older than the retention period. A cursor
        (meta audit.pruned_until) bounds every run to AUDIT_PRUNE_BATCH examined rows; warnings,
        errors and money-related events are never deleted."""
        days = audit_retention_days()
        if not days:return 0
        cutoff = now-days*86400
        row = db.execute("SELECT value FROM meta WHERE key='audit.pruned_until'").fetchone()
        start = float(row[0]) if row else 0.0
        if start>=cutoff:return 0
        batch = db.execute('SELECT id,at,level,category,action FROM audit WHERE at>=? AND at<? ORDER BY at LIMIT ?',
                           (start,cutoff,AUDIT_PRUNE_BATCH)).fetchall()
        doomed = [row['id'] for row in batch if row['level']=='info' and row['category'] in AUDIT_PRUNABLE_CATEGORIES
                  and not row['action'].startswith('payment.')]
        for index in range(0,len(doomed),500):
            chunk = doomed[index:index+500]
            db.execute('DELETE FROM audit WHERE id IN ('+','.join('?'*len(chunk))+')',chunk)
        if len(batch)<AUDIT_PRUNE_BATCH:cursor = cutoff
        else:cursor = max(batch[-1]['at'],start+1e-6)  # rows sharing the last timestamp are re-read once
        db.execute("INSERT INTO meta VALUES('audit.pruned_until',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(repr(cursor),))
        return len(doomed)

    def create_order(self, product_id, user_id, quantity=1):
        """Reserve stock and create an unpaid order (or return the user's live order for the
        same product and quantity). Promo codes credit the wallet instead (promo.py, 3.2);
        subtotal/discount_cents/promo_id stay on orders created by 3.0–3.1."""
        if type(quantity) is not int or not 1<=quantity<=100:
            raise ValueError('Количество: целое число от 1 до 100.')
        with self.tx(invalidate=False) as db:
            now = time.time()
            self.expire(db,now,self)
            p = db.execute('SELECT * FROM products WHERE id=? AND active=1 AND deleted=0', (product_id,)).fetchone()
            if not p:
                raise ValueError('Товар недоступен')
            total = p['price']*quantity
            rate = db.execute("SELECT value FROM meta WHERE key='stars_per_usd'").fetchone()
            stars = cents_to_stars(total,rate[0]) if rate else total
            reused = self._reuse_order(db,p,user_id,quantity,total,stars,now)
            if reused:return reused
            if db.execute("SELECT count(*) FROM orders WHERE user_id=? AND status='pending'", (user_id,)).fetchone()[0] >= 3:
                raise ValueError(PENDING_LIMIT_MESSAGE)
            stock = []
            if p['mode'] == 'auto':
                stock = db.execute('SELECT id FROM stock WHERE product_id=? AND order_id IS NULL ORDER BY id LIMIT ?', (product_id,quantity)).fetchall()
                if not stock:
                    raise ValueError('Товар закончился')
                if len(stock)<quantity:
                    raise ValueError('Недостаточно товара в наличии. Уменьшите количество.')
            oid = uuid.uuid4().hex
            db.execute('''INSERT INTO orders(id,user_id,product_id,title,price,mode,instruction,created,expires,quantity,payment_stars,
              subtotal,discount_cents) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)''',
              (oid,user_id,product_id,emoji_text.plain(p['name']),total,p['mode'],emoji_text.plain(p['instruction']),now,now+RESERVATION_SECONDS,quantity,stars,
               total))
            if stock:
                db.executemany('UPDATE stock SET order_id=? WHERE id=?', [(oid,item['id']) for item in stock])
            self.log(db,'telegram','order.created',oid)
            return dict(db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone())

    @staticmethod
    def _reusable_order(db, user_id, product_id, quantity, now):
        return db.execute("""SELECT * FROM orders o WHERE user_id=? AND product_id=? AND quantity=? AND status='pending'
            AND checkout=0 AND expires>=? AND charge IS NULL
            AND NOT EXISTS(SELECT 1 FROM crypto_deposits d WHERE d.order_id=o.id)
            ORDER BY created DESC LIMIT 1""",(user_id,product_id,quantity,now)).fetchone()

    def reusable_order_id(self, user_id, product_id, quantity):
        """Id of the live unpaid order create_order() would reuse for this item, or None."""
        with self.read() as db:
            row = self._reusable_order(db,user_id,product_id,quantity,time.time())
        return row['id'] if row else None

    def _reuse_order(self, db, product, user_id, quantity, total, stars, now):
        """Return the user's live unpaid order for the same item instead of reserving twice."""
        row = self._reusable_order(db,user_id,product['id'],quantity,now)
        if not row:return None
        if ((row['price'],row['payment_stars'],row['title'],row['instruction'],row['promo_id'])
                !=(total,stars,emoji_text.plain(product['name']),emoji_text.plain(product['instruction']),None)):
            # The product changed since (or a 3.1 order still carries a discount): release
            # the stale reservation and create a fresh order.
            db.execute('UPDATE stock SET order_id=NULL WHERE order_id=?',(row['id'],))
            db.execute("UPDATE orders SET status='expired' WHERE id=?",(row['id'],))
            self.log(db,'system','order.expired',row['id'])
            return None
        db.execute('UPDATE orders SET expires=max(expires,?) WHERE id=?',(now+RESERVATION_SECONDS,row['id']))
        return dict(db.execute('SELECT * FROM orders WHERE id=?',(row['id'],)).fetchone())

    def approve(self, oid, user_id, amount, currency):
        with self.tx(invalidate=False) as db:
            now = time.time()
            o = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
            expected = None if not o else o['payment_stars'] if o['payment_stars'] is not None else o['price']
            if (not o or o['user_id'] != user_id or expected != amount or currency != 'XTR'
                    or o['status'] != 'pending' or o['expires'] < now):
                raise ValueError('Счёт недействителен. Откройте каталог и создайте новый заказ.')
            if not o['checkout']:
                # Telegram may deliver the payment a little later: keep the stock for an hour.
                db.execute('UPDATE orders SET checkout=1,expires=max(expires,?) WHERE id=?', (now+CHECKOUT_SECONDS,oid))
                self.log(db,'telegram','order.checkout',oid)

    def paid(self, oid, user_id, amount, currency, charge, wallet=False, _db=None):
        if _db is not None:
            return self._paid(_db,oid,user_id,amount,currency,charge,wallet)
        try:
            with self.tx(invalidate=False) as db:
                return self._paid(db,oid,user_id,amount,currency,charge,wallet)
        except (ValueError, sqlite3.IntegrityError) as exc:
            if wallet:raise  # balance payment: no external money moved
            detail = str(exc) if isinstance(exc, ValueError) else 'Платёж уже использован'
            self._record_payment_issue('order',oid,user_id,amount,currency,charge,detail)
            if isinstance(exc, sqlite3.IntegrityError):
                raise ValueError('Платёж уже использован: требуется проверка') from None
            raise

    def _paid(self, db, oid, user_id, amount, currency, charge, wallet=False):
        now = time.time()
        if not wallet:self.expire(db,now,self)
        o = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        if wallet and o:
            expected = o['price']
        elif o:
            expected = o['payment_stars'] if o['payment_stars'] is not None else o['price']
        else:
            expected = None
        if not o or o['user_id'] != user_id or expected != amount or currency != 'XTR':
            raise ValueError('Платёж не соответствует заказу')
        if o['charge']:
            if o['charge'] != charge:
                raise ValueError('Повторная оплата заказа: требуется проверка')
            return
        if not charge:
            raise ValueError('Платёж без идентификатора: требуется проверка')
        if db.execute('SELECT 1 FROM topups WHERE charge=?',(charge,)).fetchone():
            raise ValueError('Платёж уже использован для пополнения')
        if wallet:
            if o['checkout']:
                raise ValueError('Заказ уже оплачивается через Telegram Stars. Дождитесь завершения или создайте новый заказ.')
            if o['status'] != 'pending' or o['expires'] < now:
                raise ValueError('Резерв истёк. Выберите товар заново.')
            balance = self._balance(db,user_id)
            if balance < amount:
                raise ValueError('Недостаточно средств. Пополните кошелёк.')
            db.execute('INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(?,?,?,?)',
                       (user_id,-amount,'order:'+oid,now))
        language = self.language(user_id,_db=db)
        if o['mode'] == 'auto':
            items = db.execute('SELECT secret,unit_cost FROM stock WHERE order_id=? ORDER BY id', (oid,)).fetchall()
            complete = len(items)==o['quantity']
            cost_total = sum(item['unit_cost'] for item in items) if complete and all(item['unit_cost'] is not None for item in items) else None
            if not complete:
                # Never allocate a replacement blindly after an expired reservation.
                status, result = 'needs_review', None
                body = tr('Оплата получена. Заказ {0} передан на проверку поддержки.',oid[:8],language=language)
            else:
                if len(items)==1:
                    result = items[0]['secret']
                else:
                    result = self.encrypt('\n\n'.join(str(i)+'. '+self.decrypt(item['secret']) for i,item in enumerate(items,1)))
                status = 'fulfilled'
                body = self.delivery_body(o,self.decrypt(result),_db=db)
        else:
            unit_cost = db.execute('SELECT cost_price FROM products WHERE id=?',(o['product_id'],)).fetchone()[0]
            cost_total = unit_cost*o['quantity'] if unit_cost is not None else None
            status, result = 'awaiting_details', None
            body = tr('✅ Заказ {0} оплачен.\n\nНажми «✍️ Отправить данные» ниже и пришли одним сообщением всё, что нужно для выполнения. Пароли отправлять не нужно.\n\nМожно и командой: /details {1} твои данные',
                      oid[:8],oid,language=language)+'\n'+o['title']+' × '+str(o['quantity'])
        db.execute('UPDATE orders SET charge=?,status=?,result=?,cost_total=?,cost_locked=1,paid_at=?,fulfilled_at=? WHERE id=?',
                   (charge,status,result,cost_total,now,now if status=='fulfilled' else None,oid))
        db.execute('INSERT INTO outbox(order_id,user_id,body) VALUES(?,?,?)', (oid,user_id,self.encrypt(body)))
        self.log(db,'telegram','payment.accepted',oid,details={'method':'balance' if wallet else 'Stars'})
        if status=='needs_review':self.log(db,'system','delivery.review',oid,level='error',details={'state':'needs_review'})
        usd = db.execute("SELECT 1 FROM meta WHERE key='stars_per_usd'").fetchone()
        money = (lambda cents:format_usd(cents)) if usd else (lambda stars:str(stars)+' Stars')
        lines = ['💰 Оплачен заказ '+oid[:8],o['title']+' × '+str(o['quantity']),
                 'Сумма: '+money(o['price'])+(' (с баланса)' if wallet else ''),'Покупатель: '+str(user_id)]
        if o['mode']=='manual':lines.append('Ручной заказ: ждём данные от покупателя')
        if status=='needs_review':lines.append('⚠️ Сток не выдан: требуется проверка')
        self._notify(db,'new_paid_order','\n'.join(lines),key=oid)
        if o['mode']=='auto' and status=='fulfilled':self._low_stock(db,o['product_id'])

    def _notify(self, db, kind, text, key=''):
        """Queue a staff notification (notify.Notify.emit) inside the caller's transaction.
        Optional: never raises and never undoes the caller's work."""
        try:
            module = _optional('notify')
        except Exception:  # noqa: BLE001
            log_.exception('notify module failed to import');return 0
        if module is None or not hasattr(module,'Notify'):return 0
        db.execute('SAVEPOINT notify_emit')
        try:
            notify = module.Notify(self)
            if key and 'key' in inspect.signature(notify.emit).parameters:
                result = notify.emit(db,kind,text,key=key)
            else:
                result = notify.emit(db,kind,text)
            db.execute('RELEASE notify_emit')
            return result
        except Exception:  # noqa: BLE001
            db.execute('ROLLBACK TO notify_emit');db.execute('RELEASE notify_emit')
            log_.exception('Could not queue the %s notification', kind)
            return 0

    def _low_stock(self, db, product_id):
        """After a sale: notify.Notify.check_low_stock when available, else a plain alert at
        LOW_STOCK_THRESHOLD free units (notify.py rate-limits per kind)."""
        self._live_stock_check(db,product_id)
        module = _optional('notify')
        if module is None or not hasattr(module,'Notify'):return
        check = getattr(module.Notify(self),'check_low_stock',None)
        if check is not None:
            db.execute('SAVEPOINT low_stock')
            try:
                check(db,product_id)
                db.execute('RELEASE low_stock')
            except Exception:  # noqa: BLE001
                db.execute('ROLLBACK TO low_stock');db.execute('RELEASE low_stock')
                log_.exception('Low-stock check failed for product %s', product_id)
            return
        free = db.execute('SELECT count(*) FROM stock WHERE product_id=? AND order_id IS NULL',(product_id,)).fetchone()[0]
        if free<=LOW_STOCK_THRESHOLD:
            name = db.execute('SELECT name FROM products WHERE id=?',(product_id,)).fetchone()[0]
            self._notify(db,'low_stock',('⛔ Товар закончился: ' if not free else '⚠️ Заканчивается товар: ')+name
                         +'\nОсталось: '+str(free)+' шт.',key='product:'+str(product_id))

    def _live_stock_check(self, db, product_id):
        """Panel live feed: never undoes or fails the sale that triggered it."""
        db.execute('SAVEPOINT live_stock')
        try:
            self._live_low_stock(db,product_id)
            db.execute('RELEASE live_stock')
        except Exception:  # noqa: BLE001
            db.execute('ROLLBACK TO live_stock');db.execute('RELEASE live_stock')
            log_.exception('Live low-stock check failed for product %s', product_id)

    def delivery_body(self, order, result, _db=None):
        with (nullcontext(_db) if _db is not None else self.read()) as db:
            dollars = db.execute("SELECT 1 FROM meta WHERE key='stars_per_usd'").fetchone()
            amount = format_usd(order['price']) if dollars else str(order['price'])+' Stars'
            receipt = tr('✅ Покупка успешно завершена!\n\nТовар: {0} (x{1})\nСписано: {2}',
                order['title'],order['quantity'],amount,language=self.language(order['user_id'],_db=db))
            return receipt+'\n\n'+result+('\n\n'+order['instruction'] if order['instruction'] else '')

    def purchase_delivery(self, user_id, order_id):
        with self.read() as db:
            order = db.execute("""SELECT * FROM orders WHERE id=? AND user_id=?
                AND status='fulfilled' AND charge IS NOT NULL AND result IS NOT NULL""",(order_id,user_id)).fetchone()
            if not order:
                raise ValueError('Покупка не найдена')
            return self.delivery_body(order,self.decrypt(order['result']),_db=db)

    # ------------------------------------------------------------------ wallet

    def wallet(self, user_id):
        with self.read() as db:
            balance = self._balance(db,user_id)
            history = [dict(r) for r in db.execute('SELECT delta,created,reference FROM wallet_ledger WHERE user_id=? ORDER BY id DESC LIMIT 5',(user_id,))]
        for item in history:item['kind'] = wallet_kind(item['reference'])
        return {'balance':balance,'history':history}

    def create_topup(self, user_id, amount):
        if type(amount) is not int or not 1 <= amount <= 10000:
            raise ValueError('Сумма пополнения: от 1 до 10 000 Stars, целым числом.')
        tid = 'topup:'+uuid.uuid4().hex
        with self.tx(invalidate=False) as db:
            now = time.time()
            rate = db.execute("SELECT value FROM meta WHERE key='stars_per_usd'").fetchone()
            cents = credit_cents(amount,rate[0]) if rate else amount
            if cents<1:raise ValueError('Сумма пополнения после конвертации меньше $0.01')
            open_count = db.execute('SELECT count(*) FROM topups WHERE user_id=? AND charge IS NULL AND created>? AND coalesce(expires,created+?)>?',
                                    (user_id,now-TOPUP_SECONDS,TOPUP_SECONDS,now)).fetchone()[0]
            if open_count>=TOPUP_OPEN_LIMIT:
                raise ValueError('Слишком много неоплаченных счетов пополнения. Оплатите созданный счёт или попробуйте через час.')
            db.execute('INSERT INTO topups(id,user_id,amount,created,credit_cents,expires) VALUES(?,?,?,?,?,?)',
                       (tid,user_id,amount,now,cents,now+TOPUP_SECONDS))
        return tid

    def topup_info(self, tid):
        with self.read() as db:
            row = db.execute('SELECT amount,credit_cents FROM topups WHERE id=?',(tid,)).fetchone()
            if not row:raise ValueError('Счёт не найден')
            return dict(row)

    def approve_topup(self, tid, user_id, amount, currency):
        with self.read() as db:
            row = db.execute('SELECT * FROM topups WHERE id=?',(tid,)).fetchone()
        if not row or row['user_id'] != user_id or row['amount'] != amount or currency != 'XTR' or row['charge']:
            raise ValueError('Счёт пополнения недействителен. Создайте новый в кошельке.')
        expires = row['expires'] if row['expires'] is not None else row['created']+TOPUP_SECONDS
        if expires < time.time():
            raise ValueError('Счёт пополнения истёк. Создайте новый в кошельке.')

    def credit_topup(self, tid, user_id, amount, currency, charge):
        # Expired invoices are still credited: Telegram already took the Stars.
        try:
            with self.tx(invalidate=False) as db:
                return self._credit_topup(db,tid,user_id,amount,currency,charge)
        except (ValueError, sqlite3.IntegrityError) as exc:
            detail = str(exc) if isinstance(exc, ValueError) else 'Платёж уже использован'
            self._record_payment_issue('topup',tid,user_id,amount,currency,charge,detail)
            if isinstance(exc, sqlite3.IntegrityError):
                raise ValueError('Платёж уже использован: требуется проверка') from None
            raise

    def _credit_topup(self, db, tid, user_id, amount, currency, charge):
        row = db.execute('SELECT * FROM topups WHERE id=?',(tid,)).fetchone()
        if not row or row['user_id'] != user_id or row['amount'] != amount or currency != 'XTR':
            raise ValueError('Платёж не соответствует пополнению')
        if row['charge']:
            if row['charge'] != charge:
                raise ValueError('Повторная оплата: обратитесь в поддержку')
            return False
        if not charge or db.execute('SELECT 1 FROM orders WHERE charge=?',(charge,)).fetchone():
            raise ValueError('Платёж уже использован либо отсутствует идентификатор')
        db.execute('UPDATE topups SET charge=? WHERE id=?',(charge,tid))
        db.execute('INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(?,?,?,?)',
                   (user_id,row['credit_cents'] if row['credit_cents'] is not None else amount,tid,time.time()))
        self.log(db,'telegram','wallet.credit:'+str(amount),tid)
        return True

    def pay_wallet(self, oid, user_id):
        with self.tx(invalidate=False) as db:
            row = db.execute('SELECT price FROM orders WHERE id=? AND user_id=?',(oid,user_id)).fetchone()
            if not row:
                raise ValueError('Заказ не найден')
            self._paid(db,oid,user_id,row['price'],'XTR','wallet:'+oid,wallet=True)

    def details(self, oid, user_id, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Отправьте данные для выполнения текстом после команды')
        if len(text)>4000:raise ValueError('Данные для выполнения: не длиннее 4000 символов')
        with self.tx(invalidate=False) as db:
            changed = db.execute("UPDATE orders SET details=?,status='queued' WHERE id=? AND user_id=? AND status='awaiting_details'",
                                 (self.encrypt(text),oid,user_id)).rowcount
            if not changed:
                raise ValueError('Заказ не ожидает данных')
            self.log(db,'telegram','order.details',oid)
            order = db.execute('SELECT title,quantity FROM orders WHERE id=?',(oid,)).fetchone()
            # The customer's data itself is never copied into a notification.
            self._notify(db,'manual_order_details','✍️ Получены данные по заказу '+oid[:8]+'\n'+order['title']+' × '
                         +str(order['quantity'])+'\nВозьмите заказ в работу в панели: «Заказы».',key=oid)

    # Lapsed reservations read as expired even before housekeeping runs.
    ORDER_STATUS_SQL = "CASE WHEN status='pending' AND expires<? THEN 'expired' ELSE status END AS status"

    def orders(self, user_id=None):
        with self.read() as db:
            sql = 'SELECT id,user_id,product_id,title,price,quantity,mode,'+self.ORDER_STATUS_SQL+',created,manager FROM orders'
            params = (time.time(),user_id) if user_id else (time.time(),)
            return [dict(r) for r in db.execute(sql+(' WHERE user_id=?' if user_id else '')+' ORDER BY created DESC LIMIT 300', params)]

    def purchases(self, user_id, page=0):
        page_size = 5
        page = self._page(page,0)
        with self.read() as db:
            condition = "user_id=? AND status='fulfilled' AND charge IS NOT NULL"
            # The partial index holds exactly this user's purchases (the planner may prefer a
            # status index that walks every fulfilled order otherwise).
            source = 'FROM orders INDEXED BY orders_purchases WHERE '
            total = db.execute('SELECT count(*) '+source+condition,(user_id,)).fetchone()[0]
            pages = max(1,(total+page_size-1)//page_size)
            page = min(page,pages-1)
            items = [dict(row) for row in db.execute(
                'SELECT id,title,price,quantity,created '+source+condition+
                ' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',(user_id,page_size,page*page_size))]
            return dict(items=items,total=total,page=page,pages=pages)

    def purchase(self, user_id, order_id):
        with self.read() as db:
            row = db.execute("""SELECT id,title,price,quantity,created FROM orders
                WHERE id=? AND user_id=? AND status='fulfilled' AND charge IS NOT NULL""",
                (order_id,user_id)).fetchone()
            if not row:
                raise ValueError('Покупка не найдена')
            return dict(row)

    def detail(self, oid, actor):
        with self.tx(invalidate=False) as db:
            o = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
            if not o:
                raise ValueError('Заказ не найден')
            self.log(db, actor, 'order.details.view', oid)
            return {'details':self.decrypt(o['details']), 'instruction':o['instruction']}

    def claim(self, oid, actor):
        with self.tx(invalidate=False) as db:
            if not db.execute("UPDATE orders SET status='processing',manager=? WHERE id=? AND status='queued'", (actor,oid)).rowcount:
                raise ValueError('Заказ уже взят или не готов к работе')
            self.log(db,actor,'order.claim',oid)

    @staticmethod
    def _queue_delivery(db, oid, user_id, body):
        db.execute('''INSERT INTO outbox(order_id,user_id,body) VALUES(?,?,?) ON CONFLICT(order_id)
          DO UPDATE SET body=excluded.body,sent=0,attempts=0,next_try=0,failed=NULL,sent_at=NULL,purged=0''',(oid,user_id,body))

    def complete(self, oid, result, actor):
        if not isinstance(result, str) or not result.strip():raise ValueError('Укажите результат выполнения')
        with self.tx(invalidate=False) as db:
            o = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
            if not o or o['status'] != 'processing' or o['manager'] != actor:
                raise ValueError('Сначала возьмите заказ в работу под своим именем')
            db.execute("UPDATE orders SET status='fulfilled',result=?,fulfilled_at=? WHERE id=?", (self.encrypt(result),time.time(),oid))
            self._queue_delivery(db,oid,o['user_id'],self.encrypt(self.delivery_body(o,result,_db=db)))
            self.log(db,actor,'order.complete',oid)

    def deliveries(self):
        with self.read() as db:
            return [dict(r) for r in db.execute('SELECT * FROM outbox WHERE sent=0 AND failed IS NULL AND next_try<? ORDER BY next_try LIMIT 20', (time.time(),))]

    def delivery_result(self, delivery_id, ok, permanent=False):
        """ok: delivered. Otherwise retry with exponential backoff; give up after about a day,
        or at once when permanent (bot blocked, chat not found): one error event then."""
        with self.tx(invalidate=False) as db:
            row = db.execute('SELECT order_id,sent,attempts,failed FROM outbox WHERE id=?',(delivery_id,)).fetchone()
            if not row or row['sent'] or row['failed']:return
            now = time.time()
            attempts = row['attempts']+1
            if ok:
                db.execute('UPDATE outbox SET sent=1,attempts=?,sent_at=? WHERE id=?',(attempts,now,delivery_id))
                self.log(db,'bot','delivery.sent',row['order_id'],details={'attempt':attempts})
            elif permanent or attempts>=RETRY_GIVE_UP_ATTEMPTS:
                db.execute('UPDATE outbox SET attempts=?,failed=? WHERE id=?',(attempts,now,delivery_id))
                self.log(db,'bot','delivery.failed',row['order_id'],level='error',
                         details={'attempt':attempts,'state':'blocked' if permanent else 'gave_up'})
            else:
                db.execute('UPDATE outbox SET attempts=?,next_try=? WHERE id=?',(attempts,now+retry_delay(attempts),delivery_id))
                if attempts==1:self.log(db,'bot','delivery.retry',row['order_id'],details={'attempt':attempts})

    def audits(self):
        with self.read() as db:
            return [dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 150')]

    @staticmethod
    def _event_search(query):
        """SQL condition for an events search. Order ids (hex prefix) and Telegram IDs use
        indexes; other text is matched per column, with Cyrillic-aware matching of labels."""
        query = query.strip()[:200]
        lowered = query.lower()
        if re.fullmatch(r'[0-9a-f]{8,32}',lowered) and not lowered.isdigit():
            return '((order_id>=? AND order_id<?) OR (target>=? AND target<?))',[lowered,lowered+'~']*2
        if re.fullmatch(r'[0-9]{5,19}',query):
            return '(user_id=? OR target=? OR (order_id>=? AND order_id<?))',[int(query),query,query,query+'~']
        pattern = '%'+_escape_like(query)+'%'
        folded = query.casefold()
        labels = sorted({label for label in AUDIT_LABELS.values() if folded in label.casefold()})
        options = ["summary LIKE ? ESCAPE '\\'","actor LIKE ? ESCAPE '\\'","action LIKE ? ESCAPE '\\'",
                   "target LIKE ? ESCAPE '\\'","details LIKE ? ESCAPE '\\'"]
        values = [pattern]*len(options)
        if labels:
            options.insert(0,'summary IN ('+','.join('?'*len(labels))+')');values = labels+values
        return '('+' OR '.join(options)+')',values

    def events(self, query='', category='', level='', after=0, before=0, page=1, page_size=25):
        if category not in ('','orders','payments','balance','products','system','settings','finance','support') or level not in ('','info','warning','error'):
            raise ValueError('Некорректный фильтр событий')
        if not isinstance(query, str):raise ValueError('Некорректный поиск')
        page = self._page(page)
        clauses = [];values = []
        for column, value in [('category',category),('level',level)]:
            if value:clauses.append(column+'=?');values.append(value)
        if after:clauses.append('at>=?');values.append(after)
        if before:clauses.append('at<?');values.append(before)
        query = query.strip()
        if query:
            condition, extra = self._event_search(query)
            clauses.append(condition);values += extra
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.read() as db:
            if query:
                # One pass collects the matching ids (newest first); a second count/page scan
                # of an unindexed text search would double the cost.
                ids = [row[0] for row in db.execute('SELECT id FROM audit'+where+' ORDER BY id DESC',values)]
                total = len(ids)
                pages = max(1,(total+page_size-1)//page_size);page = min(page,pages)
                chosen = ids[(page-1)*page_size:page*page_size]
                items = [dict(r) for r in db.execute('SELECT * FROM audit WHERE id IN ('+','.join('?'*len(chosen))+') ORDER BY id DESC',
                                                     chosen)] if chosen else []
                return dict(items=items,total=total,page=page,pages=pages)
            total = db.execute('SELECT count(*) FROM audit'+where,values).fetchone()[0]
            pages = max(1,(total+page_size-1)//page_size);page = min(page,pages)
            items = [dict(r) for r in db.execute('SELECT * FROM audit'+where+' ORDER BY id DESC LIMIT ? OFFSET ?',values+[page_size,(page-1)*page_size])]
            return dict(items=items,total=total,page=page,pages=pages)

    def offset(self, value=None):
        with self.read() as db:
            row = db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        current = int(row[0]) if row else 0
        if value is None or (row and int(value)==current):return current
        with self.tx(invalidate=False) as db:
            db.execute("INSERT INTO meta VALUES('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))
        return int(value)

    def language(self, user_id, _db=None):
        with (nullcontext(_db) if _db is not None else self.read()) as db:
            row = db.execute('SELECT language FROM user_languages WHERE user_id=?',(user_id,)).fetchone()
            return row['language'] if row else ''

    def set_language(self, user_id, language):
        if type(user_id) is not int or user_id<=0 or language not in ('ru','en'):
            raise ValueError('Unsupported language or user')
        with self.tx(invalidate=False) as db:
            db.execute('''INSERT INTO user_languages VALUES(?,?)
              ON CONFLICT(user_id) DO UPDATE SET language=excluded.language''',(user_id,language))

    def remember_customer(self, user_id, username='', first_name='', last_name=''):
        if type(user_id) is not int or user_id<=0:
            raise ValueError('Неверный Telegram ID')
        now = time.time()
        with self.tx(invalidate=False) as db:
            from channel_gate import register
            register(db,user_id)
            db.execute('''INSERT INTO telegram_users(user_id,username,first_name,last_name,first_seen,last_seen)
              VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,first_name=excluded.first_name,last_name=excluded.last_name,
              last_seen=max(telegram_users.last_seen,excluded.last_seen)''',
              (user_id,username or '',first_name or '',last_name or '',now,now))
            db.execute('UPDATE news_preferences SET blocked=0 WHERE user_id=?',(user_id,))

    # ------------------------------------------------------------------ customers & metrics

    def customers(self):
        with self.read() as db:
            return [dict(r) for r in db.execute('''SELECT c.*,'''+CUSTOMER_TOTALS+''' FROM (
              SELECT '''+CUSTOMER_COLUMNS+''' FROM telegram_users t) c ORDER BY c.first_seen DESC,c.user_id''')]

    def customers_page(self, query='', page=1, page_size=50, tag='', banned=False):
        """tag: only customers with this CRM tag (case-insensitive); banned: only blocked ones.
        Items carry their tags and banned flag."""
        if not isinstance(query, str) or not isinstance(tag, str):raise ValueError('Некорректный поиск')
        page = self._page(page)
        if type(page_size) is not int or not 1<=page_size<=200:raise ValueError('Некорректный размер страницы')
        query = query.strip()[:100]
        clauses, params = [], []
        if query:
            needle = query.removeprefix('@')
            pattern = '%'+_escape_like(needle)+'%'
            clauses.append("""(CAST(t.user_id AS TEXT) LIKE ? ESCAPE '\\' OR t.username LIKE ? ESCAPE '\\'
                OR (t.first_name||' '||t.last_name) LIKE ? ESCAPE '\\')""")
            params = [pattern]*3
        tag = ' '.join(tag.split())[:CUSTOMER_TAG_LENGTH]
        if tag:
            clauses.append('t.user_id IN (SELECT user_id FROM customer_tags WHERE tag=?)');params.append(tag)
        if banned:
            clauses.append('t.user_id IN (SELECT user_id FROM customer_meta WHERE banned=1)')
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.read() as db:
            total = db.execute('SELECT count(*) FROM telegram_users t'+where,params).fetchone()[0]
            pages = max(1,(total+page_size-1)//page_size);page = min(page,pages)
            items = [dict(r) for r in db.execute('''SELECT c.*,'''+CUSTOMER_TOTALS+''' FROM (
              SELECT '''+CUSTOMER_COLUMNS+''' FROM telegram_users t'''+where+'''
              ORDER BY t.first_seen DESC,t.user_id LIMIT ? OFFSET ?) c ORDER BY c.first_seen DESC,c.user_id''',
              params+[page_size,(page-1)*page_size])]
            meta = self.customers_meta([item['user_id'] for item in items],_db=db)
        for item in items:item.update(meta[item['user_id']])
        return dict(items=items,total=total,page=page,pages=pages)

    ORDER_PAGE_STATUSES = ('','pending','expired','awaiting_details','queued','processing','fulfilled','needs_review',
                           'paid','attention')

    def orders_page(self, status='', query='', page=1, page_size=50):
        """Panel order list, filtered and paginated in SQL (newest first).
        status: a stored status, 'pending'/'expired' (lapsed reservations count as expired),
        'paid' (any paid order) or 'attention' (paid, not yet fulfilled). query: order id or its
        prefix, Telegram ID, @username or a part of the product title."""
        if status not in self.ORDER_PAGE_STATUSES:raise ValueError('Некорректный фильтр заказов')
        if not isinstance(query, str):raise ValueError('Некорректный поиск')
        page = self._page(page)
        if type(page_size) is not int or not 1<=page_size<=200:raise ValueError('Некорректный размер страницы')
        now = time.time()
        clauses, params = [], []
        if status=='pending':clauses.append("o.status='pending' AND o.expires>=?");params.append(now)
        elif status=='expired':clauses.append("(o.status='expired' OR (o.status='pending' AND o.expires<?))");params.append(now)
        elif status=='paid':clauses.append('o.charge IS NOT NULL')
        elif status=='attention':clauses.append("o.status IN ('awaiting_details','queued','processing','needs_review')")
        elif status:clauses.append('o.status=?');params.append(status)
        query = query.strip()[:100]
        if query:
            options, values = [], []
            text = query.removeprefix('#').lower()
            if re.fullmatch(r'[0-9a-f]{4,32}',text):
                options.append('(o.id>=? AND o.id<?)');values += [text,text+'~']
            if re.fullmatch(r'[0-9]{1,19}',query):
                options.append('o.user_id=?');values.append(int(query))
            if query.startswith('@') and len(query)>1:
                options.append("o.user_id IN (SELECT user_id FROM telegram_users WHERE username LIKE ? ESCAPE '\\')")
                values.append('%'+_escape_like(query[1:])+'%')
            else:
                options.append("o.title LIKE ? ESCAPE '\\'");values.append('%'+_escape_like(query)+'%')
            clauses.append('('+' OR '.join(options)+')');params += values
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.read() as db:
            total = db.execute('SELECT count(*) FROM orders o'+where,params).fetchone()[0]
            pages = max(1,(total+page_size-1)//page_size);page = min(page,pages)
            items = [dict(row) for row in db.execute('''SELECT o.id,o.user_id,o.product_id,o.title,o.price,o.quantity,o.mode,
                CASE WHEN o.status='pending' AND o.expires<? THEN 'expired' ELSE o.status END AS status,o.created,o.expires,
                o.manager,o.paid_at,o.fulfilled_at,o.charge IS NOT NULL AS paid,o.subtotal,o.discount_cents,o.promo_id,
                coalesce(t.username,'') username,coalesce(t.first_name,'') first_name
                FROM (SELECT * FROM orders o'''+where+''' ORDER BY o.created DESC,o.id DESC LIMIT ? OFFSET ?) o
                LEFT JOIN telegram_users t ON t.user_id=o.user_id ORDER BY o.created DESC,o.id DESC''',
                [now,*params,page_size,(page-1)*page_size])]
        for item in items:item['paid'] = bool(item['paid'])
        return dict(items=items,total=total,page=page,pages=pages)

    def metrics(self):
        with self.read() as db:
            result = dict(db.execute('''SELECT count(*) orders,
              coalesce(sum(CASE WHEN charge IS NOT NULL THEN price ELSE 0 END),0) revenue,
              sum(CASE WHEN charge IS NOT NULL THEN 1 ELSE 0 END) paid,
              sum(CASE WHEN charge IS NOT NULL AND status!='fulfilled' THEN 1 ELSE 0 END) attention
              FROM orders''').fetchone())
            result['wallet_total'] = db.execute('SELECT coalesce(sum(balance),0) FROM wallet_balances').fetchone()[0]
            result['delivery_pending'] = db.execute('SELECT count(*) FROM outbox WHERE sent=0 AND failed IS NULL').fetchone()[0]
            result['delivery_failed'] = db.execute('SELECT count(*) FROM outbox WHERE sent=0 AND failed IS NOT NULL').fetchone()[0]
            result['payment_issues'] = db.execute('SELECT count(*) FROM payment_issues WHERE resolved_at IS NULL').fetchone()[0]
            return {k:v or 0 for k,v in result.items()}

    @staticmethod
    def _balance_reason(reason, reference):
        if not isinstance(reason, str) or not reason.strip() or len(reason)>300:
            raise ValueError('Укажите причину (до 300 символов)')
        if not isinstance(reference, str) or not reference or len(reference)>100:
            raise ValueError('Некорректный идентификатор операции')
        return reason.strip()

    def adjust_wallet(self, user_id, delta, reason, reference, actor):
        if type(delta) is not int or delta == 0 or abs(delta)>10**12:
            raise ValueError('Некорректная сумма изменения баланса')
        reason = self._balance_reason(reason, reference)
        reference = 'admin:'+reference
        with self.tx(invalidate=False) as db:
            old = db.execute('SELECT user_id,delta FROM wallet_ledger WHERE reference=?',(reference,)).fetchone()
            if old:
                if old['user_id']!=user_id or old['delta']!=delta:
                    raise ValueError('Идентификатор операции уже использован')
                return
            balance = self._balance(db,user_id)
            if balance+delta<0:
                raise ValueError('Списание превышает баланс покупателя')
            db.execute('INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(?,?,?,?)',(user_id,delta,reference,time.time()))
            self.log(db,actor,'wallet.adjust:'+str(delta)+'; '+reason,user_id,details={'before':balance,'after':balance+delta,'reason':reason})

    def set_wallet(self, user_id, amount, reason, reference, actor):
        if type(amount) is not int or not 0<=amount<=10**12:
            raise ValueError('Баланс должен быть от $0 до $10 000 000 000, не более двух знаков после точки')
        reason = self._balance_reason(reason, reference)
        reference = 'set:'+reference
        with self.tx(invalidate=False) as db:
            old = db.execute('SELECT id,user_id FROM wallet_ledger WHERE reference=?',(reference,)).fetchone()
            if old:
                # The balance right after that entry is the amount it set.
                was = db.execute('SELECT coalesce(sum(delta),0) FROM wallet_ledger WHERE user_id=? AND id<=?',(old['user_id'],old['id'])).fetchone()[0]
                if old['user_id']!=user_id or was!=amount:raise ValueError('Операция уже использована с другой суммой')
                return
            balance = self._balance(db,user_id)
            db.execute('INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(?,?,?,?)',(user_id,amount-balance,reference,time.time()))
            self.log(db,actor,'wallet.set:'+str(amount)+'; '+reason,user_id,details={'before':balance,'after':amount,'reason':reason})

    def order_info(self, oid, actor):
        with self.tx(invalidate=False) as db:
            row = db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
            if not row:
                raise ValueError('Заказ не найден')
            o = dict(row)
            if o['status']=='pending' and o['expires']<time.time():o['status'] = 'expired'
            o['details'] = self.decrypt(o['details'])
            o['result'] = self.decrypt(o['result'])
            delivery = db.execute('SELECT sent,attempts,next_try,failed FROM outbox WHERE order_id=?',(oid,)).fetchone()
            o['delivery'] = dict(delivery) if delivery else None
            self.log(db,actor,'order.view',oid)
            o['events'] = [dict(r) for r in db.execute('SELECT * FROM audit WHERE order_id=? OR target=? ORDER BY id',(oid,oid))]
            return o

    def resend(self, oid, actor):
        with self.tx(invalidate=False) as db:
            o = db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
            if not o or o['status']!='fulfilled' or not o['result']:
                raise ValueError('Повторная выдача доступна только для выполненного заказа')
            body = self.delivery_body(o,self.decrypt(o['result']),_db=db)
            self._queue_delivery(db,oid,o['user_id'],self.encrypt(body))
            self.log(db,actor,'order.resend',oid)

    # ------------------------------------------------------------------ 3.1: customer CRM

    @staticmethod
    def _customer_exists(db, user_id):
        if type(user_id) is not int or not 0<user_id<2**63:raise ValueError('Неверный Telegram ID')
        if not db.execute('SELECT 1 FROM telegram_users WHERE user_id=?',(user_id,)).fetchone():
            raise ValueError('Покупатель не найден')

    @staticmethod
    def _meta_row(db, user_id, now):
        db.execute('INSERT INTO customer_meta(user_id,updated) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET updated=excluded.updated',
                   (user_id,now))

    @staticmethod
    def normalize_tags(tags):
        """→ list of clean tags: trimmed, inner spaces collapsed, case-insensitive duplicates dropped."""
        if not isinstance(tags,(list,tuple)):raise ValueError('Метки: ожидается список')
        result, seen = [], set()
        for tag in tags:
            if not isinstance(tag,str):raise ValueError('Метка должна быть текстом')
            tag = ' '.join(tag.split())
            if not tag:continue
            if len(tag)>CUSTOMER_TAG_LENGTH or any(ord(c)<32 for c in tag):
                raise ValueError('Метка: не длиннее '+str(CUSTOMER_TAG_LENGTH)+' символов')
            if tag.casefold() in seen:continue
            seen.add(tag.casefold());result.append(tag)
        if len(result)>CUSTOMER_TAGS_MAX:raise ValueError('Не больше '+str(CUSTOMER_TAGS_MAX)+' меток у покупателя')
        return result

    def customer_meta(self, user_id, _db=None):
        with (nullcontext(_db) if _db is not None else self.read()) as db:
            row = db.execute('SELECT * FROM customer_meta WHERE user_id=?',(user_id,)).fetchone()
            tags = [r[0] for r in db.execute('SELECT tag FROM customer_tags WHERE user_id=? ORDER BY position,tag',(user_id,))]
        meta = dict(row) if row else {'user_id':user_id,'note':'','banned':0,'ban_reason':'','banned_at':None,'banned_by':'','updated':None}
        meta['banned'] = bool(meta['banned']);meta['tags'] = tags
        return meta

    def customers_meta(self, user_ids, _db=None):
        """{user_id: {'tags': [...], 'banned': bool}} for a page of customers (≤ 500 ids)."""
        ids = [uid for uid in user_ids if type(uid) is int][:500]
        result = {uid:{'tags':[],'banned':False} for uid in ids}
        if not ids:return result
        marks = ','.join('?'*len(ids))
        with (nullcontext(_db) if _db is not None else self.read()) as db:
            for row in db.execute('SELECT user_id,tag FROM customer_tags WHERE user_id IN ('+marks+') ORDER BY user_id,position,tag',ids):
                result[row[0]]['tags'].append(row[1])
            for row in db.execute('SELECT user_id FROM customer_meta WHERE banned=1 AND user_id IN ('+marks+')',ids):
                result[row[0]]['banned'] = True
        return result

    def customer_names(self, user_ids):
        """{user_id: 'Имя Фамилия' or '@username' or ''} for a handful of ids (live feed)."""
        ids = sorted({uid for uid in user_ids if type(uid) is int})[:500]
        if not ids:return {}
        with self.read() as db:
            rows = db.execute('SELECT user_id,username,first_name,last_name FROM telegram_users WHERE user_id IN ('
                              +','.join('?'*len(ids))+')',ids).fetchall()
        return {row['user_id']:' '.join(filter(None,[row['first_name'],row['last_name']])).strip()
                or ('@'+row['username'] if row['username'] else '') for row in rows}

    def customer_tags(self, limit=100):
        """Tags in use with customer counts (filter chips), most used first."""
        with self.read() as db:
            return [{'tag':row[0],'count':row[1]} for row in db.execute(
                'SELECT tag,count(*) n FROM customer_tags GROUP BY tag ORDER BY n DESC,tag LIMIT ?',(limit,))]

    def set_customer_tags(self, user_id, tags, actor):
        tags = self.normalize_tags(tags)
        now = time.time()
        with self.tx(invalidate=False) as db:
            self._customer_exists(db,user_id)
            before = [r[0] for r in db.execute('SELECT tag FROM customer_tags WHERE user_id=? ORDER BY position,tag',(user_id,))]
            if before!=tags:
                db.execute('DELETE FROM customer_tags WHERE user_id=?',(user_id,))
                db.executemany('INSERT INTO customer_tags(user_id,tag,position) VALUES(?,?,?)',[(user_id,tag,i) for i,tag in enumerate(tags)])
                self._meta_row(db,user_id,now)
                self.log(db,actor,'customer.tags',user_id,user_id=user_id,details={'before':', '.join(before),'after':', '.join(tags)})
            return self.customer_meta(user_id,_db=db)

    def set_customer_note(self, user_id, note, actor):
        if not isinstance(note,str):raise ValueError('Заметка должна быть текстом')
        note = note.strip()
        if len(note)>CUSTOMER_NOTE_LENGTH:raise ValueError('Заметка: не длиннее '+str(CUSTOMER_NOTE_LENGTH)+' символов')
        now = time.time()
        with self.tx(invalidate=False) as db:
            self._customer_exists(db,user_id)
            self._meta_row(db,user_id,now)
            db.execute('UPDATE customer_meta SET note=? WHERE user_id=?',(note,user_id))
            # The note is internal text: the journal only records that it changed.
            self.log(db,actor,'customer.note',user_id,user_id=user_id,details={'state':'cleared' if not note else 'saved'})
            return self.customer_meta(user_id,_db=db)

    def ban_customer(self, user_id, reason, actor):
        if not isinstance(reason,str) or not reason.strip() or len(reason.strip())>BAN_REASON_LENGTH:
            raise ValueError('Укажите причину блокировки (до '+str(BAN_REASON_LENGTH)+' символов)')
        reason = reason.strip();now = time.time()
        with self.tx(invalidate=False) as db:
            self._customer_exists(db,user_id)
            self._meta_row(db,user_id,now)
            db.execute('UPDATE customer_meta SET banned=1,ban_reason=?,banned_at=?,banned_by=? WHERE user_id=?',(reason,now,str(actor)[:64],user_id))
            self.log(db,actor,'customer.ban',user_id,level='warning',user_id=user_id,details={'reason':reason})
            meta = self.customer_meta(user_id,_db=db)
        self._forget_bans()
        return meta

    def unban_customer(self, user_id, actor):
        now = time.time()
        with self.tx(invalidate=False) as db:
            self._customer_exists(db,user_id)
            row = db.execute('SELECT banned FROM customer_meta WHERE user_id=?',(user_id,)).fetchone()
            if row and row['banned']:
                db.execute("UPDATE customer_meta SET banned=0,ban_reason='',banned_at=NULL,banned_by='',updated=? WHERE user_id=?",(now,user_id))
                self.log(db,actor,'customer.unban',user_id,level='warning',user_id=user_id)
            meta = self.customer_meta(user_id,_db=db)
        self._forget_bans()
        return meta

    def _forget_bans(self):
        with _BANNED_LOCK:_BANNED.pop(self._cache_id,None)

    def is_banned(self, user_id):
        """Bot hot path: the (small) set of banned ids is cached per process for ≤ BAN_CACHE_SECONDS,
        so a ban saved by the panel reaches the bot process within 5 s."""
        now = time.monotonic()
        with _BANNED_LOCK:
            hit = _BANNED.get(self._cache_id)
        if not hit or hit[0]<=now:
            with self.read() as db:
                banned = frozenset(row[0] for row in db.execute('SELECT user_id FROM customer_meta WHERE banned=1'))
            hit = (now+BAN_CACHE_SECONDS,banned)
            with _BANNED_LOCK:_BANNED[self._cache_id] = hit
        return user_id in hit[1]

    # ------------------------------------------------------------------ 3.1: order notes

    def order_notes(self, order_id):
        with self.read() as db:
            if not db.execute('SELECT 1 FROM orders WHERE id=?',(order_id,)).fetchone():raise ValueError('Заказ не найден')
            return [dict(row) for row in db.execute('''SELECT id,order_id,author,text,created FROM order_notes
                WHERE order_id=? ORDER BY id LIMIT ?''',(order_id,ORDER_NOTES_MAX))]

    def add_order_note(self, order_id, text, actor):
        if not isinstance(text,str) or not text.strip():raise ValueError('Введите текст заметки')
        text = text.strip()
        if len(text)>ORDER_NOTE_LENGTH:raise ValueError('Заметка: не длиннее '+str(ORDER_NOTE_LENGTH)+' символов')
        now = time.time()
        with self.tx(invalidate=False) as db:
            if not db.execute('SELECT 1 FROM orders WHERE id=?',(order_id,)).fetchone():raise ValueError('Заказ не найден')
            if db.execute('SELECT count(*) FROM order_notes WHERE order_id=?',(order_id,)).fetchone()[0]>=ORDER_NOTES_MAX:
                raise ValueError('У заказа уже '+str(ORDER_NOTES_MAX)+' заметок')
            note_id = db.execute('INSERT INTO order_notes(order_id,author,text,created) VALUES(?,?,?,?)',
                                 (order_id,str(actor)[:64],text,now)).lastrowid
            self.log(db,actor,'order.note',order_id)
            return dict(db.execute('SELECT id,order_id,author,text,created FROM order_notes WHERE id=?',(note_id,)).fetchone())

    # ------------------------------------------------------------------ 3.1: support reply templates

    @staticmethod
    def _template_values(title, text):
        if not isinstance(title,str) or not title.strip():raise ValueError('Укажите название шаблона')
        if not isinstance(text,str) or not text.strip():raise ValueError('Введите текст шаблона')
        title, text = ' '.join(title.split()), text.strip()
        if len(title)>TEMPLATE_TITLE_LENGTH:raise ValueError('Название шаблона: не длиннее '+str(TEMPLATE_TITLE_LENGTH)+' символов')
        if len(text)>TEMPLATE_TEXT_LENGTH:raise ValueError('Текст шаблона: не длиннее '+str(TEMPLATE_TEXT_LENGTH)+' символов')
        return title, text

    def support_templates(self):
        with self.read() as db:
            return [dict(row) for row in db.execute('SELECT * FROM support_templates ORDER BY title COLLATE NOCASE,id LIMIT ?',(TEMPLATES_MAX,))]

    def save_support_template(self, title, text, actor, template_id=None):
        title, text = self._template_values(title, text)
        now = time.time()
        with self.tx(invalidate=False) as db:
            if template_id is None:
                if db.execute('SELECT count(*) FROM support_templates').fetchone()[0]>=TEMPLATES_MAX:
                    raise ValueError('Не больше '+str(TEMPLATES_MAX)+' шаблонов')
                template_id = db.execute('INSERT INTO support_templates(title,text,author,created,updated) VALUES(?,?,?,?,?)',
                                         (title,text,str(actor)[:64],now,now)).lastrowid
                self.log(db,actor,'support.template.create',template_id,details={'title':title})
            else:
                if not db.execute('UPDATE support_templates SET title=?,text=?,updated=? WHERE id=?',(title,text,now,template_id)).rowcount:
                    raise ValueError('Шаблон не найден')
                self.log(db,actor,'support.template.update',template_id,details={'title':title})
            return dict(db.execute('SELECT * FROM support_templates WHERE id=?',(template_id,)).fetchone())

    def delete_support_template(self, template_id, actor):
        with self.tx(invalidate=False) as db:
            row = db.execute('SELECT title FROM support_templates WHERE id=?',(template_id,)).fetchone()
            if not row:raise ValueError('Шаблон не найден')
            db.execute('DELETE FROM support_templates WHERE id=?',(template_id,))
            self.log(db,actor,'support.template.delete',template_id,details={'title':row['title']})
        return {'ok':True}

    # ------------------------------------------------------------------ 3.1: product bulk actions

    BULK_ACTIONS = ('show','hide','delete','category','price_percent')

    def bulk_products(self, product_ids, action, value, actor):
        """One transaction for all selected products; every change is journaled per product.
        price_percent: −90…+500 %, rounded half-up to whole price units (cents in USD), never below 1."""
        if action not in self.BULK_ACTIONS:raise ValueError('Неизвестное массовое действие')
        if not isinstance(product_ids,(list,tuple)) or not product_ids:raise ValueError('Выберите хотя бы один товар')
        if len(product_ids)>BULK_MAX:raise ValueError('Не больше '+str(BULK_MAX)+' товаров за раз')
        if any(type(pid) is not int or not 0<pid<2**63 for pid in product_ids):raise ValueError('Некорректный список товаров')
        ids = sorted(set(product_ids))
        if action=='category':
            if not isinstance(value,str) or not value.strip():raise ValueError('Укажите категорию')
            value = value.strip()
            if len(value)>80 or '\n' in value:raise ValueError('Категория: не длиннее 80 символов')
        elif action=='price_percent':
            if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)) or value!=value or not -90<=value<=500:
                raise ValueError('Изменение цены: от −90 % до +500 %')
            if value==0:raise ValueError('Укажите процент изменения цены')
            factor = (100+Decimal(str(value)))/100
        changed = 0
        with self.tx() as db:  # invalidates the cached catalog
            rows = {row['id']:row for row in db.execute('SELECT * FROM products WHERE deleted=0 AND id IN ('+','.join('?'*len(ids))+')',ids)}
            missing = [pid for pid in ids if pid not in rows]
            if missing:raise ValueError('Товар не найден: '+', '.join(map(str,missing[:5])))
            for pid in ids:
                row = rows[pid]
                if action in ('show','hide'):
                    active = int(action=='show')
                    if row['active']==active:continue
                    db.execute('UPDATE products SET active=? WHERE id=?',(active,pid))
                    self.log(db,actor,'product.bulk',pid,details={'state':action})
                    if active:
                        from broadcasts import Broadcasts
                        Broadcasts(self).auto(db,pid,actor=actor)
                elif action=='delete':
                    db.execute('UPDATE products SET deleted=1,active=0 WHERE id=?',(pid,))
                    self.log(db,actor,'product.delete',pid,details={'state':'bulk'})
                elif action=='category':
                    if row['category']==value:continue
                    sort = db.execute('SELECT coalesce(max(sort),0)+1 FROM products WHERE category=? AND deleted=0',(value,)).fetchone()[0]
                    db.execute('UPDATE products SET category=?,sort=? WHERE id=?',(value,sort,pid))
                    self.log(db,actor,'product.bulk',pid,details={'state':'category','before':row['category'],'after':value})
                else:
                    price = int((Decimal(row['price'])*factor).quantize(Decimal(1),ROUND_HALF_UP))
                    price = min(max(1,price),10**10)
                    if price==row['price']:continue
                    db.execute('UPDATE products SET price=? WHERE id=?',(price,pid))
                    # A price raised above the stored "was" price would show a fake discount.
                    db.execute('UPDATE products SET old_price=NULL WHERE id=? AND old_price<=price',(pid,))
                    self.log(db,actor,'product.bulk',pid,details={'state':'price','before':row['price'],'after':price})
                changed += 1
        return {'ok':True,'changed':changed,'total':len(ids)}

    # ------------------------------------------------------------------ 3.1: live activity feed

    def _live_low_stock(self, db, product_id):
        """Feed entry when free stock reaches the low-stock threshold (≤ once per product per 6 h)."""
        product = db.execute("SELECT id,name FROM products WHERE id=? AND active=1 AND deleted=0 AND mode='auto'",(product_id,)).fetchone()
        if not product:return
        free = db.execute('SELECT count(*) FROM stock WHERE product_id=? AND order_id IS NULL',(product_id,)).fetchone()[0]
        limit = LOW_STOCK_THRESHOLD
        module = _optional('notify')
        if module is not None and hasattr(module,'Notify'):
            notifier = module.Notify(self)
            if hasattr(notifier,'threshold') and hasattr(notifier,'settings'):
                limit = notifier.threshold(notifier.settings(db),product_id)
        if free>limit:return
        now = time.time();key = 'live.low_stock:'+str(product_id)
        last = db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        if last and now-float(last[0])<LIVE_LOW_STOCK_SECONDS:return
        db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,repr(now)))
        db.execute("INSERT INTO live_events(at,kind,ref,data) VALUES(?,'stock.low',?,?)",
                   (now,str(product_id),json.dumps({'title':product['name'],'quantity':free,'threshold':limit},ensure_ascii=False)))

    def live_max_id(self):
        with self.read() as db:
            return db.execute('SELECT coalesce(max(id),0) FROM live_events').fetchone()[0]

    def live_since(self, after_id, limit=200):
        """Feed rows newer than after_id, oldest first (the panel's poller fans them out)."""
        with self.read() as db:
            return [dict(row) for row in db.execute('SELECT * FROM live_events WHERE id>? ORDER BY id LIMIT ?',(after_id,limit))]

    def live_recent(self, kinds, limit=30, after_id=0):
        """Newest rows of the given kinds, newest first."""
        kinds = list(kinds)
        if not kinds:return []
        with self.read() as db:
            return [dict(row) for row in db.execute('SELECT * FROM live_events WHERE id>? AND kind IN ('+','.join('?'*len(kinds))+
                ') ORDER BY id DESC LIMIT ?',(after_id,*kinds,limit))]

    def live_unread(self, kinds, seen_id, cap=100):
        kinds = list(kinds)
        if not kinds:return 0
        with self.read() as db:
            return db.execute('SELECT count(*) FROM (SELECT 1 FROM live_events WHERE id>? AND kind IN ('+','.join('?'*len(kinds))+
                ') LIMIT ?)',(seen_id,*kinds,cap)).fetchone()[0]

    def live_seen(self, username):
        with self.read() as db:
            row = db.execute('SELECT seen_id FROM live_seen WHERE username=?',(username,)).fetchone()
        return row[0] if row else 0

    def set_live_seen(self, username, event_id):
        if type(event_id) is not int or event_id<0:raise ValueError('Некорректное событие')
        with self.tx(invalidate=False) as db:
            event_id = min(event_id,db.execute('SELECT coalesce(max(id),0) FROM live_events').fetchone()[0])
            db.execute('''INSERT INTO live_seen(username,seen_id,updated) VALUES(?,?,?) ON CONFLICT(username)
                DO UPDATE SET seen_id=max(seen_id,excluded.seen_id),updated=excluded.updated''',(username,event_id,time.time()))
            return db.execute('SELECT seen_id FROM live_seen WHERE username=?',(username,)).fetchone()[0]

    @staticmethod
    def _prune_live(db, now):
        return db.execute('DELETE FROM live_events WHERE id IN (SELECT id FROM live_events WHERE at<? ORDER BY at LIMIT ?)',
                          (now-LIVE_KEEP_SECONDS,LIVE_PRUNE_BATCH)).rowcount

    # ------------------------------------------------------------------ 3.1: monthly revenue goal

    def revenue_goal(self):
        with self.read() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?',(REVENUE_GOAL_KEY,)).fetchone()
        try:return int(row[0]) if row else 0
        except ValueError:return 0

    def set_revenue_goal(self, amount, actor):
        if type(amount) is not int or not 0<=amount<=10**12:raise ValueError('Цель: сумма от 0 до 10 000 000 000')
        with self.tx(invalidate=False) as db:
            before = db.execute('SELECT value FROM meta WHERE key=?',(REVENUE_GOAL_KEY,)).fetchone()
            db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(REVENUE_GOAL_KEY,str(amount)))
            self.log(db,actor,'insights.goal','revenue',details={'before':int(before[0]) if before else 0,'after':amount})
        return amount
