"""Promo codes (3.2): a code credits a fixed amount to the customer's wallet balance.

Amounts are integer price units (cents when the shop runs in USD, Stars before that).
Promos.redeem_to_balance() validates the code and credits the wallet in one IMMEDIATE
transaction, so global and per-user limits hold even when several customers race for the
last use. Failed attempts are counted per customer (FAIL_LIMIT per FAIL_WINDOW) so codes
cannot be guessed by brute force.

History: 3.0–3.1 codes were checkout discounts. Their redemptions (promo_redemptions, one row
per discounted order) stay for reports and still count against the limits of a code; 'fixed'
codes keep working as balance codes for the same amount, 'percent' codes are switched off
once by initialize() and kept in the list for history. Columns min_order_cents and
product_ids are no longer used.
"""
import re
import time

KINDS = ('percent','fixed')            # stored kind; new codes are always 'fixed'
CODE_RE = re.compile(r'[A-Z0-9_-]{3,32}')
MAX_AMOUNT_CENTS = 1_000_000          # $10 000 per activation (or 1 000 000 Stars before USD)
MAX_VALUE_CENTS = MAX_AMOUNT_CENTS     # 3.0 name
PAGE_SIZE_MAX = 200
FAIL_LIMIT = 5                         # wrong codes per customer ...
FAIL_WINDOW = 10*60                    # ... within this many seconds
MIGRATION_KEY = 'promo.balance_codes'
LEGACY_NOTE = '[3.2] Процентный код отключён: промокоды теперь пополняют баланс. '

NOT_FOUND = 'Промокод не найден'
NOT_STARTED = 'Промокод ещё не действует'
EXPIRED = 'Срок действия промокода истёк'
EXHAUSTED = 'Промокод больше недоступен: лимит использований исчерпан'
ALREADY_USED = 'Вы уже использовали этот промокод'
TOO_MANY = 'Слишком много попыток, попробуй позже'
CUSTOMER_ERRORS = (NOT_FOUND, NOT_STARTED, EXPIRED, EXHAUSTED, ALREADY_USED, TOO_MANY)


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS promo_codes(
        id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK(kind IN ('percent','fixed')), value INTEGER NOT NULL CHECK(value>0),
        min_order_cents INTEGER NOT NULL DEFAULT 0 CHECK(min_order_cents>=0),
        max_uses INTEGER NOT NULL DEFAULT 0 CHECK(max_uses>=0),
        per_user INTEGER NOT NULL DEFAULT 1 CHECK(per_user>=1),
        starts_at REAL, ends_at REAL, product_ids TEXT NOT NULL DEFAULT '[]',
        active INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, updated REAL NOT NULL, actor TEXT NOT NULL DEFAULT '')''')
    # 3.0–3.1 checkout discounts (history; still counted against the limits of a code).
    db.execute('''CREATE TABLE IF NOT EXISTS promo_redemptions(
        id INTEGER PRIMARY KEY, promo_id INTEGER NOT NULL REFERENCES promo_codes(id),
        user_id INTEGER NOT NULL, order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
        discount_cents INTEGER NOT NULL CHECK(discount_cents>=0), created REAL NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS promo_redemption_user ON promo_redemptions(promo_id,user_id)')
    db.execute('CREATE INDEX IF NOT EXISTS promo_active ON promo_codes(active,id)')
    # 3.2 balance credits: the n-th use of a code by a customer; reference = wallet_ledger.reference.
    db.execute('''CREATE TABLE IF NOT EXISTS promo_credits(
        id INTEGER PRIMARY KEY, promo_id INTEGER NOT NULL REFERENCES promo_codes(id),
        user_id INTEGER NOT NULL, n INTEGER NOT NULL CHECK(n>=1),
        amount_cents INTEGER NOT NULL CHECK(amount_cents>0), reference TEXT NOT NULL UNIQUE,
        attempt TEXT, created REAL NOT NULL, UNIQUE(promo_id,user_id,n))''')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS promo_credit_attempt ON promo_credits(user_id,promo_id,attempt) WHERE attempt IS NOT NULL')
    db.execute('CREATE INDEX IF NOT EXISTS promo_credit_created ON promo_credits(created)')
    db.execute('CREATE TABLE IF NOT EXISTS promo_failures(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, at REAL NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS promo_failure_user ON promo_failures(user_id,at)')
    db.execute('CREATE INDEX IF NOT EXISTS promo_failure_at ON promo_failures(at)')
    _migrate_balance_codes(db)


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None


def _migrate_balance_codes(db):
    """Once: percent discount codes cannot become balance codes, switch them off with a note;
    drop the bot's remembered checkout codes (user_state 'promo')."""
    if not _has_table(db,'meta') or db.execute('SELECT 1 FROM meta WHERE key=?',(MIGRATION_KEY,)).fetchone():return
    now = time.time()
    for row in db.execute("SELECT id,note FROM promo_codes WHERE kind='percent'").fetchall():
        note = row['note'] if row['note'].startswith(LEGACY_NOTE) else (LEGACY_NOTE+row['note'])[:200]
        db.execute('UPDATE promo_codes SET active=0,note=?,updated=? WHERE id=?',(note,now,row['id']))
    if _has_table(db,'user_state'):
        db.execute("DELETE FROM user_state WHERE key='promo'")
    db.execute('INSERT INTO meta(key,value) VALUES(?,?)',(MIGRATION_KEY,str(now)))


def convert_to_usd(db, rate):
    """For Store._convert_to_usd: code amounts and credited amounts are price units too
    (the wallet ledger itself is converted by the store)."""
    from money import stars_to_cents
    for row in db.execute('SELECT id,kind,value,min_order_cents FROM promo_codes').fetchall():
        value = max(1,stars_to_cents(row['value'],rate)) if row['kind']=='fixed' else row['value']
        db.execute('UPDATE promo_codes SET value=?,min_order_cents=? WHERE id=?',
                   (value,stars_to_cents(row['min_order_cents'],rate),row['id']))
    for row in db.execute('SELECT id,discount_cents FROM promo_redemptions').fetchall():
        db.execute('UPDATE promo_redemptions SET discount_cents=? WHERE id=?',(stars_to_cents(row['discount_cents'],rate),row['id']))
    for row in db.execute('SELECT id,amount_cents FROM promo_credits').fetchall():
        db.execute('UPDATE promo_credits SET amount_cents=? WHERE id=?',(max(1,stars_to_cents(row['amount_cents'],rate)),row['id']))


def normalize_code(code):
    return code.strip().upper() if isinstance(code,str) else ''


def _int(value, low, high, message):
    if type(value) is not int or not low<=value<=high:raise ValueError(message)
    return value


def _time(value, message):
    if value is None or value=='':return None
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not 946684800<=value<=4102444800:
        raise ValueError(message)
    return float(value)


def validate(data):
    """Clean admin input. 'value' is accepted as the 3.0 name of amount_cents."""
    if not isinstance(data,dict):raise ValueError('Некорректные данные промокода')
    code = normalize_code(data.get('code'))
    if not CODE_RE.fullmatch(code):
        raise ValueError('Код: от 3 до 32 символов, латинские буквы, цифры, «_» и «-»')
    amount = data.get('amount_cents') if data.get('amount_cents') is not None else data.get('value')
    amount = _int(amount,1,MAX_AMOUNT_CENTS,'Сумма начисления: больше нуля и не больше $10 000')
    max_uses = _int(data.get('max_uses',0),0,10**9,'Лимит использований: целое число от 0 (0 — без лимита)')
    per_user = _int(data.get('per_user',1),1,10**6,'Использований на покупателя: целое число от 1')
    starts = _time(data.get('starts_at'),'Некорректная дата начала')
    ends = _time(data.get('ends_at'),'Некорректная дата окончания')
    if starts is not None and ends is not None and ends<=starts:
        raise ValueError('Дата окончания должна быть позже даты начала')
    note = data.get('note') or ''
    if not isinstance(note,str) or len(note.strip())>200:raise ValueError('Заметка: до 200 символов')
    return dict(code=code,amount_cents=amount,max_uses=max_uses,per_user=per_user,starts_at=starts,ends_at=ends,
                active=bool(data.get('active',True)),note=note.strip())


FIELDS = ('code','amount_cents','max_uses','per_user','starts_at','ends_at','active','note')
STATS = '''(SELECT count(*) FROM promo_credits c WHERE c.promo_id=p.id) credits,
  (SELECT count(*) FROM promo_redemptions r WHERE r.promo_id=p.id) legacy_uses,
  (SELECT coalesce(sum(c.amount_cents),0) FROM promo_credits c WHERE c.promo_id=p.id) credited_cents,
  (SELECT count(DISTINCT c.user_id) FROM promo_credits c WHERE c.promo_id=p.id) users,
  (SELECT coalesce(sum(r.discount_cents),0) FROM promo_redemptions r WHERE r.promo_id=p.id) discount_cents'''


def _row(row):
    item = dict(row)
    legacy_percent = item['value'] if item['kind']=='percent' else None
    item['amount_cents'] = item['value'] if item['kind']=='fixed' else None
    item['legacy_percent'] = legacy_percent
    item['used'] = item.pop('credits')+item['legacy_uses']
    item['active'] = bool(item['active'])
    for key in ('kind','value','min_order_cents','product_ids'):item.pop(key,None)
    return item


class Promos:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------ admin

    def _save(self, db, data, code_id=None, actor=''):
        clash = db.execute('SELECT id FROM promo_codes WHERE code=?',(data['code'],)).fetchone()
        if clash and clash['id']!=code_id:raise ValueError('Такой промокод уже существует')
        values = [data['code'],'fixed',data['amount_cents'],data['max_uses'],data['per_user'],data['starts_at'],
                  data['ends_at'],int(data['active']),data['note']]
        columns = ('code','kind','value','max_uses','per_user','starts_at','ends_at','active','note')
        now = time.time()
        if code_id is None:
            cursor = db.execute('INSERT INTO promo_codes('+','.join(columns)+",min_order_cents,product_ids,created,updated,actor) VALUES("
                                +','.join('?'*len(columns))+",0,'[]',?,?,?)",values+[now,now,str(actor)])
            return cursor.lastrowid
        db.execute('UPDATE promo_codes SET '+','.join(key+'=?' for key in columns)+",min_order_cents=0,product_ids='[]',updated=? WHERE id=?",
                   values+[now,code_id])
        return code_id

    def create(self, data, actor):
        data = validate(data)
        with self.store.tx(invalidate=False) as db:
            code_id = self._save(db,data,actor=actor)
            self.store.log(db,actor,'promo.create:'+data['code'],code_id,details={'title':data['code'],'amount':data['amount_cents']})
            return self._get(db,code_id)

    def update(self, code_id, data, actor):
        with self.store.tx(invalidate=False) as db:
            old = self._get(db,code_id)
            merged = {key:old[key] for key in FIELDS}
            merged.update({key:value for key,value in (data or {}).items() if key in FIELDS})
            if (data or {}).get('amount_cents') is None and 'value' in (data or {}):merged['amount_cents'] = data['value']
            clean = validate(merged)
            self._save(db,clean,code_id)
            self.store.log(db,actor,'promo.update:'+clean['code'],code_id,details={'title':clean['code'],'amount':clean['amount_cents']})
            return self._get(db,code_id)

    def set_active(self, code_id, active, actor):
        with self.store.tx(invalidate=False) as db:
            item = self._get(db,code_id)
            if active and item['amount_cents'] is None:
                raise ValueError('Укажите сумму начисления: процентные коды больше не поддерживаются')
            if item['active']!=bool(active):
                db.execute('UPDATE promo_codes SET active=?,updated=? WHERE id=?',(int(bool(active)),time.time(),code_id))
                self.store.log(db,actor,('promo.enable:' if active else 'promo.disable:')+item['code'],code_id,details={'title':item['code']})
            return self._get(db,code_id)

    @staticmethod
    def _get(db, code_id):
        if type(code_id) is not int:raise ValueError(NOT_FOUND)
        row = db.execute('SELECT p.*,'+STATS+' FROM promo_codes p WHERE p.id=?',(code_id,)).fetchone()
        if not row:raise ValueError(NOT_FOUND)
        return _row(row)

    def get(self, code_id):
        with self.store.read() as db:
            return self._get(db,code_id)

    @staticmethod
    def _page(page, page_size):
        if type(page_size) is not int or not 1<=page_size<=PAGE_SIZE_MAX:raise ValueError('Некорректный размер страницы')
        try:return max(1,int(page))
        except (TypeError,ValueError):raise ValueError('Некорректный номер страницы') from None

    def list(self, include_inactive=True, query='', page=1, page_size=50):
        page = self._page(page,page_size)
        clauses = [];params = []
        if not include_inactive:clauses.append('active=1')
        query = normalize_code(query)[:32]
        if query:
            clauses.append("code LIKE ? ESCAPE '\\'")
            params.append('%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.store.read() as db:
            total = db.execute('SELECT count(*) FROM promo_codes'+where,params).fetchone()[0]
            pages = max(1,(total+page_size-1)//page_size);page = min(page,pages)
            items = [_row(r) for r in db.execute('SELECT p.*,'+STATS+' FROM (SELECT * FROM promo_codes'+where+
                     ' ORDER BY id DESC LIMIT ? OFFSET ?) p ORDER BY p.id DESC',params+[page_size,(page-1)*page_size])]
            totals = db.execute('SELECT count(*),coalesce(sum(amount_cents),0),count(DISTINCT user_id) FROM promo_credits').fetchone()
        return dict(items=items,total=total,page=page,pages=pages,
                    totals=dict(uses=totals[0],credited_cents=totals[1],users=totals[2]))

    def redemptions(self, code_id, page=1, page_size=20):
        """Who used a code and when, newest first: balance credits and 3.0 checkout discounts."""
        page = self._page(page,page_size)
        with self.store.read() as db:
            item = self._get(db,code_id)
            total = item['used']
            pages = max(1,(total+page_size-1)//page_size);page = min(page,pages)
            rows = [dict(row) for row in db.execute('''SELECT * FROM (
                SELECT 'credit' kind,id,user_id,amount_cents,created,NULL order_id FROM promo_credits WHERE promo_id=?
                UNION ALL SELECT 'discount' kind,id,user_id,discount_cents,created,order_id FROM promo_redemptions WHERE promo_id=?)
                ORDER BY created DESC,id DESC LIMIT ? OFFSET ?''',(code_id,code_id,page_size,(page-1)*page_size))]
        names = self.store.customer_names([row['user_id'] for row in rows]) if hasattr(self.store,'customer_names') else {}
        for row in rows:row['name'] = names.get(row['user_id'],'')
        return dict(code=item['code'],items=rows,total=total,page=page,pages=pages)

    # ------------------------------------------------------------------ customer

    @staticmethod
    def _uses(db, promo_id, user_id=None):
        """Balance credits plus 3.0 checkout redemptions of a code (of one customer when given)."""
        user = ' AND user_id=?' if user_id is not None else ''
        params = (promo_id,user_id) if user_id is not None else (promo_id,)
        return (db.execute('SELECT count(*) FROM promo_credits WHERE promo_id=?'+user,params).fetchone()[0]
                +db.execute('SELECT count(*) FROM promo_redemptions WHERE promo_id=?'+user,params).fetchone()[0])

    def failures(self, user_id, now=None):
        """Failed attempts of a customer within the current window."""
        now = time.time() if now is None else now
        with self.store.read() as db:
            return db.execute('SELECT count(*) FROM promo_failures WHERE user_id=? AND at>=?',(user_id,now-FAIL_WINDOW)).fetchone()[0]

    def redeem_to_balance(self, code, user_id, attempt=None):
        """Credit the code's amount to the customer's wallet. Returns {code, amount_cents,
        balance, repeated}. Raises ValueError with a customer message (CUSTOMER_ERRORS).
        attempt: the message that asked for it (e.g. 'telegram:<user>:<update>'); a repeated
        delivery of the same message returns the first result instead of a second credit."""
        if type(user_id) is not int or user_id<=0:raise ValueError(NOT_FOUND)
        attempt = str(attempt)[:100] if attempt else None
        code = normalize_code(code)[:64]
        error = None
        with self.store.tx(invalidate=False) as db:
            now = time.time()
            if attempt:
                done = db.execute('''SELECT p.code,c.amount_cents FROM promo_credits c JOIN promo_codes p ON p.id=c.promo_id
                    WHERE c.user_id=? AND c.attempt=? AND p.code=?''',(user_id,attempt,code)).fetchone()
                if done:
                    return dict(code=done['code'],amount_cents=done['amount_cents'],balance=self.store._balance(db,user_id),repeated=True)
            db.execute('DELETE FROM promo_failures WHERE at<?',(now-FAIL_WINDOW,))
            failed = db.execute('SELECT count(*) FROM promo_failures WHERE user_id=? AND at>=?',(user_id,now-FAIL_WINDOW)).fetchone()[0]
            if failed>=FAIL_LIMIT:
                error = TOO_MANY
            else:
                try:
                    return self._credit(db,code,user_id,attempt,now)
                except ValueError as exc:
                    error = str(exc)
                    db.execute('INSERT INTO promo_failures(user_id,at) VALUES(?,?)',(user_id,now))
        raise ValueError(error)

    def _credit(self, db, code, user_id, attempt, now):
        row = db.execute('SELECT * FROM promo_codes WHERE code=?',(code,)).fetchone() if CODE_RE.fullmatch(code) else None
        if not row or not row['active'] or row['kind']!='fixed':raise ValueError(NOT_FOUND)
        if row['starts_at'] is not None and now<row['starts_at']:raise ValueError(NOT_STARTED)
        if row['ends_at'] is not None and now>=row['ends_at']:raise ValueError(EXPIRED)
        if self._uses(db,row['id'],user_id)>=row['per_user']:raise ValueError(ALREADY_USED)
        if row['max_uses'] and self._uses(db,row['id'])>=row['max_uses']:raise ValueError(EXHAUSTED)
        n = db.execute('SELECT coalesce(max(n),0)+1 FROM promo_credits WHERE promo_id=? AND user_id=?',(row['id'],user_id)).fetchone()[0]
        amount = row['value']
        reference = 'promo:%d:%d:%d' % (row['id'],user_id,n)
        db.execute('INSERT INTO promo_credits(promo_id,user_id,n,amount_cents,reference,attempt,created) VALUES(?,?,?,?,?,?,?)',
                   (row['id'],user_id,n,amount,reference,attempt,now))
        db.execute('INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(?,?,?,?)',(user_id,amount,reference,now))
        self.store.log(db,'telegram','promo.redeemed:'+row['code'],user_id,details={'code':row['code'],'amount':amount})
        return dict(code=row['code'],amount_cents=amount,balance=self.store._balance(db,user_id),repeated=False)

