"""One-time channel onboarding. All Telegram calls happen outside DB transactions."""
from contextlib import closing
import logging
import re
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramAPIError,TelegramRetryAfter

log=logging.getLogger(__name__)
ADMIN_TTL=60
FAIL_OPEN_AFTER=3
FAIL_WINDOW=600
FAIL_OPEN_SECONDS=600
MEMBER_STATUSES=('creator','administrator','member')

KNOWN_USERS='''SELECT user_id FROM telegram_users UNION SELECT user_id FROM orders
    UNION SELECT user_id FROM wallet_ledger UNION SELECT user_id FROM topups UNION SELECT user_id FROM crypto_deposits'''


def initialize(db):
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_access'").fetchone()
    db.execute('''CREATE TABLE IF NOT EXISTS channel_access(user_id INTEGER PRIMARY KEY,
        completed_at REAL,verified_channel INTEGER,next_check REAL NOT NULL DEFAULT 0)''')
    db.execute('''CREATE TABLE IF NOT EXISTS channel_settings(id INTEGER PRIMARY KEY CHECK(id=1),
        enabled INTEGER NOT NULL,channel_ref TEXT NOT NULL,chat_id INTEGER,title TEXT NOT NULL,
        join_url BLOB NOT NULL,revision TEXT NOT NULL,checked_at REAL)''')
    if not exists:
        # Existing buyers have already completed their first shop visit.
        db.execute('INSERT OR IGNORE INTO channel_access(user_id,completed_at) SELECT user_id,? FROM ('+KNOWN_USERS+')',(time.time(),))


def register(db,user_id):
    row=db.execute('SELECT completed_at FROM channel_access WHERE user_id=?',(user_id,)).fetchone()
    enabled=db.execute('SELECT enabled FROM channel_settings WHERE id=1').fetchone()
    if row:
        if row['completed_at'] is None and not (enabled and enabled[0]):
            db.execute('UPDATE channel_access SET completed_at=? WHERE user_id=?',(time.time(),user_id))
        return
    known=db.execute('SELECT 1 FROM ('+KNOWN_USERS+') WHERE user_id=? LIMIT 1',(user_id,)).fetchone()
    completed=time.time() if known or not (enabled and enabled[0]) else None
    db.execute('INSERT INTO channel_access(user_id,completed_at) VALUES(?,?)',(user_id,completed))


def normalize(channel,join_url=''):
    channel=channel.strip();join_url=join_url.strip()
    if channel.startswith(('https://','http://','t.me/')):
        value=urlsplit(channel if '://' in channel else 'https://'+channel)
        if value.hostname!='t.me' or value.username or value.password or value.port or value.query or value.fragment:
            raise ValueError('Укажите @username канала, ссылку t.me/username или ID -100…')
        channel='@'+value.path.strip('/')
    if not channel:return {'channel_ref':'','join_url':''} if not join_url else _invalid_channel()
    if re.fullmatch(r'-100[0-9]{5,13}',channel):pass
    else:
        channel='@'+channel.removeprefix('@')
        if not re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{0,31}',channel):return _invalid_channel()
    if join_url:
        value=urlsplit(join_url)
        if value.scheme!='https' or value.hostname!='t.me' or value.username or value.password or value.port or value.query or value.fragment:
            raise ValueError('Нужна ссылка https://t.me/username или https://t.me/+…')
        if not re.fullmatch(r'/(?:[A-Za-z][A-Za-z0-9_]{0,31}|\+[A-Za-z0-9_-]{5,128}|joinchat/[A-Za-z0-9_-]{5,128})/?',value.path):
            raise ValueError('Нужна ссылка на канал, а не на отдельный пост')
    if channel.startswith('@'):
        expected='https://t.me/'+channel[1:]
        if join_url and join_url.rstrip('/').lower()!=expected.lower():
            raise ValueError('Ссылка должна вести в указанный публичный канал')
        join_url=expected
    return {'channel_ref':channel,'join_url':join_url}


def _invalid_channel():raise ValueError('Укажите @username канала, ссылку t.me/username или ID -100…')


async def inspect_channel(bot,channel,join_url=''):
    data=normalize(channel,join_url)
    if not data['channel_ref']:raise ValueError('Укажите канал для подписки')
    target=int(data['channel_ref']) if data['channel_ref'].startswith('-') else data['channel_ref']
    try:
        chat=await bot.get_chat(target,request_timeout=10)
        if chat.type!='channel':raise ValueError('Нужен Telegram-канал, а не группа или личный чат')
        member=await bot.get_chat_member(chat.id,bot.id,request_timeout=10)
        if member.status not in ('creator','administrator'):
            raise ValueError('Добавьте бота администратором канала и повторите проверку')
    except TelegramRetryAfter:
        raise ValueError('Telegram ограничил частоту проверок. Повторите чуть позже') from None
    except TelegramAPIError:
        raise ValueError('Канал недоступен боту. Проверьте адрес и права администратора') from None
    except (TimeoutError,OSError):
        raise ValueError('Telegram не ответил. Настройки не изменены; повторите проверку') from None
    username=getattr(chat,'username',None)
    if username:
        data['channel_ref']='@'+username;data['join_url']='https://t.me/'+username
    elif not data['join_url'] or not re.fullmatch(r'https://t.me/(?:\+|joinchat/)[A-Za-z0-9_-]{5,128}/?',data['join_url']):
        raise ValueError('Для закрытого канала укажите действующую пригласительную ссылку https://t.me/+…')
    return {**data,'chat_id':chat.id,'title':chat.title or 'Канал магазина','checked_at':time.time()}


_states={}
_states_lock=threading.Lock()


def _state(store):
    """Per-database process state shared by all ChannelGate instances (bot creates one per update)."""
    key=str(store.path)
    with _states_lock:
        return _states.setdefault(key,dict(urls={},admin={},failures=[],open_until=0.0,lock=threading.Lock()))


class ChannelGate:
    def __init__(self,store):
        self.store=store
        self.state=_state(store)

    def _read(self):
        # Deferred read: no write lock for the hot path that runs on every update.
        db=sqlite3.connect(self.store.path,timeout=15)
        db.row_factory=sqlite3.Row
        return closing(db)

    def _join_url(self,blob):
        blob=bytes(blob)
        with self.state['lock']:
            cached=self.state['urls'].get(blob)
        if cached is not None:return cached
        value=self.store.decrypt(blob)
        with self.state['lock']:
            if len(self.state['urls'])>16:self.state['urls'].clear()
            self.state['urls'][blob]=value
        return value

    def settings(self,db=None):
        if db is None:
            with self._read() as db:return self.settings(db)
        row=db.execute('SELECT * FROM channel_settings WHERE id=1').fetchone()
        if not row:return dict(enabled=False,channel_ref='',join_url='',chat_id=None,title='',revision='',checked_at=None)
        result=dict(row);result.pop('id');result['enabled']=bool(result['enabled']);result['join_url']=self._join_url(result['join_url'])
        return result

    def save(self,enabled,data,actor):
        if enabled and (type(data.get('chat_id')) is not int or not data.get('checked_at')):
            raise ValueError('Сначала проверьте доступ бота к каналу')
        with self.store.tx() as db:
            old=self.settings(db)
            revision=old['revision'] if old['chat_id']==data.get('chat_id') and old['enabled']==bool(enabled) else uuid.uuid4().hex
            db.execute('''INSERT INTO channel_settings VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                enabled=excluded.enabled,channel_ref=excluded.channel_ref,chat_id=excluded.chat_id,title=excluded.title,
                join_url=excluded.join_url,revision=excluded.revision,checked_at=excluded.checked_at''',
                (int(enabled),data['channel_ref'],data.get('chat_id'),data.get('title',''),self.store.encrypt(data['join_url']),revision,data.get('checked_at')))
            self.store.log(db,actor,'channel.settings','channel',details={'state':'enabled' if enabled else 'disabled'})
        with self.state['lock']:
            self.state['admin'].clear();self.state['failures'].clear();self.state['open_until']=0.0
        return {'ok':True}

    def fail_open_active(self):
        with self.state['lock']:return self.state['open_until']>time.time()

    def required(self,user_id):
        with self._read() as db:
            config=self.settings(db)
            if not config['enabled']:return None
            row=db.execute('SELECT completed_at FROM channel_access WHERE user_id=?',(user_id,)).fetchone()
        if row and row['completed_at'] is not None:return None
        # Telegram or the bot's admin rights are broken: do not lock the whole shop.
        if self.fail_open_active():return None
        return config

    def _admin_ok(self,chat_id):
        with self.state['lock']:
            expires=self.state['admin'].get(chat_id,0)
        return expires>time.time()

    def _remember_admin(self,chat_id,ok):
        with self.state['lock']:
            if ok:self.state['admin'][chat_id]=time.time()+ADMIN_TTL
            else:self.state['admin'].pop(chat_id,None)

    def _healthy(self):
        with self.state['lock']:self.state['failures'].clear()

    async def verify(self,bot,user_id,config):
        now=time.time()
        with self.store.tx() as db:
            current=self.settings(db)
            if not current['enabled']:return 'changed'
            if current['revision']!=config['revision']:return 'changed'
            db.execute('INSERT OR IGNORE INTO channel_access(user_id) VALUES(?)',(user_id,))
            row=db.execute('SELECT completed_at,next_check FROM channel_access WHERE user_id=?',(user_id,)).fetchone()
            if row['completed_at'] is not None:return 'passed'
            if row['next_check']>now:return 'wait'
            db.execute('UPDATE channel_access SET next_check=? WHERE user_id=?',(now+3,user_id))
        chat_id=config['chat_id']
        try:
            if not self._admin_ok(chat_id):
                admin=await bot.get_chat_member(chat_id,bot.id,request_timeout=10)
                if admin.status not in ('creator','administrator'):
                    self._remember_admin(chat_id,False)
                    return self._failure()
                self._remember_admin(chat_id,True)
            member=await bot.get_chat_member(chat_id,user_id,request_timeout=10)
        except TelegramRetryAfter as exc:
            with self.store.tx() as db:db.execute('UPDATE channel_access SET next_check=? WHERE user_id=?',(time.time()+max(3,exc.retry_after),user_id))
            return 'wait'
        except (TelegramAPIError,TimeoutError,OSError):
            self._remember_admin(chat_id,False)
            return self._failure()
        self._healthy()
        subscribed=member.status in MEMBER_STATUSES or (member.status=='restricted' and getattr(member,'is_member',False) is True)
        if not subscribed:return 'missing'
        with self.store.tx() as db:
            current=self.settings(db)
            if not current['enabled'] or current['revision']!=config['revision']:return 'changed'
            db.execute('UPDATE channel_access SET completed_at=?,verified_channel=? WHERE user_id=?',(time.time(),config['chat_id'],user_id))
        return 'passed'

    def _failure(self):
        now=time.time()
        with self.state['lock']:
            failures=[at for at in self.state['failures'] if at>now-FAIL_WINDOW]+[now]
            opened=len(failures)>=FAIL_OPEN_AFTER
            self.state['failures']=[] if opened else failures
            if opened:self.state['open_until']=now+FAIL_OPEN_SECONDS
        with self.store.tx() as db:
            if opened:
                log.error('Channel subscription check unavailable %s times; gate opened for %s s',FAIL_OPEN_AFTER,FAIL_OPEN_SECONDS)
                self.store.log(db,'bot','channel.check.failed','channel',level='error',details={'state':'fail_open'})
            elif not db.execute("SELECT 1 FROM audit WHERE action='channel.check.failed' AND at>? LIMIT 1",(now-300,)).fetchone():
                self.store.log(db,'bot','channel.check.failed','channel',level='warning')
        # The visitor who hit the threshold is let in now; nobody is marked as verified.
        return 'passed' if opened else 'unavailable'
