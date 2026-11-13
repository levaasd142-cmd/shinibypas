"""Staff notifications: panel-editable settings, a durable encrypted queue the bot sends from,
per-kind dedupe and rate limits, and a once-a-day summary (Moscow time)."""
from datetime import datetime, time as dtime, timedelta, timezone
import json
import os
import re
import time

import emoji_text
from money import format_usd

KINDS = ('new_paid_order','manual_order_details','support_message','low_stock','payment_issue','errors',
         'daily_summary','test')
TOGGLES = ('new_paid_order','manual_order_details','support_message','low_stock','payment_issue','errors','daily_summary')
# Same key (e.g. one product, one ticket) is announced at most once per window.
COOLDOWNS = {'low_stock':6*3600,'support_message':600,'payment_issue':300,'errors':900}
# Per kind and hour; further alerts are dropped and counted.
HOURLY_LIMITS = {'new_paid_order':200,'manual_order_details':200,'support_message':60,'low_stock':60,
                 'payment_issue':60,'errors':20,'daily_summary':5,'test':10}
MAX_TEXT = 3500
MAX_CHATS = 20
KEEP_SECONDS = 7*86400
PRUNE_EVERY = 3600
SETTINGS_KEY = 'notify.settings'
SUMMARY_KEY = 'notify.summary_date'
TIME_RE = re.compile(r'([01][0-9]|2[0-3]):[0-5][0-9]')

try:
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo('Europe/Moscow')
except Exception:  # no tz database (Windows without tzdata): Moscow is UTC+3 all year
    MSK = timezone(timedelta(hours=3))


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS notify_queue(
        id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, kind TEXT NOT NULL, body BLOB NOT NULL,
        created REAL NOT NULL, sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        next_try REAL NOT NULL DEFAULT 0, failed REAL, sent_at REAL)''')
    db.execute('CREATE INDEX IF NOT EXISTS notify_due ON notify_queue(next_try) WHERE sent=0 AND failed IS NULL')
    db.execute('CREATE INDEX IF NOT EXISTS notify_created ON notify_queue(created)')
    db.execute('CREATE TABLE IF NOT EXISTS notify_dedupe(key TEXT PRIMARY KEY, last REAL NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS notify_dedupe_last ON notify_dedupe(last)')
    db.execute('''CREATE TABLE IF NOT EXISTS notify_rate(kind TEXT PRIMARY KEY, window_start REAL NOT NULL,
        count INTEGER NOT NULL DEFAULT 0, dropped INTEGER NOT NULL DEFAULT 0)''')


def env_chat_ids():
    result = []
    for part in re.split(r'[,\s;]+',os.getenv('ADMIN_CHAT_ID','').strip()):
        if re.fullmatch(r'-?[0-9]{1,16}',part) and int(part) and int(part) not in result:result.append(int(part))
    return result[:MAX_CHATS]


def defaults():
    return dict(admin_chat_ids=env_chat_ids(),new_paid_order=True,manual_order_details=True,support_message=True,
                low_stock=True,low_stock_threshold=3,low_stock_overrides={},payment_issue=True,errors=True,
                daily_summary=True,daily_summary_time='09:00')


def validate(data):
    if not isinstance(data,dict):raise ValueError('Некорректные настройки уведомлений')
    result = defaults()
    chats = data.get('admin_chat_ids',result['admin_chat_ids'])
    if isinstance(chats,str):chats = [part for part in re.split(r'[,\s;]+',chats.strip()) if part]
    try:
        clean = []
        for chat in chats:
            if isinstance(chat,bool):raise ValueError
            value = int(chat)
            if not value or abs(value)>=10**16:raise ValueError
            if value not in clean:clean.append(value)
    except (TypeError,ValueError):
        raise ValueError('ID чатов администраторов: целые числа (Telegram ID), через запятую') from None
    if len(clean)>MAX_CHATS:raise ValueError('Не более 20 чатов для уведомлений')
    result['admin_chat_ids'] = clean
    for key in TOGGLES:
        if key in data:result[key] = bool(data[key])
    threshold = data.get('low_stock_threshold',result['low_stock_threshold'])
    if type(threshold) is not int or not 0<=threshold<=10000:raise ValueError('Порог остатка: целое число от 0 до 10000')
    result['low_stock_threshold'] = threshold
    overrides = data.get('low_stock_overrides') or {}
    if not isinstance(overrides,dict) or len(overrides)>1000:raise ValueError('Некорректные пороги остатка по товарам')
    clean = {}
    for key,value in overrides.items():
        if not str(key).isdigit() or type(value) is not int or not 0<=value<=10000:
            raise ValueError('Порог остатка: целое число от 0 до 10000')
        clean[str(int(key))] = value
    result['low_stock_overrides'] = clean
    when = data.get('daily_summary_time',result['daily_summary_time'])
    if not isinstance(when,str) or not TIME_RE.fullmatch(when.strip()):raise ValueError('Время сводки: ЧЧ:ММ (по Москве)')
    result['daily_summary_time'] = when.strip()
    return result


def _money(db, amount):
    usd = db.execute("SELECT 1 FROM meta WHERE key='stars_per_usd'").fetchone()
    return format_usd(amount) if usd else str(amount)+' Stars'


def _has_table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())


class Notify:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------ settings

    def settings(self, db=None):
        if db is None:
            with self.store.read() as db:return self.settings(db)
        row = db.execute('SELECT value FROM meta WHERE key=?',(SETTINGS_KEY,)).fetchone()
        result = defaults()
        if row:
            try:result.update(json.loads(row[0]))
            except ValueError:pass
        return result

    def save(self, settings, actor):
        clean = validate(settings)
        with self.store.tx(invalidate=False) as db:
            db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (SETTINGS_KEY,json.dumps(clean,sort_keys=True)))
            self.store.log(db,actor,'notify.settings','notify')
        return clean

    # ------------------------------------------------------------------ producers

    def emit(self, db, kind, text, *, key='', cooldown=None):
        """Queue `text` for every admin chat. Call inside the producer's transaction.
        Returns the number of queued messages (0: disabled, deduplicated or rate-limited)."""
        if kind not in KINDS:raise ValueError('Неизвестный тип уведомления')
        settings = self.settings(db)
        if kind in TOGGLES and not settings.get(kind):return 0
        chats = settings.get('admin_chat_ids') or []
        if not chats:return 0
        now = time.time()
        window = COOLDOWNS.get(kind,0) if cooldown is None else cooldown
        if key and window:
            dedupe = kind+':'+str(key)[:200]
            row = db.execute('SELECT last FROM notify_dedupe WHERE key=?',(dedupe,)).fetchone()
            if row and now-row['last']<window:return 0
            db.execute('INSERT INTO notify_dedupe(key,last) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET last=excluded.last',(dedupe,now))
        rate = db.execute('SELECT * FROM notify_rate WHERE kind=?',(kind,)).fetchone()
        if not rate or now-rate['window_start']>=3600:
            db.execute('''INSERT INTO notify_rate(kind,window_start,count,dropped) VALUES(?,?,1,0)
                ON CONFLICT(kind) DO UPDATE SET window_start=excluded.window_start,count=1''',(kind,now))
        elif rate['count']>=HOURLY_LIMITS.get(kind,60):
            db.execute('UPDATE notify_rate SET dropped=dropped+1 WHERE kind=?',(kind,))
            return 0
        else:
            db.execute('UPDATE notify_rate SET count=count+1 WHERE kind=?',(kind,))
        text = str(text)
        if len(text)>MAX_TEXT:text = text[:MAX_TEXT-1]+'…'
        body = self.store.encrypt(text)
        db.executemany('INSERT INTO notify_queue(chat_id,kind,body,created) VALUES(?,?,?,?)',[(chat,kind,body,now) for chat in chats])
        return len(chats)

    def notify(self, kind, text, *, key='', cooldown=None):
        """emit() in its own transaction."""
        with self.store.tx(invalidate=False) as db:
            return self.emit(db,kind,text,key=key,cooldown=cooldown)

    def threshold(self, settings, product_id):
        return settings.get('low_stock_overrides',{}).get(str(product_id),settings.get('low_stock_threshold',3))

    def check_low_stock(self, db, product_id):
        """After a sale: alert when an auto-delivery product's free stock is at or below its
        threshold (at most once per product per 6 h). Returns True when an alert was queued."""
        settings = self.settings(db)
        if not settings.get('low_stock'):return False
        product = db.execute("SELECT id,name FROM products WHERE id=? AND active=1 AND deleted=0 AND mode='auto'",(product_id,)).fetchone()
        if not product:return False
        free = db.execute('SELECT count(*) FROM stock WHERE product_id=? AND order_id IS NULL',(product_id,)).fetchone()[0]
        limit = self.threshold(settings,product_id)
        if free>limit:return False
        text = ('⛔ Товар закончился: ' if not free else '⚠️ Заканчивается товар: ')+emoji_text.plain(product['name'])+'\nОсталось: '+str(free)+' шт. (порог '+str(limit)+')'
        return bool(self.emit(db,'low_stock',text,key='product:'+str(product_id)))

    def send_test(self, actor):
        with self.store.tx(invalidate=False) as db:
            count = self.emit(db,'test','✅ Тестовое уведомление TheMarket')
            if not count:raise ValueError('Укажите хотя бы один чат администратора')
            self.store.log(db,actor,'notify.test','notify')
            return count

    # ------------------------------------------------------------------ queue (bot)

    def due(self, limit=20):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('Некорректный размер пакета')
        with self.store.read() as db:
            rows = db.execute('''SELECT id,chat_id,kind,body,attempts FROM notify_queue WHERE sent=0 AND failed IS NULL
                AND next_try<=? ORDER BY next_try,id LIMIT ?''',(time.time(),limit)).fetchall()
            return [{'id':r['id'],'chat_id':r['chat_id'],'kind':r['kind'],'attempts':r['attempts'],
                     'text':self.store.decrypt(r['body'])} for r in rows]

    def result(self, notification_id, ok, permanent=False):
        from store import RETRY_GIVE_UP_ATTEMPTS, retry_delay
        with self.store.tx(invalidate=False) as db:
            row = db.execute('SELECT * FROM notify_queue WHERE id=?',(notification_id,)).fetchone()
            if not row or row['sent'] or row['failed']:return None
            now = time.time();attempts = row['attempts']+1
            if ok:
                db.execute('UPDATE notify_queue SET sent=1,attempts=?,sent_at=? WHERE id=?',(attempts,now,notification_id))
                return 'sent'
            # Admin alerts are less important than customer deliveries: give up after ~2 hours.
            if permanent or attempts>=min(RETRY_GIVE_UP_ATTEMPTS,10):
                db.execute('UPDATE notify_queue SET attempts=?,failed=? WHERE id=?',(attempts,now,notification_id))
                return 'failed'
            db.execute('UPDATE notify_queue SET attempts=?,next_try=? WHERE id=?',(attempts,now+retry_delay(attempts),notification_id))
            return 'retry'

    def pending_count(self):
        with self.store.read() as db:
            return db.execute('SELECT count(*) FROM notify_queue WHERE sent=0 AND failed IS NULL').fetchone()[0]

    # ------------------------------------------------------------------ daily summary

    def summary(self, now=None, db=None):
        """Numbers for yesterday and today (Moscow days) plus the ready-to-send text."""
        if db is None:
            with self.store.read() as db:return self.summary(now,db)
        now = time.time() if now is None else now
        local = datetime.fromtimestamp(now,MSK)
        today = datetime.combine(local.date(),dtime(0),MSK).timestamp()
        yesterday = datetime.combine(local.date()-timedelta(days=1),dtime(0),MSK).timestamp()
        def period(start,end):
            row = db.execute('''SELECT count(*) orders,coalesce(sum(price),0) revenue FROM orders
                WHERE charge IS NOT NULL AND paid_at>=? AND paid_at<?''',(start,end)).fetchone()
            new = db.execute('SELECT count(*) FROM telegram_users WHERE first_seen>=? AND first_seen<?',(start,end)).fetchone()[0]
            return {'revenue_cents':row['revenue'],'orders':row['orders'],'new_customers':new}
        settings = self.settings(db)
        low = []
        for row in db.execute("""SELECT p.id,p.name,(SELECT count(*) FROM stock s WHERE s.product_id=p.id AND s.order_id IS NULL) free
                FROM products p WHERE p.active=1 AND p.deleted=0 AND p.mode='auto' ORDER BY free,p.id LIMIT 1000"""):
            if row['free']<=self.threshold(settings,row['id']):low.append({'id':row['id'],'name':emoji_text.plain(row['name']),'free':row['free']})
        tickets = db.execute("SELECT count(*) FROM support_tickets WHERE status='open'").fetchone()[0] if _has_table(db,'support_tickets') else 0
        result = {'date':local.date().isoformat(),'yesterday':period(yesterday,today),'today':period(today,now),
                  'open_tickets':tickets,'low_stock':low[:20],'low_stock_total':len(low)}
        def line(label,data):
            return (label+': выручка '+_money(db,data['revenue_cents'])+' · заказов '+str(data['orders'])+
                    ' · новых покупателей '+str(data['new_customers']))
        lines = ['📊 Сводка TheMarket · '+local.strftime('%d.%m.%Y'),'',
                 line('Вчера',result['yesterday']),line('Сегодня (на '+local.strftime('%H:%M')+' МСК)',result['today']),
                 '','💬 Открытых обращений: '+str(tickets),'📦 Мало на складе: '+str(len(low))]
        lines += ['  • '+item['name'][:60]+' — '+str(item['free'])+' шт.' for item in low[:10]]
        if len(low)>10:lines.append('  … и ещё '+str(len(low)-10))
        result['text'] = '\n'.join(lines)
        return result

    def tick(self, now=None):
        """Called by the bot's service worker every ~30 s. Sends the daily summary once per
        Moscow day after the configured time, and prunes old queue rows. Returns what it did."""
        now = time.time() if now is None else now
        done = {'summary':False,'pruned':0}
        with self.store.read() as db:
            settings = self.settings(db)
            marker = db.execute('SELECT value FROM meta WHERE key=?',(SUMMARY_KEY,)).fetchone()
            last_prune = db.execute("SELECT value FROM meta WHERE key='notify.pruned'").fetchone()
        local = datetime.fromtimestamp(now,MSK)
        today = local.date().isoformat()
        if settings.get('daily_summary') and local.strftime('%H:%M')>=settings.get('daily_summary_time','09:00') \
                and (not marker or marker[0]!=today):
            with self.store.tx(invalidate=False) as db:
                # Re-checked under the write lock: two processes never both send it.
                marker = db.execute('SELECT value FROM meta WHERE key=?',(SUMMARY_KEY,)).fetchone()
                if not marker or marker[0]!=today:
                    db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(SUMMARY_KEY,today))
                    done['summary'] = bool(self.emit(db,'daily_summary',self.summary(now,db)['text']))
        if not last_prune or now-float(last_prune[0])>=PRUNE_EVERY:
            with self.store.tx(invalidate=False) as db:
                done['pruned'] = db.execute('''DELETE FROM notify_queue WHERE id IN (SELECT id FROM notify_queue
                    WHERE created<? AND (sent=1 OR failed IS NOT NULL) ORDER BY created LIMIT 1000)''',(now-KEEP_SECONDS,)).rowcount
                db.execute('DELETE FROM notify_dedupe WHERE key IN (SELECT key FROM notify_dedupe WHERE last<? LIMIT 1000)',(now-86400,))
                db.execute("INSERT INTO meta(key,value) VALUES('notify.pruned',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(repr(now),))
        return done
