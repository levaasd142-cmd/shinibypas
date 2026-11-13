"""Database load test: builds a shop at production scale and times every hot Store call.

    python loadtest_db.py                 # build a fresh database in a temp dir and measure
    python loadtest_db.py --db big.sqlite3 --keep   # reuse/keep a generated database
    python loadtest_db.py --json          # machine-readable results

Data volume (contract §6): 300 products, 50k stock rows, 20k customers, 150k orders,
300k wallet ledger rows, 500k audit rows, plus top-ups, deposits and outbox rows.
Targets: bot-path calls < 5 ms, panel calls < 150 ms (median of repeated calls).
This is a benchmark script, not a unit test.
"""
import argparse
import hashlib
import json
import os
import random
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

PRODUCTS = 300
STOCK = 50_000
CUSTOMERS = 20_000
ORDERS = 150_000
LEDGER = 300_000
AUDIT = 500_000
DAY = 86400
BOT_TARGET_MS = 5
PANEL_TARGET_MS = 150
WORKER_TARGET_MS = 50       # background housekeeping: bounded write-lock hold
TARGETS = {'bot': BOT_TARGET_MS, 'worker': WORKER_TARGET_MS, 'panel': PANEL_TARGET_MS}
CATEGORIES = ['Игры', 'Подписки', 'Программы', 'VPN', 'Аккаунты', 'Ключи Steam', 'Музыка',
              'Кино и ТВ', 'Обучение', 'Услуги', 'Дизайн', 'Prompts']
WORDS = ['ключ', 'подписка', 'месяц', 'год', 'премиум', 'аккаунт', 'активация', 'лицензия',
         'Netflix', 'Spotify', 'Office', 'Windows', 'игра', 'пак', 'бонус', 'Pro', 'Plus', 'Ultra']
ACTIONS = [('order.created', 'orders', 'info', 30), ('payment.accepted', 'orders', 'info', 20),
           ('delivery.sent', 'orders', 'info', 20), ('order.expired', 'orders', 'info', 15),
           ('wallet.credit', 'payments', 'info', 6), ('product.save', 'products', 'info', 2),
           ('stock.import', 'products', 'info', 1), ('delivery.retry', 'orders', 'info', 2),
           ('crypto.created', 'payments', 'info', 2), ('crypto.review', 'payments', 'warning', 1),
           ('delivery.failed', 'orders', 'error', 1), ('finance.entry', 'finance', 'info', 1),
           ('bot.offline', 'system', 'warning', 1)]


def skewed(rng, count):
    """Index in [0,count) with a heavy head: a few customers/products get most of the traffic."""
    return min(count-1, int(rng.paretovariate(1.2)-1) % count) if rng.random() < 0.5 else rng.randrange(count)


def generate(store, seed=7, quiet=False):
    rng = random.Random(seed)
    now = time.time()
    start = now-365*DAY
    say = (lambda *a: None) if quiet else (lambda *a: print(*a, file=sys.stderr, flush=True))
    with store.tx() as db:
        say('products…')
        db.execute("INSERT INTO meta(key,value) VALUES('stars_per_usd','50') ON CONFLICT(key) DO NOTHING")
        products = []
        for pid in range(1, PRODUCTS+1):
            mode = 'manual' if pid % 10 in (3, 7, 9) else 'auto'
            name = ' '.join(rng.sample(WORDS, 3)).capitalize()+' #'+str(pid)
            price = rng.choice([99, 149, 299, 499, 999, 1999, 4999])
            active = 0 if pid % 17 == 0 else 1
            deleted = 1 if pid % 97 == 0 else 0
            db.execute('''INSERT INTO products(id,category,name,description,price,mode,instruction,active,deleted,cost_price)
                VALUES(?,?,?,?,?,?,?,?,?,?)''', (pid, CATEGORIES[pid % len(CATEGORIES)], name,
                'Описание товара '+name+'. '+' '.join(rng.choices(WORDS, k=40)), price, mode,
                'Инструкция по активации', active, deleted, price//3))
            products.append((pid, mode, price))
        auto_ids = [p for p in products if p[1] == 'auto']
        say('customers…')
        users = list(range(100_000_001, 100_000_001+CUSTOMERS))
        db.executemany('INSERT INTO telegram_users VALUES(?,?,?,?,?,?)',
            [(uid, 'user'+str(uid % 100000), 'Имя'+str(i % 500), '', start+rng.random()*300*DAY, now-rng.random()*60*DAY)
             for i, uid in enumerate(users)])
        db.executemany('INSERT INTO user_languages VALUES(?,?)', [(uid, rng.choice(['ru', 'ru', 'en'])) for uid in users[::2]])
        db.executemany("INSERT INTO meta(key,value) VALUES(?,?)", [('input_mode:'+str(uid), 'txid') for uid in users[:2000]])
        say('orders…')
        orders = [];outbox = [];wallet_paid = []
        pending_users = set()
        for i in range(ORDERS):
            uid = users[skewed(rng, CUSTOMERS)]
            pid, mode, price = products[skewed(rng, PRODUCTS)]
            quantity = 1 if rng.random() < 0.9 else rng.randint(2, 5)
            created = start+(i/ORDERS)*365*DAY+rng.random()*60
            oid = uuid.UUID(int=rng.getrandbits(128)).hex
            roll = rng.random()
            charge = paid_at = fulfilled_at = manager = None
            if i > ORDERS-400 and uid not in pending_users and roll < 0.5:
                status, expires = 'pending', now+rng.choice([-600, 200, 280])
                created = now-rng.random()*300;pending_users.add(uid)
            elif roll < 0.27:
                status, expires = 'expired', created+300
            else:
                charge = ('wallet:'+oid) if rng.random() < 0.4 else 'stars-'+oid
                paid_at = created+rng.random()*120
                if mode == 'auto' or roll > 0.31:status, fulfilled_at = 'fulfilled', paid_at+(0 if mode == 'auto' else rng.random()*7200)
                else:status = rng.choice(['awaiting_details', 'queued', 'processing'])
                expires = created+300
                manager = 'owner' if mode == 'manual' and status in ('processing', 'fulfilled') else None
                if charge.startswith('wallet:'):wallet_paid.append((uid, price*quantity, oid, paid_at))
                outbox.append((oid, uid, b'x', 1, 1, 0, paid_at, 1))
            orders.append((oid, uid, pid, 'Товар '+str(pid), price*quantity, mode, '', status, charge, created, expires,
                           manager, 0, quantity, price*quantity*50//100, charge and price*quantity//3, 1 if charge else 0,
                           paid_at, fulfilled_at))
        db.executemany('''INSERT INTO orders(id,user_id,product_id,title,price,mode,instruction,status,charge,created,expires,
            manager,checkout,quantity,payment_stars,cost_total,cost_locked,paid_at,fulfilled_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', orders)
        db.executemany('INSERT INTO outbox(order_id,user_id,body,sent,attempts,next_try,sent_at,purged) VALUES(?,?,?,?,?,?,?,?)', outbox)
        say('stock…')
        cipher = store.cipher
        token = cipher.encrypt(b'LICENSE-KEY-XXXX-XXXX-XXXX')
        sold_auto = [o for o in orders if o[5] == 'auto' and o[8]]
        pending_auto = [o for o in orders if o[5] == 'auto' and o[7] == 'pending']
        stock = [];sid = 0
        for o in pending_auto+sold_auto[-(STOCK*7//10):]:
            for _ in range(o[13]):
                sid += 1;stock.append((sid, o[2], 'h1:'+hashlib.sha256(str(sid).encode()).hexdigest(), token, o[0], 30))
                if len(stock) >= STOCK*7//10:break
            if len(stock) >= STOCK*7//10:break
        while len(stock) < STOCK:
            sid += 1;pid = auto_ids[skewed(rng, len(auto_ids))][0]
            stock.append((sid, pid, 'h1:'+hashlib.sha256(str(sid).encode()).hexdigest(), token, None, 30))
        db.executemany('INSERT INTO stock(id,product_id,fingerprint,secret,order_id,unit_cost) VALUES(?,?,?,?,?,?)', stock)
        say('wallet ledger…')
        ledger = [(uid, -amount, 'order:'+oid, at) for uid, amount, oid, at in wallet_paid]
        topups = [];deposits = []
        whale = users[0]
        while len(ledger) < LEDGER:
            uid = whale if len(ledger) % 60 == 0 else users[skewed(rng, CUSTOMERS)]
            at = start+rng.random()*365*DAY
            amount = rng.choice([500, 1000, 2000, 5000, 10000])
            roll = rng.random()
            if roll < 0.75:
                tid = 'topup:'+uuid.UUID(int=rng.getrandbits(128)).hex
                topups.append((tid, uid, amount*50//100, 'tch-'+tid, at, amount, at+3600))
                ledger.append((uid, amount, tid, at))
            elif roll < 0.85:
                did = uuid.UUID(int=rng.getrandbits(128)).hex
                deposits.append((did, uid, '0xwallet', '0xtoken', str(amount*10**16+len(deposits)), amount, int(at), int(at)+900,
                                 'tx'+did, 'bsc', 0))
                ledger.append((uid, amount, 'bsc:tx'+did, at))
            else:
                ledger.append((uid, rng.choice([-1, 1])*rng.randint(1, 500), 'admin:'+uuid.UUID(int=rng.getrandbits(128)).hex, at))
        ledger.sort(key=lambda row: row[3])
        db.executemany('INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(?,?,?,?)', ledger)
        db.executemany('INSERT INTO topups(id,user_id,amount,charge,created,credit_cents,expires) VALUES(?,?,?,?,?,?,?)', topups)
        db.executemany('''INSERT INTO crypto_deposits(id,user_id,wallet,token,amount_raw,credit_cents,created,expires,txid,network,amount_active)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''', deposits)
        say('audit…')
        weights = [a[3] for a in ACTIONS]
        audit = []
        for i in range(AUDIT):
            action, category, level, _ = rng.choices(ACTIONS, weights)[0]
            o = orders[rng.randrange(ORDERS)]
            at = start+(i/AUDIT)*365*DAY
            order_id = o[0] if category == 'orders' else None
            details = json.dumps({'title': o[3], 'price': o[4], 'quantity': o[13]}, ensure_ascii=False) if order_id else '{}'
            audit.append((at, 'telegram', action, order_id or str(o[2]), level, category, action, details,
                          o[1] if order_id else None, order_id))
        db.executemany('''INSERT INTO audit(at,actor,action,target,level,category,summary,details,user_id,order_id)
            VALUES(?,?,?,?,?,?,?,?,?,?)''', audit)
    with store.tx() as db:
        db.execute('ANALYZE')
    # A v3 store backfills its own summaries (registry, balances) on open; reopening does that.
    return dict(users=users, auto=[p[0] for p in auto_ids if p[0] % 17 and p[0] % 97], whale=whale)


def timed(function, repeat):
    samples = []
    for _ in range(repeat):
        began = time.perf_counter()
        function()
        samples.append((time.perf_counter()-began)*1000)
    samples.sort()
    return statistics.median(samples), samples[min(len(samples)-1, int(len(samples)*0.95))]


def measure(store, info, repeat=30):
    from analytics import Analytics
    rng = random.Random(11)
    users = info['users'];whale = info['whale']
    stocked = info['auto']
    results = []

    def run(name, kind, function, count=repeat):
        if function is None:
            results.append(dict(name=name, kind=kind, median=None, p95=None))
            return
        try:
            median, p95 = timed(function, count)
        except Exception as exc:  # noqa: BLE001 - benchmark reports, never stops
            results.append(dict(name=name, kind=kind, median=None, p95=None, error=repr(exc)))
            return
        results.append(dict(name=name, kind=kind, median=round(median, 3), p95=round(p95, 3)))

    has = lambda name: getattr(store, name, None)
    # Fresh stock for the write-path measurements.
    for pid in stocked[:5]:
        store.import_stock(pid, '\n'.join('bench-'+str(pid)+'-'+uuid.uuid4().hex for _ in range(300)), 'bench')
    run('product', 'bot', lambda: store.product(rng.choice(stocked)))
    run('products(public)', 'bot', lambda: store.products(public=True))
    run('categories', 'bot', has('categories') and (lambda: store.categories()))
    run('search_products', 'bot', has('search_products') and (lambda: store.search_products(rng.choice(WORDS))))
    run('wallet', 'bot', lambda: store.wallet(users[skewed(rng, len(users))]))
    run('wallet(whale)', 'bot', lambda: store.wallet(whale))
    run('purchases', 'bot', lambda: store.purchases(users[skewed(rng, len(users))]))
    run('purchases(whale)', 'bot', lambda: store.purchases(whale))
    run('orders(user)', 'bot', lambda: store.orders(users[skewed(rng, len(users))]))
    run('input_mode', 'bot', lambda: store.input_mode(rng.choice(users)))
    run('language', 'bot', lambda: store.language(rng.choice(users)))
    run('user_state', 'bot', has('user_state') and (lambda: store.user_state(rng.choice(users), 'promo')))
    run('remember_customer', 'bot', lambda: store.remember_customer(users[rng.randrange(50)], 'user', 'Имя'))
    fresh = iter(range(900_000_001, 900_100_000))
    created = []

    def buy():
        uid = next(fresh)
        created.append(store.create_order(stocked[len(created) % 5], uid))
    run('create_order', 'bot', buy, 200)
    paying = iter(created)

    def pay():
        order = next(paying)
        store.approve(order['id'], order['user_id'], order['payment_stars'], 'XTR')
        store.paid(order['id'], order['user_id'], order['payment_stars'], 'XTR', 'bench-'+order['id'])
    run('approve+paid', 'bot', pay, 100)

    def pay_wallet():
        order = next(paying)
        with store.tx() as db:
            db.execute('INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(?,?,?,?)',
                       (order['user_id'], order['price'], 'bench:'+order['id'], time.time()))
        began = time.perf_counter()
        store.pay_wallet(order['id'], order['user_id'])
        return time.perf_counter()-began
    run('pay_wallet(+credit)', 'bot', pay_wallet, 100)
    run('deliveries', 'bot', lambda: store.deliveries())
    if hasattr(store, '_prune_audit'):
        # First runs after an upgrade work through the audit retention backlog (bounded batches).
        run('housekeeping(backlog)', 'worker', lambda: store.housekeeping(), 10)
        began = time.time()
        while time.time()-began < 120:
            with store.read() as db:
                cursor = db.execute("SELECT value FROM meta WHERE key='audit.pruned_until'").fetchone()
            if cursor and float(cursor[0]) >= time.time()-180*DAY-60:
                break
            store.housekeeping()
    run('housekeeping', 'worker', lambda: store.housekeeping(), 10)
    # Panel
    run('metrics', 'panel', lambda: store.metrics(), 10)
    run('customers_page', 'panel', lambda: store.customers_page(page=rng.randint(1, 50)), 10)
    run('customers_page(search)', 'panel', lambda: store.customers_page(query='user12'), 10)
    run('orders() last 300', 'panel', lambda: store.orders(), 10)
    run('orders_page', 'panel', has('orders_page') and (lambda: store.orders_page(page=rng.randint(1, 100))), 10)
    run('orders_page(status)', 'panel', has('orders_page') and (lambda: store.orders_page(status='fulfilled', page=rng.randint(1, 100))), 10)
    run('orders_page(search)', 'panel', has('orders_page') and (lambda: store.orders_page(query='Товар 12')), 10)
    run('events', 'panel', lambda: store.events(page=rng.randint(1, 100)), 10)
    run('events(category)', 'panel', lambda: store.events(category='payments', page=2), 10)
    run('events(level=error)', 'panel', lambda: store.events(level='error'), 10)
    run('events(search)', 'panel', lambda: store.events(query='Товар 12'), 10)
    run('events(order id)', 'panel', lambda: store.events(query=created[0]['id']), 10)
    run('analytics month', 'panel', lambda: Analytics(store).report('month'), 5)
    run('stock_items', 'panel', lambda: store.stock_items(stocked[0]), 10)
    run('order_info', 'panel', lambda: store.order_info(created[0]['id'], 'bench'), 10)
    run('payment_list', 'panel', lambda: store.payment_list(), 10)
    run('products(panel)', 'panel', lambda: store.products(), 10)
    return results


def report(results):
    width = max(len(r['name']) for r in results)
    lines = [f"{'method':<{width}}  kind   median ms   p95 ms  target"]
    for r in results:
        target = TARGETS[r['kind']]
        if r['median'] is None:
            lines.append(f"{r['name']:<{width}}  {r['kind']:<6}  {'n/a':>9}  {'':>7}  {r.get('error', 'not available')}")
            continue
        verdict = 'ok' if r['median'] < target else 'SLOW'
        lines.append(f"{r['name']:<{width}}  {r['kind']:<6}  {r['median']:>9.3f}  {r['p95']:>7.2f}  <{target} {verdict}")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--db', help='database file (generated if missing)')
    parser.add_argument('--keep', action='store_true', help='keep the generated database')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--repeat', type=int, default=30)
    args = parser.parse_args(argv)
    from store import Store
    temp = None
    if args.db:
        path = Path(args.db)
    else:
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name)/'load.sqlite3'
    key_file = path.with_name(path.name+'.key')
    exists = path.exists() and key_file.exists()
    key = key_file.read_bytes() if exists else Fernet.generate_key()
    os.environ.setdefault('BSC_WALLET', '')
    store = Store(path, key)
    began = time.time()
    if exists:
        info = json.loads(path.with_name(path.name+'.info').read_text())
    else:
        info = generate(store)
        if args.db and args.keep:
            key_file.write_bytes(key)
            path.with_name(path.name+'.info').write_text(json.dumps(info))
        store = Store(path, key)  # reopen: one-off migrations/backfills run like on an upgrade
    print(f'database ready in {time.time()-began:.1f}s ({path.stat().st_size/1e6:.0f} MB)', file=sys.stderr)
    results = measure(store, info, args.repeat)
    print(json.dumps(results, ensure_ascii=False, indent=1) if args.json else report(results))
    if temp and not args.keep:
        store.close();temp.cleanup()
    slow = [r for r in results if r['median'] is not None and r['median'] >= TARGETS[r['kind']]]
    return 1 if slow else 0


if __name__ == '__main__':
    sys.exit(main())
