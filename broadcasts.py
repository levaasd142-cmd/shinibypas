"""Durable, rate-limited shop announcements.

Delivery rules:
- a request that certainly never reached Telegram (DNS, refused connection) goes back to pending;
- Telegram 5xx / network trouble pauses the whole queue with exponential backoff instead of
  burning through recipients;
- only a timeout after the request may have been sent is recorded as 'uncertain' (never retried).
"""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import threading
import time
import uuid
from urllib.parse import urlsplit

from aiogram.exceptions import (TelegramBadRequest,TelegramForbiddenError,TelegramNetworkError,TelegramNotFound,
                                TelegramRetryAfter,TelegramServerError)
from aiogram.types import FSInputFile

import emoji_text

try:
    import ratelimit  # global Telegram limiter (bot); broadcasts are bulk traffic
except ImportError:
    ratelimit=None

log=logging.getLogger(__name__)
BANNER_PATH=Path(__file__).parent/'assets'/'welcome.png'
MAX_ATTEMPTS=6
BATCH=10
SWEEP_SECONDS=30
PRUNE_SECONDS=3600
KEEP_DAYS=90
BACKOFF_START=5
BACKOFF_MAX=300
_banner_cache={}
_banner_lock=threading.Lock()

AUDIENCE='''WITH users AS (SELECT user_id FROM telegram_users UNION SELECT user_id FROM orders
    UNION SELECT user_id FROM wallet_ledger UNION SELECT user_id FROM topups UNION SELECT user_id FROM crypto_deposits)
    SELECT u.user_id FROM users u LEFT JOIN news_preferences n ON n.user_id=u.user_id
    WHERE u.user_id>0 AND coalesce(n.enabled,1)=1 AND coalesce(n.blocked,0)=0'''


def initialize(db):
    if 'news_announced' not in {row['name'] for row in db.execute('PRAGMA table_info(products)')}:
        db.execute('ALTER TABLE products ADD COLUMN news_announced INTEGER NOT NULL DEFAULT 1')
    db.execute('''CREATE TABLE IF NOT EXISTS news_preferences(user_id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1,blocked INTEGER NOT NULL DEFAULT 0,next_send REAL NOT NULL DEFAULT 0)''')
    if 'next_send' not in {row['name'] for row in db.execute('PRAGMA table_info(news_preferences)')}:
        db.execute('ALTER TABLE news_preferences ADD COLUMN next_send REAL NOT NULL DEFAULT 0')
    db.execute('''CREATE TABLE IF NOT EXISTS broadcasts(id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,
        content BLOB NOT NULL,product_id INTEGER REFERENCES products(id),status TEXT NOT NULL,created REAL NOT NULL,
        ready_at REAL NOT NULL,started REAL,finished REAL,actor TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS broadcast_deliveries(campaign_id TEXT NOT NULL REFERENCES broadcasts(id),
        user_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        next_try REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,claim TEXT,
        message_id INTEGER,error TEXT NOT NULL DEFAULT '',PRIMARY KEY(campaign_id,user_id))''')
    db.execute('CREATE INDEX IF NOT EXISTS broadcasts_due ON broadcasts(status,ready_at)')
    if 'summary' not in {row['name'] for row in db.execute('PRAGMA table_info(broadcasts)')}:
        # Final counts of campaigns whose per-recipient rows were pruned.
        db.execute('ALTER TABLE broadcasts ADD COLUMN summary TEXT')
    db.execute('CREATE INDEX IF NOT EXISTS broadcast_pending ON broadcast_deliveries(status,next_try,campaign_id)')
    db.execute('CREATE INDEX IF NOT EXISTS broadcast_campaign_status ON broadcast_deliveries(campaign_id,status,user_id)')
    db.execute('CREATE INDEX IF NOT EXISTS broadcast_user_status ON broadcast_deliveries(user_id,status)')
    columns={row['name'] for row in db.execute('PRAGMA table_info(broadcasts)')}
    if 'segment' not in columns:db.execute('ALTER TABLE broadcasts ADD COLUMN segment TEXT')
    if 'scheduled_at' not in columns:db.execute('ALTER TABLE broadcasts ADD COLUMN scheduled_at REAL')


SEGMENT_TYPES=('all','buyers','non_buyers','language','product','inactive')
MAX_SCHEDULE_DAYS=90
PAID_BY_USER='EXISTS(SELECT 1 FROM orders o WHERE o.user_id=u.user_id AND o.charge IS NOT NULL'


def validate_segment(segment):
    """Normalized audience filter: {'type':'all'|'buyers'|'non_buyers'} | {'type':'language','language':'ru'|'en'}
    | {'type':'product','product_id':int} | {'type':'inactive','days':1..3650}."""
    if segment in (None,'',{}):return {'type':'all'}
    if not isinstance(segment,dict) or segment.get('type') not in SEGMENT_TYPES:raise ValueError('Выберите аудиторию рассылки')
    kind=segment['type']
    if kind=='language':
        if segment.get('language') not in ('ru','en'):raise ValueError('Выберите язык аудитории')
        return {'type':kind,'language':segment['language']}
    if kind=='product':
        if type(segment.get('product_id')) is not int or segment['product_id']<=0:raise ValueError('Выберите товар для аудитории')
        return {'type':kind,'product_id':segment['product_id']}
    if kind=='inactive':
        if type(segment.get('days')) is not int or not 1<=segment['days']<=3650:raise ValueError('Неактивность: от 1 до 3650 дней')
        return {'type':kind,'days':segment['days']}
    return {'type':kind}


def audience_sql(segment=None,now=None):
    """SELECT user_id … of everyone a campaign with this segment would be sent to now."""
    segment=validate_segment(segment);kind=segment['type'];params=[]
    sql=AUDIENCE
    if kind=='buyers':sql+=' AND '+PAID_BY_USER+')'
    elif kind=='non_buyers':sql+=' AND NOT '+PAID_BY_USER+')'
    elif kind=='product':
        sql+=' AND '+PAID_BY_USER+' AND o.product_id=?)';params.append(segment['product_id'])
    elif kind=='language':
        sql+=" AND coalesce((SELECT language FROM user_languages l WHERE l.user_id=u.user_id),'ru')=?";params.append(segment['language'])
    elif kind=='inactive':
        cutoff=(time.time() if now is None else now)-segment['days']*86400
        sql+=''' AND NOT EXISTS(SELECT 1 FROM telegram_users t WHERE t.user_id=u.user_id AND t.last_seen>=?)
            AND NOT EXISTS(SELECT 1 FROM orders o WHERE o.user_id=u.user_id AND o.created>=?)'''
        params+=[cutoff,cutoff]
    return sql,params


def audience_count(db,segment=None):
    sql,params=audience_sql(segment)
    return db.execute('SELECT count(*) FROM ('+sql+')',params).fetchone()[0]


def _scheduled(value):
    if value in (None,'',0):return None
    if isinstance(value,bool) or not isinstance(value,(int,float)):raise ValueError('Некорректное время запуска')
    if value>time.time()+MAX_SCHEDULE_DAYS*86400:raise ValueError('Запуск можно запланировать не более чем на 90 дней вперёд')
    return float(value)


def product_emoji(db,product_id):
    row=db.execute('SELECT emoji_id FROM products WHERE id=?',(product_id,)).fetchone()
    emoji_id=row['emoji_id'] if row and re.fullmatch(r'[0-9]{1,20}',row['emoji_id']) else ''
    fallback='📦'
    if emoji_id:
        for pack in db.execute('SELECT items FROM emoji_packs'):
            match=next((item for item in json.loads(pack['items']) if item['id']==emoji_id),None)
            if match:
                fallback=match.get('emoji') or fallback
                break
    return {'emoji_id':emoji_id,'emoji':fallback}


class Broadcasts:
    def __init__(self,store):
        self.store=store
        self._last_sweep=0.0
        self._last_prune=0.0
        self._failures=0

    def settings(self,db=None):
        if db is None:
            with self.store.tx() as db:return self.settings(db)
        row=db.execute("SELECT value FROM meta WHERE key='news.settings'").fetchone()
        return json.loads(row[0]) if row else {'new_product':True,'restock':True}

    def save_settings(self,new_product,restock,actor):
        with self.store.tx() as db:
            value=json.dumps(dict(new_product=bool(new_product),restock=bool(restock)))
            db.execute("INSERT INTO meta VALUES('news.settings',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(value,))
            self.store.log(db,actor,'broadcast.settings','news')

    def preferences(self,user_id,enabled=None):
        with self.store.tx() as db:
            if enabled is not None:
                db.execute('INSERT INTO news_preferences(user_id,enabled) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled',(user_id,int(enabled)))
            row=db.execute('SELECT enabled FROM news_preferences WHERE user_id=?',(user_id,)).fetchone()
            return not row or bool(row[0])

    def _freeze_audience(self,db,campaign_id,segment=None):
        sql,params=audience_sql(segment)
        db.execute('INSERT OR IGNORE INTO broadcast_deliveries(campaign_id,user_id) SELECT ?,a.user_id FROM ('+sql+') a',[campaign_id]+params)
        if not db.execute('SELECT 1 FROM broadcast_deliveries WHERE campaign_id=?',(campaign_id,)).fetchone():
            db.execute("UPDATE broadcasts SET status='completed',finished=? WHERE id=?",(time.time(),campaign_id))

    def auto(self,db,product_id,added=0,actor='system'):
        product=db.execute('SELECT * FROM products WHERE id=? AND active=1 AND deleted=0',(product_id,)).fetchone()
        if not product:return
        available=db.execute('SELECT count(*) FROM stock WHERE product_id=? AND order_id IS NULL',(product_id,)).fetchone()[0]
        if product['mode']=='auto' and not available:return
        kind='new_product' if not product['news_announced'] else 'restock' if added else None
        if not kind:return
        db.execute('UPDATE products SET news_announced=1 WHERE id=?',(product_id,))
        if not self.settings(db)[kind] or not audience_count(db):return
        now=time.time()
        existing=db.execute("""SELECT * FROM broadcasts WHERE product_id=? AND status='queued' AND started IS NULL
            AND kind IN ('new_product','restock') AND ready_at>? ORDER BY created DESC LIMIT 1""",(product_id,now)).fetchone()
        data={'name':emoji_text.plain(product['name']),'price':product['price'],'usd':bool(db.execute("SELECT 1 FROM meta WHERE key='stars_per_usd'").fetchone()),
              'available':available,'manual':product['mode']=='manual','added':added}
        data.update(product_emoji(db,product_id))
        if existing:
            old=json.loads(self.store.decrypt(existing['content']));data['added']+=old.get('added',0)
            db.execute('UPDATE broadcasts SET content=? WHERE id=?',(self.store.encrypt(json.dumps(data,ensure_ascii=False)),existing['id']))
            return
        campaign_id=uuid.uuid4().hex
        title=('Новый товар · ' if kind=='new_product' else 'Пополнение стока · ')+emoji_text.plain(product['name'])
        db.execute('INSERT INTO broadcasts(id,kind,title,content,product_id,status,created,ready_at,actor) VALUES(?,?,?,?,?,\'queued\',?,?,?)',
            (campaign_id,kind,title,self.store.encrypt(json.dumps(data,ensure_ascii=False)),product_id,now,now+30,actor))
        self._freeze_audience(db,campaign_id)
        self.store.log(db,actor,'broadcast.queued',campaign_id)

    @staticmethod
    def validate(data):
        allowed={'text','text_en','button_text','target','url','product_id','with_banner'}
        result={key:data.get(key) for key in allowed}
        for key in ('text','text_en','button_text','url'):result[key]=str(result.get(key) or '').strip()
        if not result['text']:raise ValueError('Введите текст рассылки')
        limit=1024 if result.get('with_banner') else 3500
        for key,label in (('text','Текст'),('text_en','Английский текст')):emoji_text.validate(result[key],label,field=key)
        if any(len(emoji_text.plain(result[key]).encode('utf-16-le'))//2>limit for key in ('text','text_en')):
            raise ValueError('Максимум '+str(limit)+' символов с учётом эмодзи')
        if len(result['button_text'])>40:raise ValueError('Название кнопки: до 40 символов')
        if result['target'] not in ('catalog','product','url','none'):raise ValueError('Выберите действие кнопки')
        if result['target']=='url':
            url=urlsplit(result['url'])
            if url.scheme!='https' or not url.hostname or url.username or url.password or len(result['url'])>1000:
                raise ValueError('Для кнопки нужна HTTPS-ссылка без логина и пароля')
        if result['target']=='product' and (type(result['product_id']) is not int or result['product_id']<=0):
            raise ValueError('Выберите товар для кнопки')
        if result['target']!='product':result['product_id']=None
        if result['target']!='url':result['url']=''
        result['with_banner']=bool(result['with_banner'])
        return result

    def draft(self,reference,title,data,actor,replace=False):
        try:reference=str(uuid.UUID(reference))
        except (ValueError,TypeError):raise ValueError('Неверный идентификатор рассылки') from None
        title=title.strip()
        if not title or len(title)>120:raise ValueError('Название рассылки: от 1 до 120 символов')
        content=self.validate(data)
        segment=validate_segment(data.get('segment'))
        scheduled=_scheduled(data.get('scheduled_at'))
        segment_json=json.dumps(segment,sort_keys=True)
        with self.store.tx() as db:
            if segment['type']=='product' and not db.execute('SELECT 1 FROM products WHERE id=?',(segment['product_id'],)).fetchone():
                raise ValueError('Товар для аудитории не найден')
            old=db.execute('SELECT * FROM broadcasts WHERE id=?',(reference,)).fetchone()
            if old:
                if replace:
                    if old['status']!='draft':raise ValueError('Изменить можно только черновик')
                    if content['target']=='product' and not db.execute('SELECT 1 FROM products WHERE id=? AND active=1 AND deleted=0',(content['product_id'],)).fetchone():raise ValueError('Товар недоступен в каталоге')
                    db.execute('UPDATE broadcasts SET title=?,content=?,segment=?,scheduled_at=? WHERE id=?',
                        (title,self.store.encrypt(json.dumps(content,ensure_ascii=False)),segment_json,scheduled,reference))
                    self.store.log(db,actor,'broadcast.edit',reference)
                    return {'id':reference}
                if (old['title']!=title or json.loads(self.store.decrypt(old['content']))!=content
                        or self._segment(old)!=segment or old['scheduled_at']!=scheduled):
                    raise ValueError('Этот идентификатор уже использован для другой рассылки')
                return {'id':reference}
            if replace:raise ValueError('Рассылка не найдена')
            if content['target']=='product' and not db.execute('SELECT 1 FROM products WHERE id=? AND active=1 AND deleted=0',(content['product_id'],)).fetchone():
                raise ValueError('Товар недоступен в каталоге')
            now=time.time()
            db.execute("INSERT INTO broadcasts(id,kind,title,content,status,created,ready_at,actor,segment,scheduled_at) VALUES(?,'custom',?,?,'draft',?,?,?,?,?)",
                (reference,title,self.store.encrypt(json.dumps(content,ensure_ascii=False)),now,now,actor,segment_json,scheduled))
            self.store.log(db,actor,'broadcast.draft',reference)
        return {'id':reference}

    def action(self,campaign_id,action,actor):
        with self.store.tx() as db:
            row=db.execute('SELECT * FROM broadcasts WHERE id=?',(campaign_id,)).fetchone()
            if not row:raise ValueError('Рассылка не найдена')
            current=row['status'];state=current
            if action=='start' and current=='draft':
                data=json.loads(self.store.decrypt(row['content']))
                if data.get('target')=='product' and not db.execute('SELECT 1 FROM products WHERE id=? AND active=1 AND deleted=0',(data['product_id'],)).fetchone():raise ValueError('Товар недоступен')
                now=time.time()
                if row['scheduled_at'] and row['scheduled_at']>now:
                    # The audience is selected when the scheduled time comes (claim_batch).
                    state='scheduled';db.execute('UPDATE broadcasts SET ready_at=? WHERE id=?',(row['scheduled_at'],campaign_id))
                else:
                    self._freeze_audience(db,campaign_id,self._segment(row))
                    db.execute('UPDATE broadcasts SET ready_at=? WHERE id=?',(now,campaign_id))
                    state='queued' if db.execute('SELECT 1 FROM broadcast_deliveries WHERE campaign_id=?',(campaign_id,)).fetchone() else 'completed'
            elif action=='pause' and current in ('queued','running'):state='paused'
            elif action=='resume' and current=='paused':state='queued'
            elif action=='cancel' and current in ('draft','scheduled','queued','running','paused'):
                state='cancelled';db.execute("UPDATE broadcast_deliveries SET status='skipped',error='cancelled' WHERE campaign_id=? AND status='pending'",(campaign_id,))
            elif action not in ('start','pause','resume','cancel'):raise ValueError('Неизвестное действие')
            if state!=current:
                db.execute('UPDATE broadcasts SET status=?,finished=CASE WHEN ? IN (\'completed\',\'cancelled\') THEN ? ELSE NULL END WHERE id=?',(state,state,time.time(),campaign_id))
                self.store.log(db,actor,'broadcast.'+action,campaign_id)
        return {'ok':True}

    @staticmethod
    def _segment(row):
        try:return json.loads(row['segment']) if row['segment'] else {'type':'all'}
        except ValueError:return {'type':'all'}

    def preview(self,segment=None):
        """Audience size for a segment right now: {'segment','count','total'}."""
        segment=validate_segment(segment)
        with self.store.read() as db:
            return {'segment':segment,'count':audience_count(db,segment),'total':audience_count(db)}

    def catalog(self,page=1):
        with self.store.tx() as db:
            total=db.execute('SELECT count(*) FROM broadcasts').fetchone()[0];pages=max(1,(total+19)//20);page=max(1,min(page,pages))
            items=[]
            for row in db.execute('SELECT id,kind,title,status,created,ready_at,started,finished,summary,segment,scheduled_at FROM broadcasts ORDER BY created DESC LIMIT 20 OFFSET ?',((page-1)*20,)):
                item=dict(row);item['counts']=self._counts(db,row['id'],item.pop('summary'));item['segment']=self._segment(row);items.append(item)
            return dict(items=items,total=total,page=page,pages=pages,settings=self.settings(db),audience=audience_count(db))

    @staticmethod
    def _counts(db,campaign_id,summary=None):
        result={key:0 for key in ('pending','sending','sent','failed','blocked','skipped','uncertain')}
        if summary:result.update(json.loads(summary))
        result.update(dict(db.execute('SELECT status,count(*) FROM broadcast_deliveries WHERE campaign_id=? GROUP BY status',(campaign_id,)).fetchall()))
        result['total']=sum(result.values());return result

    def detail(self,campaign_id):
        with self.store.tx() as db:
            row=db.execute('SELECT * FROM broadcasts WHERE id=?',(campaign_id,)).fetchone()
            if not row:raise ValueError('Рассылка не найдена')
            result=dict(row);result['content']=self._content(db,row);result['counts']=self._counts(db,campaign_id,result.pop('summary',None))
            result['segment']=self._segment(row)
            if row['status'] in ('draft','scheduled'):result['audience']=audience_count(db,result['segment'])
            result['previews']={}
            for language in ('ru','en'):
                item={**result,'language':language}
                text,keyboard,banner=render_delivery(item)
                result['previews'][language]=dict(text=text,keyboard=keyboard,banner=banner,entities=delivery_entities(item,text))
            return result

    def _content(self,db,row):
        content=json.loads(self.store.decrypt(row['content']))
        # Pending campaigns from older versions have no saved emoji metadata.
        if row['kind']!='custom' and 'emoji_id' not in content:
            content.update(product_emoji(db,row['product_id']))
        return content

    def _finish(self,db,campaign_id=None):
        """Complete running campaigns without pending work; indexed per campaign."""
        if campaign_id is None:
            rows=db.execute("SELECT id FROM broadcasts WHERE status IN ('queued','running')").fetchall()
        else:
            rows=db.execute("SELECT id FROM broadcasts WHERE id=? AND status IN ('queued','running')",(campaign_id,)).fetchall()
        for row in rows:
            if db.execute("SELECT 1 FROM broadcast_deliveries WHERE campaign_id=? AND status IN ('pending','sending') LIMIT 1",(row['id'],)).fetchone():
                continue
            db.execute("UPDATE broadcasts SET status='completed',finished=? WHERE id=?",(time.time(),row['id']))
            self.store.log(db,'bot','broadcast.completed',row['id'])

    def _sweep(self,db,now):
        """Bookkeeping that a single claim does not depend on: runs at most every 30 s."""
        if now-self._last_sweep<SWEEP_SECONDS:return
        self._last_sweep=now
        db.execute("""UPDATE broadcast_deliveries SET status='skipped',error='notifications_disabled' WHERE status='pending'
            AND user_id IN (SELECT user_id FROM news_preferences WHERE enabled=0 OR blocked=1)""")
        self._finish(db)
        if now-self._last_prune>=PRUNE_SECONDS:
            self._last_prune=now
            self._prune(db,now-KEEP_DAYS*86400)

    def _prune(self,db,before):
        """Archive final counts of old finished campaigns and drop their per-recipient rows."""
        for row in db.execute("""SELECT id,summary FROM broadcasts WHERE status IN ('completed','cancelled')
                AND finished IS NOT NULL AND finished<? AND EXISTS(SELECT 1 FROM broadcast_deliveries d WHERE d.campaign_id=broadcasts.id)""",(before,)).fetchall():
            counts=self._counts(db,row['id'],row['summary']);counts.pop('total')
            db.execute('UPDATE broadcasts SET summary=? WHERE id=?',(json.dumps({k:v for k,v in counts.items() if v}),row['id']))
            db.execute('DELETE FROM broadcast_deliveries WHERE campaign_id=?',(row['id'],))

    def prune(self,keep_days=KEEP_DAYS):
        with self.store.tx() as db:self._prune(db,time.time()-keep_days*86400)

    def claim(self):
        items=self.claim_batch(1)
        return items[0] if items else None

    def claim_batch(self,limit=BATCH):
        now=time.time()
        with self.store.tx() as db:
            db.execute("UPDATE broadcast_deliveries SET status='uncertain',error='interrupted' WHERE status='sending' AND lease_until<?",(now,))
            self._sweep(db,now)
            self._promote_scheduled(db,now)
            cooldown=db.execute("SELECT value FROM meta WHERE key='news.cooldown'").fetchone()
            if cooldown and float(cooldown[0])>now:return []
            result=[];users=set()
            campaigns=db.execute("""SELECT id,kind,content,product_id FROM broadcasts WHERE status IN ('queued','running')
                AND ready_at<=? ORDER BY created,id""",(now,)).fetchall()
            for campaign in campaigns:
                if len(result)>=limit:break
                if campaign['kind']!='custom':
                    product=db.execute('SELECT active,deleted,mode FROM products WHERE id=?',(campaign['product_id'],)).fetchone()
                    available=db.execute('SELECT count(*) FROM stock WHERE product_id=? AND order_id IS NULL',(campaign['product_id'],)).fetchone()[0]
                    if not product or not product['active'] or product['deleted'] or product['mode']=='auto' and not available:
                        db.execute("UPDATE broadcasts SET status='cancelled',finished=? WHERE id=?",(now,campaign['id']))
                        db.execute("UPDATE broadcast_deliveries SET status='skipped',error='product_unavailable' WHERE campaign_id=? AND status='pending'",(campaign['id'],))
                        self.store.log(db,'bot','broadcast.cancel',campaign['id'],details={'state':'product_unavailable'})
                        continue
                rows=db.execute("""SELECT d.*,coalesce(l.language,'ru') language FROM broadcast_deliveries d
                    LEFT JOIN user_languages l ON l.user_id=d.user_id LEFT JOIN news_preferences n ON n.user_id=d.user_id
                    WHERE d.campaign_id=? AND d.status='pending' AND d.next_try<=? AND coalesce(n.next_send,0)<=?
                    AND coalesce(n.enabled,1)=1 AND coalesce(n.blocked,0)=0
                    AND NOT EXISTS(SELECT 1 FROM broadcast_deliveries busy WHERE busy.user_id=d.user_id AND busy.status='sending')
                    ORDER BY d.user_id LIMIT ?""",(campaign['id'],now,now,limit-len(result))).fetchall()
                rows=[row for row in rows if row['user_id'] not in users]
                if not rows:continue
                content=self._content(db,campaign)
                for row in rows:
                    users.add(row['user_id'])
                    claim=uuid.uuid4().hex
                    db.execute('INSERT INTO news_preferences(user_id,next_send) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET next_send=excluded.next_send',(row['user_id'],now+1.1))
                    db.execute("UPDATE broadcast_deliveries SET status='sending',claim=?,lease_until=?,attempts=attempts+1 WHERE campaign_id=? AND user_id=?",(claim,now+120,row['campaign_id'],row['user_id']))
                    item=dict(row)
                    item.update(status='sending',kind=campaign['kind'],product_id=campaign['product_id'],content=content,claim=claim,attempts=row['attempts']+1)
                    result.append(item)
                db.execute("UPDATE broadcasts SET status='running',started=coalesce(started,?) WHERE id=?",(now,campaign['id']))
            return result

    def _promote_scheduled(self,db,now):
        """Scheduled campaigns whose time has come: select the audience now and queue them."""
        for row in db.execute("SELECT * FROM broadcasts WHERE status='scheduled' AND ready_at<=? ORDER BY ready_at LIMIT 5",(now,)).fetchall():
            self._freeze_audience(db,row['id'],self._segment(row))
            if db.execute('SELECT 1 FROM broadcast_deliveries WHERE campaign_id=?',(row['id'],)).fetchone():
                db.execute("UPDATE broadcasts SET status='queued' WHERE id=?",(row['id'],))
            else:
                db.execute("UPDATE broadcasts SET status='completed',finished=? WHERE id=?",(now,row['id']))
            self.store.log(db,'bot','broadcast.start',row['id'],details={'state':'scheduled'})

    def release(self,items):
        """Return claimed-but-unsent deliveries to the queue (graceful stop or global pause)."""
        if not items:return
        with self.store.tx() as db:
            for item in items:
                if db.execute("""UPDATE broadcast_deliveries SET status='pending',attempts=max(0,attempts-1),lease_until=0
                        WHERE campaign_id=? AND user_id=? AND status='sending' AND claim=?""",(item['campaign_id'],item['user_id'],item['claim'])).rowcount:
                    db.execute('UPDATE news_preferences SET next_send=0 WHERE user_id=?',(item['user_id'],))

    def result(self,item,status,message_id=None,retry_after=0,error=None,not_sent=False):
        """Record a delivery outcome. not_sent=True: Telegram never received the request, so the
        attempt is not counted. A retryable delivery that ran out of attempts becomes 'failed'."""
        if status not in ('sent','blocked','failed','uncertain','pending'):raise ValueError('Неверный результат доставки')
        now=time.time()
        if status=='pending' and not not_sent and item.get('attempts',0)>=MAX_ATTEMPTS:
            status,error='failed','attempts'
        detail='' if status=='sent' else str(error or status)[:200]
        with self.store.tx() as db:
            if not db.execute("""UPDATE broadcast_deliveries SET status=?,message_id=?,error=?,next_try=?,lease_until=0,
                    attempts=CASE WHEN ? THEN max(0,attempts-1) ELSE attempts END
                    WHERE campaign_id=? AND user_id=? AND status='sending' AND claim=?""",
                (status,message_id,detail,now+retry_after,int(bool(not_sent)),item['campaign_id'],item['user_id'],item['claim'])).rowcount:return
            db.execute('UPDATE news_preferences SET next_send=max(next_send,?) WHERE user_id=?',(now+1.1,item['user_id']))
            if status=='blocked':db.execute('INSERT INTO news_preferences(user_id,blocked) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET blocked=1',(item['user_id'],))
            if retry_after:self._cooldown(db,now+retry_after)
            self._finish(db,item['campaign_id'])

    @staticmethod
    def _cooldown(db,until):
        db.execute("""INSERT INTO meta VALUES('news.cooldown',?) ON CONFLICT(key) DO UPDATE SET
            value=CASE WHEN CAST(value AS REAL)>CAST(excluded.value AS REAL) THEN value ELSE excluded.value END""",(repr(float(until)),))

    def pause_queue(self,seconds):
        """Global pause for every campaign (Telegram unreachable or failing)."""
        with self.store.tx() as db:self._cooldown(db,time.time()+seconds)

    def backoff(self):
        """Exponential global backoff: 5 s, 10 s, 20 s … capped at 5 minutes."""
        seconds=min(BACKOFF_MAX,BACKOFF_START*2**min(self._failures,10))
        self._failures+=1
        self.pause_queue(seconds)
        return seconds

    def healthy(self):self._failures=0

    # Banner file_id cache: memory first, then the meta table; re-upload when Telegram rejects it.
    @staticmethod
    def _banner_key():
        try:
            info=BANNER_PATH.stat()
            return 'news.banner:'+hashlib.sha256(f'{info.st_size}:{info.st_mtime_ns}'.encode()).hexdigest()[:16]
        except OSError:return 'news.banner:missing'

    def banner_file_id(self):
        key=self._banner_key()
        with _banner_lock:cached=_banner_cache.get((str(self.store.path),key))
        if cached:return cached
        with self.store.tx() as db:
            row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        if not row:return None
        with _banner_lock:_banner_cache[(str(self.store.path),key)]=row[0]
        return row[0]

    def remember_banner(self,file_id):
        key=self._banner_key()
        with _banner_lock:
            if file_id:_banner_cache[(str(self.store.path),key)]=file_id
            else:_banner_cache.pop((str(self.store.path),key),None)
        with self.store.tx() as db:
            db.execute("DELETE FROM meta WHERE key LIKE 'news.banner:%' AND key<>?",(key,))
            if file_id:db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,file_id))
            else:db.execute('DELETE FROM meta WHERE key=?',(key,))


def render_delivery(item):
    data=item['content'];en=item['language']=='en'
    emoji_id=data.get('emoji_id','') if item['kind']!='custom' else ''
    if not re.fullmatch(r'[0-9]{1,20}',emoji_id):emoji_id=''
    if item['kind']=='custom':
        text=data['text_en'] or data['text'] if en else data['text']
        labels={'catalog':('Открыть каталог','Open catalog'),'product':('Открыть товар','View item'),'url':('Перейти','Open link'),'none':('','')}
        label=data['button_text'] or labels[data['target']][en]
        target=data['target'];url=data['url'];product_id=data['product_id'];banner=data['with_banner']
    else:
        from money import format_usd
        title=('New item in TheMarket' if en else 'Новый товар в TheMarket') if item['kind']=='new_product' else ('Stock replenished' if en else 'Сток пополнен')
        amount=format_usd(data['price']) if data['usd'] else str(data['price'])+' Stars'
        available=('Made to order' if en else 'Под заказ') if data['manual'] else ('In stock: ' if en else 'В наличии: ')+str(data['available'])
        icon=(data.get('emoji') or '📦')+' ' if emoji_id else ''
        text='📦 '+title+'\n\n'+icon+data['name']+'\n'+('Price: ' if en else 'Цена: ')+amount+'\n'+available
        label='View item' if en else 'Открыть товар';target='product';url='';product_id=item['product_id'];banner=False
    rows=[]
    if target!='none':
        button={'text':label,'style':'primary'}
        if emoji_id:button['icon_custom_emoji_id']=emoji_id
        if target=='url':button['url']=url
        else:button['callback_data']='product:'+str(product_id) if target=='product' else 'catalog'
        rows.append([button])
    return text,{'inline_keyboard':rows},banner


def delivery_entities(item,text):
    if item['kind']=='custom':return emoji_text.with_entities(text)[1]
    emoji_id=item['content'].get('emoji_id','')
    if not re.fullmatch(r'[0-9]{1,20}',emoji_id):return []
    fallback=item['content'].get('emoji') or '📦'
    prefix=text[:text.index('\n\n')+2]
    return [dict(type='custom_emoji',offset=len(prefix.encode('utf-16-le'))//2,
                 length=len(fallback.encode('utf-16-le'))//2,custom_emoji_id=emoji_id)]


def _chain(exc):
    seen=[]
    while exc is not None and not any(exc is item for item in seen):
        seen.append(exc);exc=exc.__cause__ or exc.__context__
    return seen


def never_sent(exc):
    """True when the request certainly did not reach Telegram (DNS failure, refused/failed connect)."""
    try:
        from aiohttp import ClientConnectorError,ConnectionTimeoutError,InvalidURL
        connect_errors=(ClientConnectorError,ConnectionTimeoutError,InvalidURL)
    except ImportError:connect_errors=()
    for item in _chain(exc):
        if connect_errors and isinstance(item,connect_errors):return True
        if isinstance(item,(ConnectionRefusedError,socket.gaierror)):return True
    return False


def classify(exc):
    """Map a send exception to (delivery status, kind). Kinds in OUTAGE pause the whole queue."""
    if isinstance(exc,TelegramForbiddenError):return 'blocked','blocked'
    if isinstance(exc,TelegramNotFound):return 'blocked','chat_not_found'
    if isinstance(exc,TelegramBadRequest):
        message=str(getattr(exc,'message',exc)).lower()
        if any(reason in message for reason in ('chat not found','user not found','peer_id_invalid','user is deactivated')):
            return 'blocked','chat_not_found'
        return 'failed','bad_request'
    if isinstance(exc,TelegramServerError):return 'pending','server_error'
    if never_sent(exc):return 'pending','not_sent'
    if isinstance(exc,(TelegramNetworkError,TimeoutError,OSError)):return 'uncertain','network'
    return 'uncertain','unexpected'


OUTAGE=('server_error','not_sent','network','unexpected')


def _bad_file(exc):
    message=str(getattr(exc,'message',exc)).lower()
    return any(reason in message for reason in ('wrong file','file identifier','file_id','file reference','wrong remote file'))


async def _send(bot,service,item,text,markup,banner):
    kwargs={'parse_mode':None,'reply_markup':markup if markup['inline_keyboard'] else None,'allow_paid_broadcast':False,'request_timeout':20}
    entities=delivery_entities(item,text)
    text=emoji_text.plain(text)
    if entities:
        kwargs['caption_entities' if banner else 'entities']=entities
        try:return await _send_kwargs(bot,service,item,text,kwargs,banner)
        except TelegramBadRequest as exc:
            # Bot without custom-emoji rights: resend with the plain fallback emoji.
            if _bad_file(exc) or 'chat not found' in str(getattr(exc,'message',exc)).lower():raise
            kwargs.pop('caption_entities',None);kwargs.pop('entities',None)
    return await _send_kwargs(bot,service,item,text,kwargs,banner)


async def _send_kwargs(bot,service,item,text,kwargs,banner):
    if not banner:return await bot.send_message(item['user_id'],text,**kwargs)
    file_id=await asyncio.to_thread(service.banner_file_id)
    if file_id:
        try:return await bot.send_photo(item['user_id'],file_id,caption=text,**kwargs)
        except TelegramBadRequest as exc:
            if not _bad_file(exc):raise
            await asyncio.to_thread(service.remember_banner,None)
    message=await bot.send_photo(item['user_id'],FSInputFile(BANNER_PATH),caption=text,**kwargs)
    photos=getattr(message,'photo',None)
    if photos:
        try:await asyncio.to_thread(service.remember_banner,photos[-1].file_id)
        except Exception:log.warning('Broadcast banner file_id was not cached')
    return message


async def deliver(bot,service,item):
    """Send one claimed delivery and record it. Returns 'sent', 'retry_after', 'outage' or the failure kind."""
    text,markup,banner=render_delivery(item)
    try:
        message=await _send(bot,service,item,text,markup,banner)
    except TelegramRetryAfter as exc:
        await asyncio.to_thread(service.result,item,'pending',None,max(1,exc.retry_after)+1,'retry_after',True)
        return 'retry_after'
    except Exception as exc:
        status,kind=classify(exc)
        await asyncio.to_thread(service.result,item,status,None,0,kind,kind=='not_sent')
        if kind in OUTAGE:
            seconds=await asyncio.to_thread(service.backoff)
            log.warning('Broadcasts paused for %ss: %s (%s)',seconds,kind,type(exc).__name__)
            return 'outage'
        return kind
    await asyncio.to_thread(service.result,item,'sent',getattr(message,'message_id',None))
    service.healthy()
    return 'sent'


async def send_one(bot,service):
    item=await asyncio.to_thread(service.claim)
    if not item:return False
    await deliver(bot,service,item)
    return True


class TokenBucket:
    """Async token bucket: `rate` messages per second with a burst of at most `rate`."""
    def __init__(self,rate,clock=time.monotonic):
        self.rate=max(0.1,float(rate));self.capacity=max(1.0,self.rate)
        self.tokens=self.capacity;self.clock=clock;self.updated=clock()

    def delay(self):
        now=self.clock()
        self.tokens=min(self.capacity,self.tokens+max(0.0,now-self.updated)*self.rate);self.updated=now
        # Reserve a token; a negative balance is the time the caller must wait.
        self.tokens-=1
        return 0.0 if self.tokens>=0 else -self.tokens/self.rate

    async def wait(self):
        delay=self.delay()
        if delay:await asyncio.sleep(delay)


def configured_rate():
    """BROADCAST_RATE messages/second, default 10, capped at 20 (Telegram's bulk limit is ~30/s)."""
    try:rate=float(os.getenv('BROADCAST_RATE','10'))
    except ValueError:rate=10.0
    return min(20.0,max(0.5,rate))


def _limiter():
    """ratelimit.acquire(chat_id, bulk=True) of the bot's global limiter, when present."""
    acquire=getattr(ratelimit,'acquire',None)
    return acquire if asyncio.iscoroutinefunction(acquire) else None


async def worker(bot,store,rate=None,idle=2.0):
    service=Broadcasts(store)
    bucket=TokenBucket(rate or configured_rate())
    last_error=0
    while True:
        items=[];busy=False
        try:
            items=await asyncio.to_thread(service.claim_batch,BATCH)
            busy=bool(items)
            while items:
                acquire=_limiter()
                if acquire:await acquire(items[0]['user_id'],bulk=True)
                else:await bucket.wait()
                item=items.pop(0)
                if await deliver(bot,service,item) in ('outage','retry_after'):
                    busy=False;break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if time.time()-last_error>60:
                log.error('Broadcast worker: %s',type(exc).__name__)
                last_error=time.time()
        finally:
            if items:
                # Claimed but never sent (stop requested or queue paused): back to pending.
                try:service.release(items)
                except Exception:log.warning('Broadcast deliveries were not released; they expire as uncertain')
        await asyncio.sleep(0.05 if busy else idle)
