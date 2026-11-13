"""Panel staff accounts, roles and server-side sessions.

Passwords: scrypt (n=2**14, r=8, p=1, 16-byte per-user salt), compared in constant time; an
unknown username still costs one scrypt run. Sessions: 32 random bytes handed to the browser,
only their SHA-256 is stored; 12 h idle and 7 d absolute lifetime.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time

ROLES = ('owner','manager','support')
PERMISSIONS = ('orders.view','orders.manage','customers.view','customers.balance','products.manage',
               'stock.manage','stock.reveal','support','promo.view','promo.manage','analytics','payments',
               'settings','broadcasts','backups','staff','audit')
ROLE_PERMISSIONS = {
    'owner':frozenset(PERMISSIONS),
    'manager':frozenset({'orders.view','orders.manage','customers.view','products.manage','stock.manage',
                         'support','promo.view'}),
    'support':frozenset({'support','orders.view','customers.view'}),
}
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2**14, 8, 1, 32
IDLE_SECONDS = 12*3600
ABSOLUTE_SECONDS = 7*86400
TOUCH_SECONDS = 60
MAX_SESSIONS_PER_USER = 20
USERNAME_RE = re.compile(r'[A-Za-z0-9_.-]{3,32}')
MIN_PASSWORD = 10
MAX_PASSWORD = 256

# Bootstrap owner credentials from the environment, per database file (see ensure_owner).
_ENV_OWNERS = {}
_ENV_LOCK = threading.Lock()


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS staff_users(
        id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('owner','manager','support')), active INTEGER NOT NULL DEFAULT 1,
        env_owner INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, last_login REAL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS staff_sessions(
        token_hash TEXT PRIMARY KEY, staff_id INTEGER NOT NULL REFERENCES staff_users(id),
        created REAL NOT NULL, last_seen REAL NOT NULL, expires REAL NOT NULL,
        ip TEXT NOT NULL DEFAULT '', user_agent TEXT NOT NULL DEFAULT '')''')
    db.execute('CREATE INDEX IF NOT EXISTS staff_sessions_user ON staff_sessions(staff_id,created)')
    db.execute('CREATE INDEX IF NOT EXISTS staff_sessions_expires ON staff_sessions(expires)')
    db.execute('CREATE INDEX IF NOT EXISTS staff_sessions_seen ON staff_sessions(last_seen)')


def permissions(role):
    return set(ROLE_PERMISSIONS.get(role,()))


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(),salt=salt,n=SCRYPT_N,r=SCRYPT_R,p=SCRYPT_P,dklen=SCRYPT_LEN)
    return 'scrypt$%d$%d$%d$%s$%s'%(SCRYPT_N,SCRYPT_R,SCRYPT_P,base64.b64encode(salt).decode(),base64.b64encode(digest).decode())


_DUMMY_HASH = None


def verify_password(password, stored):
    try:
        scheme,n,r,p,salt,digest = stored.split('$')
        if scheme!='scrypt':raise ValueError
        n,r,p = int(n),int(r),int(p)
        salt,digest = base64.b64decode(salt),base64.b64decode(digest)
    except (ValueError,AttributeError):
        return False
    if not isinstance(password,str):password = ''
    candidate = hashlib.scrypt(password.encode(),salt=salt,n=n,r=r,p=p,dklen=len(digest),maxmem=64*1024*1024)
    return hmac.compare_digest(candidate,digest)


def _dummy_hash():
    global _DUMMY_HASH
    if _DUMMY_HASH is None:_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest() if isinstance(token,str) and 20<=len(token)<=200 else None


def _password(password):
    if not isinstance(password,str) or not MIN_PASSWORD<=len(password)<=MAX_PASSWORD:
        raise ValueError('Пароль: от 10 до 256 символов')
    return password


def _public(row):
    item = {key:row[key] for key in ('id','username','role','created','updated','last_login') if key in row.keys()}
    item['active'] = bool(row['active']);item['env_owner'] = bool(row['env_owner'])
    item['permissions'] = sorted(permissions(row['role']))
    return item


class Staff:
    def __init__(self, store):
        self.store = store
        self._key = os.path.abspath(store.path)

    # ------------------------------------------------------------------ accounts

    def ensure_owner(self, username, password):
        """Bootstrap/refresh the owner from ADMIN_USER/ADMIN_PASSWORD. That owner can never be
        disabled or demoted, and the environment password always signs in."""
        username = (username or 'owner').strip()
        if not USERNAME_RE.fullmatch(username):raise ValueError('ADMIN_USER: от 3 до 32 латинских букв, цифр, «_», «.», «-»')
        if not isinstance(password,str) or not password:raise ValueError('ADMIN_PASSWORD не задан')
        with _ENV_LOCK:
            _ENV_OWNERS[self._key] = (username.casefold(),hashlib.sha256(password.encode()).digest())
        with self.store.read() as db:
            row = db.execute('SELECT * FROM staff_users WHERE username=?',(username,)).fetchone()
        fresh = None if row and verify_password(password,row['password_hash']) else hash_password(password)
        now = time.time()
        with self.store.tx(invalidate=False) as db:
            db.execute('UPDATE staff_users SET env_owner=0 WHERE env_owner=1 AND username<>?',(username,))
            row = db.execute('SELECT * FROM staff_users WHERE username=?',(username,)).fetchone()
            if not row:
                staff_id = db.execute('''INSERT INTO staff_users(username,password_hash,role,active,env_owner,created,updated)
                    VALUES(?,?,'owner',1,1,?,?)''',(username,fresh or hash_password(password),now,now)).lastrowid
                self.store.log(db,'system','staff.create:'+username,staff_id,details={'state':'owner'})
            else:
                staff_id = row['id']
                db.execute("UPDATE staff_users SET role='owner',active=1,env_owner=1,password_hash=coalesce(?,password_hash),updated=? WHERE id=?",
                           (fresh,now,staff_id))
            return _public(db.execute('SELECT * FROM staff_users WHERE id=?',(staff_id,)).fetchone())

    def create(self, username, password, role, actor):
        if not isinstance(username,str) or not USERNAME_RE.fullmatch(username.strip()):
            raise ValueError('Логин: от 3 до 32 символов — латинские буквы, цифры, «_», «.», «-»')
        username = username.strip()
        if role not in ROLES:raise ValueError('Неизвестная роль')
        digest = hash_password(_password(password))
        now = time.time()
        with self.store.tx(invalidate=False) as db:
            if db.execute('SELECT 1 FROM staff_users WHERE username=?',(username,)).fetchone():
                raise ValueError('Такой логин уже существует')
            staff_id = db.execute('INSERT INTO staff_users(username,password_hash,role,created,updated) VALUES(?,?,?,?,?)',
                                  (username,digest,role,now,now)).lastrowid
            self.store.log(db,actor,'staff.create:'+username,staff_id,details={'state':role})
            return _public(db.execute('SELECT * FROM staff_users WHERE id=?',(staff_id,)).fetchone())

    def update(self, user_id, role=None, active=None, password=None, actor='system'):
        if role is not None and role not in ROLES:raise ValueError('Неизвестная роль')
        digest = hash_password(_password(password)) if password is not None else None
        with self.store.tx(invalidate=False) as db:
            row = db.execute('SELECT * FROM staff_users WHERE id=?',(user_id,)).fetchone() if type(user_id) is int else None
            if not row:raise ValueError('Сотрудник не найден')
            new_role = role or row['role'];new_active = row['active'] if active is None else int(bool(active))
            demoting = row['role']=='owner' and row['active'] and (new_role!='owner' or not new_active)
            if demoting and row['env_owner']:
                raise ValueError('Владельца из настроек (ADMIN_USER) нельзя отключить или понизить')
            if demoting and db.execute("SELECT count(*) FROM staff_users WHERE role='owner' AND active=1").fetchone()[0]<=1:
                raise ValueError('Нельзя отключить или понизить последнего владельца')
            db.execute('UPDATE staff_users SET role=?,active=?,password_hash=coalesce(?,password_hash),updated=? WHERE id=?',
                       (new_role,new_active,digest,time.time(),user_id))
            if digest or not new_active or new_role!=row['role']:
                db.execute('DELETE FROM staff_sessions WHERE staff_id=?',(user_id,))
            changes = [name for name,changed in (('role',new_role!=row['role']),('active',new_active!=row['active']),('password',bool(digest))) if changed]
            self.store.log(db,actor,'staff.update:'+row['username'],user_id,details={'state':','.join(changes) or 'none'})
            return _public(db.execute('SELECT * FROM staff_users WHERE id=?',(user_id,)).fetchone())

    def list(self):
        """All staff accounts (bounded: a shop has a handful) with their live session counts."""
        now = time.time()
        with self.store.read() as db:
            rows = db.execute('''SELECT u.*,(SELECT count(*) FROM staff_sessions s WHERE s.staff_id=u.id
                AND s.expires>? AND s.last_seen>?) sessions FROM staff_users u ORDER BY u.role='owner' DESC,u.username LIMIT 500''',
                (now,now-IDLE_SECONDS)).fetchall()
        result = []
        for row in rows:
            item = _public(row);item['sessions'] = row['sessions'];result.append(item)
        return result

    def get(self, user_id):
        with self.store.read() as db:
            row = db.execute('SELECT * FROM staff_users WHERE id=?',(user_id,)).fetchone()
        if not row:raise ValueError('Сотрудник не найден')
        return _public(row)

    def authenticate(self, username, password):
        """Staff dict or None. Always one scrypt evaluation, whatever fails."""
        name = username.strip() if isinstance(username,str) else ''
        password = password if isinstance(password,str) and len(password)<=MAX_PASSWORD else ''
        row = None
        if USERNAME_RE.fullmatch(name):
            with self.store.read() as db:
                row = db.execute('SELECT * FROM staff_users WHERE username=?',(name,)).fetchone()
        ok = verify_password(password,row['password_hash'] if row else _dummy_hash())
        if row and not ok and row['env_owner']:
            with _ENV_LOCK:env = _ENV_OWNERS.get(self._key)
            if env and env[0]==name.casefold():
                ok = hmac.compare_digest(hashlib.sha256(password.encode()).digest(),env[1])
        if not row or not ok or not row['active'] or not password:return None
        return _public(row)

    # ------------------------------------------------------------------ sessions

    def create_session(self, staff_id, ip='', user_agent=''):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.store.tx(invalidate=False) as db:
            row = db.execute('SELECT active FROM staff_users WHERE id=?',(staff_id,)).fetchone()
            if not row or not row['active']:raise ValueError('Сотрудник не найден или отключён')
            # Bounded cleanup of dead sessions on every login.
            db.execute('''DELETE FROM staff_sessions WHERE token_hash IN (SELECT token_hash FROM staff_sessions
                WHERE expires<? OR last_seen<? LIMIT 200)''',(now,now-IDLE_SECONDS))
            db.execute('INSERT INTO staff_sessions(token_hash,staff_id,created,last_seen,expires,ip,user_agent) VALUES(?,?,?,?,?,?,?)',
                       (_token_hash(token),staff_id,now,now,now+ABSOLUTE_SECONDS,str(ip or '')[:64],str(user_agent or '')[:300]))
            db.execute('''DELETE FROM staff_sessions WHERE staff_id=? AND token_hash NOT IN (SELECT token_hash FROM staff_sessions
                WHERE staff_id=? ORDER BY created DESC LIMIT ?)''',(staff_id,staff_id,MAX_SESSIONS_PER_USER))
            db.execute('UPDATE staff_users SET last_login=? WHERE id=?',(now,staff_id))
        return token

    def session(self, token):
        digest = _token_hash(token)
        if not digest:return None
        now = time.time()
        with self.store.read() as db:
            row = db.execute('''SELECT s.created session_created,s.last_seen,s.expires,u.* FROM staff_sessions s
                JOIN staff_users u ON u.id=s.staff_id WHERE s.token_hash=?''',(digest,)).fetchone()
        if not row:return None
        if not row['active'] or now>=row['expires'] or now-row['last_seen']>=IDLE_SECONDS:
            self.revoke_session(token)
            return None
        seen = row['last_seen']
        if now-seen>=TOUCH_SECONDS:
            with self.store.tx(invalidate=False) as db:
                db.execute('UPDATE staff_sessions SET last_seen=max(last_seen,?) WHERE token_hash=?',(now,digest))
            seen = now
        item = _public(row)
        item.update(session_created=row['session_created'],session_expires=min(row['expires'],seen+IDLE_SECONDS))
        return item

    def revoke_session(self, token):
        digest = _token_hash(token)
        if not digest:return False
        with self.store.tx(invalidate=False) as db:
            return bool(db.execute('DELETE FROM staff_sessions WHERE token_hash=?',(digest,)).rowcount)

    def revoke_all(self, staff_id):
        with self.store.tx(invalidate=False) as db:
            return db.execute('DELETE FROM staff_sessions WHERE staff_id=?',(staff_id,)).rowcount
