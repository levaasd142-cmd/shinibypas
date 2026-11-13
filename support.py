"""Customer support tickets: one open ticket per customer, encrypted message bodies and a
durable outbound queue that the bot's service worker sends staff replies from."""
import time

MAX_TEXT = 3500
RATE_LIMIT = 20                 # customer messages per user per RATE_WINDOW
RATE_WINDOW = 3600
PAGE_SIZE_MAX = 100
THREAD_LIMIT_MAX = 200
PREVIEW_CHARS = 120
NOTIFY_PREVIEW = 300

EMPTY_TEXT = 'Напишите текст сообщения'
TOO_LONG = 'Сообщение слишком длинное: максимум 3500 символов'
RATE_LIMITED = 'Слишком много сообщений: не более 20 в час. Попробуйте позже.'
BAD_FILE = 'Не удалось принять вложение'


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS support_tickets(
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('open','closed')),
        created REAL NOT NULL, last_message_at REAL NOT NULL, last_message_id INTEGER,
        unread INTEGER NOT NULL DEFAULT 0, assignee TEXT, closed_at REAL, closed_by TEXT)''')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS support_one_open ON support_tickets(user_id) WHERE status='open'")
    db.execute('CREATE INDEX IF NOT EXISTS support_status ON support_tickets(status,last_message_at)')
    db.execute('CREATE INDEX IF NOT EXISTS support_recent ON support_tickets(last_message_at)')
    db.execute('CREATE INDEX IF NOT EXISTS support_unread ON support_tickets(unread) WHERE unread>0')
    db.execute('CREATE INDEX IF NOT EXISTS support_user ON support_tickets(user_id,id)')
    db.execute('''CREATE TABLE IF NOT EXISTS support_messages(
        id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES support_tickets(id),
        user_id INTEGER NOT NULL, author TEXT NOT NULL CHECK(author IN ('customer','staff')),
        actor TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL CHECK(kind IN ('text','photo')),
        body BLOB NOT NULL, file_id TEXT, created REAL NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS support_thread ON support_messages(ticket_id,id)')
    db.execute('CREATE INDEX IF NOT EXISTS support_rate ON support_messages(user_id,author,created)')
    db.execute('''CREATE TABLE IF NOT EXISTS support_outbox(
        id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL UNIQUE REFERENCES support_messages(id),
        user_id INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        next_try REAL NOT NULL DEFAULT 0, failed REAL, sent_at REAL)''')
    db.execute('CREATE INDEX IF NOT EXISTS support_outbox_due ON support_outbox(next_try) WHERE sent=0 AND failed IS NULL')


def _text(text, allow_empty=False):
    if text is None:text = ''
    if not isinstance(text,str):raise ValueError(EMPTY_TEXT)
    text = text.strip()
    if not text and not allow_empty:raise ValueError(EMPTY_TEXT)
    if len(text)>MAX_TEXT:raise ValueError(TOO_LONG)
    return text


def _page(page, page_size):
    if type(page_size) is not int or not 1<=page_size<=PAGE_SIZE_MAX:raise ValueError('Некорректный размер страницы')
    if isinstance(page,bool):raise ValueError('Некорректный номер страницы')
    try:return max(1,int(page))
    except (TypeError,ValueError):raise ValueError('Некорректный номер страницы') from None


def _escape_like(text):
    return text.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')


class Support:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------ customer side

    def open_or_append(self, user_id, text, *, kind='text', file_id=None):
        """A customer message. Returns {'ticket_id','message_id','new_ticket'}."""
        if type(user_id) is not int or user_id<=0:raise ValueError('Неверный Telegram ID')
        if kind not in ('text','photo'):raise ValueError(BAD_FILE)
        text = _text(text,allow_empty=kind=='photo')
        if kind=='photo':
            if not isinstance(file_id,str) or not 0<len(file_id)<=256:raise ValueError(BAD_FILE)
        else:file_id = None
        now = time.time()
        with self.store.tx(invalidate=False) as db:
            recent = db.execute("SELECT count(*) FROM support_messages WHERE user_id=? AND author='customer' AND created>?",
                                (user_id,now-RATE_WINDOW)).fetchone()[0]
            if recent>=RATE_LIMIT:raise ValueError(RATE_LIMITED)
            row = db.execute("SELECT id FROM support_tickets WHERE user_id=? AND status='open'",(user_id,)).fetchone()
            new = row is None
            if new:
                ticket_id = db.execute("INSERT INTO support_tickets(user_id,status,created,last_message_at) VALUES(?,'open',?,?)",
                                       (user_id,now,now)).lastrowid
            else:ticket_id = row['id']
            message_id = db.execute('''INSERT INTO support_messages(ticket_id,user_id,author,kind,body,file_id,created)
                VALUES(?,?,'customer',?,?,?,?)''',(ticket_id,user_id,kind,self.store.encrypt(text),file_id,now)).lastrowid
            db.execute('UPDATE support_tickets SET last_message_at=?,last_message_id=?,unread=unread+1 WHERE id=?',
                       (now,message_id,ticket_id))
            if new:self.store.log(db,'telegram','support.open',ticket_id,user_id=user_id)
            self._notify(db,ticket_id,user_id,text,kind,new)
        return {'ticket_id':ticket_id,'message_id':message_id,'new_ticket':new}

    def _notify(self, db, ticket_id, user_id, text, kind, new):
        try:
            from notify import Notify
        except ImportError:
            return
        user = db.execute('SELECT username,first_name FROM telegram_users WHERE user_id=?',(user_id,)).fetchone()
        name = ('@'+user['username'] if user and user['username'] else (user['first_name'] if user else '')) or str(user_id)
        preview = text if len(text)<=NOTIFY_PREVIEW else text[:NOTIFY_PREVIEW]+'…'
        head = ('🆘 Новое обращение #' if new else '💬 Сообщение в обращении #')+str(ticket_id)+' · '+name+' ('+str(user_id)+')'
        body = head+('\n📷 Фото' if kind=='photo' else '')+('\n\n'+preview if preview else '')
        # A burst of messages in one ticket produces one alert per cooldown window.
        Notify(self.store).emit(db,'support_message',body,key='ticket:'+str(ticket_id))

    def open_ticket(self, user_id):
        """The customer's open ticket id or None (for the bot's support screen)."""
        with self.store.read() as db:
            row = db.execute("SELECT id FROM support_tickets WHERE user_id=? AND status='open'",(user_id,)).fetchone()
            return row['id'] if row else None

    # ------------------------------------------------------------------ staff side

    @staticmethod
    def _ticket(db, ticket_id):
        row = db.execute('SELECT * FROM support_tickets WHERE id=?',(ticket_id,)).fetchone() if type(ticket_id) is int else None
        if not row:raise ValueError('Обращение не найдено')
        return row

    def _message(self, row):
        item = dict(row)
        item['text'] = self.store.decrypt(item.pop('body'))
        return item

    def reply(self, ticket_id, text, actor):
        text = _text(text)
        # The bot sends a reply as ONE Telegram message (max 4096 UTF-16 units with its
        # heading): a split reply could be sent twice when a later part fails and is retried.
        if len(text.encode('utf-16-le'))//2>MAX_TEXT:raise ValueError(TOO_LONG)
        now = time.time()
        with self.store.tx(invalidate=False) as db:
            ticket = self._ticket(db,ticket_id)
            message_id = db.execute('''INSERT INTO support_messages(ticket_id,user_id,author,actor,kind,body,created)
                VALUES(?,?,'staff',?,'text',?,?)''',(ticket_id,ticket['user_id'],str(actor),self.store.encrypt(text),now)).lastrowid
            db.execute('INSERT INTO support_outbox(message_id,user_id) VALUES(?,?)',(message_id,ticket['user_id']))
            # Replying means the staff member has read the thread.
            db.execute('UPDATE support_tickets SET last_message_at=?,last_message_id=?,unread=0,assignee=coalesce(assignee,?) WHERE id=?',
                       (now,message_id,str(actor),ticket_id))
            self.store.log(db,actor,'support.reply',ticket_id,user_id=ticket['user_id'])
            return self._thread_message(db,message_id)

    def _thread_message(self, db, message_id):
        row = db.execute('''SELECT m.*,CASE WHEN o.id IS NULL THEN NULL WHEN o.sent=1 THEN 'sent'
            WHEN o.failed IS NOT NULL THEN 'failed' ELSE 'pending' END delivery
            FROM support_messages m LEFT JOIN support_outbox o ON o.message_id=m.id WHERE m.id=?''',(message_id,)).fetchone()
        return self._message(row)

    def _set_status(self, ticket_id, actor, status):
        with self.store.tx(invalidate=False) as db:
            ticket = self._ticket(db,ticket_id)
            if ticket['status']==status:
                raise ValueError('Обращение уже закрыто' if status=='closed' else 'Обращение уже открыто')
            if status=='closed':
                db.execute("UPDATE support_tickets SET status='closed',closed_at=?,closed_by=?,unread=0 WHERE id=?",
                           (time.time(),str(actor),ticket_id))
            else:
                other = db.execute("SELECT id FROM support_tickets WHERE user_id=? AND status='open'",(ticket['user_id'],)).fetchone()
                if other:raise ValueError('У покупателя уже есть открытое обращение #'+str(other['id']))
                db.execute("UPDATE support_tickets SET status='open',closed_at=NULL,closed_by=NULL WHERE id=?",(ticket_id,))
            self.store.log(db,actor,'support.'+('close' if status=='closed' else 'reopen'),ticket_id,user_id=ticket['user_id'])
        return self.ticket(ticket_id)

    def close(self, ticket_id, actor):return self._set_status(ticket_id,actor,'closed')

    def reopen(self, ticket_id, actor):return self._set_status(ticket_id,actor,'open')

    def assign(self, ticket_id, actor, assignee=None):
        """Assign to `assignee` (default: the acting staff member); '' unassigns."""
        assignee = str(actor) if assignee is None else str(assignee).strip()
        if len(assignee)>64:raise ValueError('Некорректный сотрудник')
        with self.store.tx(invalidate=False) as db:
            ticket = self._ticket(db,ticket_id)
            db.execute('UPDATE support_tickets SET assignee=? WHERE id=?',(assignee or None,ticket_id))
            self.store.log(db,actor,'support.assign',ticket_id,user_id=ticket['user_id'],details={'state':assignee or 'none'})
        return self.ticket(ticket_id)

    def mark_read(self, ticket_id, actor):
        with self.store.tx(invalidate=False) as db:
            self._ticket(db,ticket_id)
            db.execute('UPDATE support_tickets SET unread=0 WHERE id=?',(ticket_id,))
        return {'ok':True}

    LIST_SQL = '''SELECT t.id,t.user_id,t.status,t.created,t.last_message_at,t.unread unread_for_staff,t.assignee,
        t.closed_at,t.closed_by,coalesce(u.username,'') username,coalesce(u.first_name,'') first_name,
        coalesce(u.last_name,'') last_name,m.body last_body,m.author last_author,m.kind last_kind
        FROM support_tickets t LEFT JOIN telegram_users u ON u.user_id=t.user_id
        LEFT JOIN support_messages m ON m.id=t.last_message_id'''

    def _item(self, row):
        item = dict(row)
        body = item.pop('last_body')
        text = self.store.decrypt(body) if body else ''
        item['last_text'] = text if len(text)<=PREVIEW_CHARS else text[:PREVIEW_CHARS]+'…'
        return item

    def ticket(self, ticket_id):
        with self.store.read() as db:
            self._ticket(db,ticket_id)
            return self._item(db.execute(self.LIST_SQL+' WHERE t.id=?',(ticket_id,)).fetchone())

    def tickets(self, status='open', query='', page=1, page_size=30):
        if status not in ('open','closed','all'):raise ValueError('Некорректный фильтр обращений')
        if not isinstance(query,str):raise ValueError('Некорректный поиск')
        page = _page(page,page_size)
        clauses = [];params = []
        if status!='all':clauses.append('t.status=?');params.append(status)
        query = query.strip()[:100]
        if query:
            number = query.lstrip('#')
            pattern = '%'+_escape_like(query.removeprefix('@'))+'%'
            condition = """(CAST(t.user_id AS TEXT) LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\'
                OR (u.first_name||' '||u.last_name) LIKE ? ESCAPE '\\'"""
            params += [pattern]*3
            if number.isdigit():
                condition += ' OR t.id=?';params.append(int(number))
            clauses.append(condition+')')
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.store.read() as db:
            total = db.execute('SELECT count(*) FROM support_tickets t LEFT JOIN telegram_users u ON u.user_id=t.user_id'+where,params).fetchone()[0]
            pages = max(1,(total+page_size-1)//page_size);page = min(page,pages)
            rows = db.execute(self.LIST_SQL+where+' ORDER BY t.last_message_at DESC,t.id DESC LIMIT ? OFFSET ?',
                              params+[page_size,(page-1)*page_size]).fetchall()
            items = [self._item(row) for row in rows]
        return dict(items=items,total=total,page=page,pages=pages)

    def thread(self, ticket_id, before=None, limit=100):
        """Ticket and its newest `limit` messages (oldest first); pass before=<message id> for older ones."""
        if type(limit) is not int or not 1<=limit<=THREAD_LIMIT_MAX:raise ValueError('Некорректный размер страницы')
        with self.store.read() as db:
            self._ticket(db,ticket_id)
            ticket = self._item(db.execute(self.LIST_SQL+' WHERE t.id=?',(ticket_id,)).fetchone())
            sql = '''SELECT m.*,CASE WHEN o.id IS NULL THEN NULL WHEN o.sent=1 THEN 'sent'
                WHEN o.failed IS NOT NULL THEN 'failed' ELSE 'pending' END delivery
                FROM support_messages m LEFT JOIN support_outbox o ON o.message_id=m.id WHERE m.ticket_id=?'''
            params = [ticket_id]
            if before is not None:
                if type(before) is not int:raise ValueError('Некорректный курсор')
                sql += ' AND m.id<?';params.append(before)
            rows = db.execute(sql+' ORDER BY m.id DESC LIMIT ?',params+[limit+1]).fetchall()
            more = len(rows)>limit
            messages = [self._message(row) for row in reversed(rows[:limit])]
            history = db.execute('SELECT count(*) FROM support_tickets WHERE user_id=?',(ticket['user_id'],)).fetchone()[0]
        ticket['tickets_total'] = history
        return {'ticket':ticket,'messages':messages,'has_more':more}

    def unread_count(self):
        """Tickets with customer messages the staff has not read yet."""
        with self.store.read() as db:
            return db.execute('SELECT count(*) FROM support_tickets WHERE unread>0').fetchone()[0]

    def open_count(self, db=None):
        if db is None:
            with self.store.read() as db:return self.open_count(db)
        return db.execute("SELECT count(*) FROM support_tickets WHERE status='open'").fetchone()[0]

    # ------------------------------------------------------------------ outbound queue (bot)

    def outbox_due(self, limit=20):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('Некорректный размер пакета')
        with self.store.read() as db:
            rows = db.execute('''SELECT o.id,o.user_id,o.attempts,m.ticket_id,m.body,coalesce(l.language,'') language
                FROM support_outbox o JOIN support_messages m ON m.id=o.message_id
                LEFT JOIN user_languages l ON l.user_id=o.user_id
                WHERE o.sent=0 AND o.failed IS NULL AND o.next_try<=? ORDER BY o.next_try,o.id LIMIT ?''',
                (time.time(),limit)).fetchall()
            return [{'id':r['id'],'user_id':r['user_id'],'ticket_id':r['ticket_id'],'attempts':r['attempts'],
                     'language':r['language'],'text':self.store.decrypt(r['body'])} for r in rows]

    def outbox_result(self, outbox_id, ok, permanent=False):
        """Same policy as Store.delivery_result: exponential backoff, give up after about a day
        or at once when permanent (bot blocked by the customer)."""
        from store import RETRY_GIVE_UP_ATTEMPTS, retry_delay
        with self.store.tx(invalidate=False) as db:
            row = db.execute('''SELECT o.*,m.ticket_id FROM support_outbox o JOIN support_messages m ON m.id=o.message_id
                WHERE o.id=?''',(outbox_id,)).fetchone()
            if not row or row['sent'] or row['failed']:return None
            now = time.time();attempts = row['attempts']+1
            if ok:
                db.execute('UPDATE support_outbox SET sent=1,attempts=?,sent_at=? WHERE id=?',(attempts,now,outbox_id))
                return 'sent'
            if permanent or attempts>=RETRY_GIVE_UP_ATTEMPTS:
                db.execute('UPDATE support_outbox SET attempts=?,failed=? WHERE id=?',(attempts,now,outbox_id))
                self.store.log(db,'bot','support.delivery_failed',row['ticket_id'],level='warning',user_id=row['user_id'],
                               details={'attempt':attempts,'state':'blocked' if permanent else 'gave_up'})
                return 'failed'
            db.execute('UPDATE support_outbox SET attempts=?,next_try=? WHERE id=?',(attempts,now+retry_delay(attempts),outbox_id))
            return 'retry'
