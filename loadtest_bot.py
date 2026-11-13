"""Bot load test: simulated customers through the real poll loop and Dispatcher.

    python loadtest_bot.py [--users 2000] [--steps 5] [--latency 0.05] [--rate 1000] [--profile]

A temporary shop database is filled with ~300 products (stock for the automatic ones),
customers with languages and wallet balances. A fake Telegram answers every API call after
`--latency` seconds. Each simulated customer walks a realistic path (start, menu, catalog
pages, product cards, quantity, checkout, wallet purchases, invoices, purchases, search,
support messages). Updates are fed through bot.poll() in batches of 100, as getUpdates does.

Reported: updates/s, handler time per update with and without Telegram latency
(p50/p95/p99), the longest event-loop stall (a ticker measures loop lag), and the outbound
limiter statistics. `--rate` sets the global limiter (new messages/s); the default 1000
measures the bot's own capacity, `--rate 25` the production setting (interactive replies
then queue behind Telegram's limit). `--profile` prints the top functions by own time.

Targets: >= 200 updates/s, p95 < 300 ms excluding Telegram latency, loop stall < 100 ms.
"""
import argparse
import asyncio
import contextvars
import cProfile
import gc
import io
import os
import pstats
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--users', type=int, default=2000)
    parser.add_argument('--steps', type=int, default=8, help='updates per user')
    parser.add_argument('--products', type=int, default=300)
    parser.add_argument('--stock', type=int, default=60, help='stock items per automatic product')
    parser.add_argument('--latency', type=float, default=0.05)
    parser.add_argument('--rate', type=float, default=1000.0, help='global limiter, new messages per second')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--profile', action='store_true')
    return parser.parse_args()


ARGS = parse_args() if __name__ == '__main__' else None
TEMP = tempfile.TemporaryDirectory(prefix='themarket-load-')
os.environ.setdefault('STOCK_KEY', 'J6x4dPn9w0iT6S3m2g0uAZB0Ztd2gS2b3QyM2lQx0aQ=')
os.environ['DATABASE_PATH'] = str(Path(TEMP.name)/'shop.sqlite3')
os.environ.setdefault('SUPPORT_CONTACT', '@support')
os.environ.setdefault('SHOP_TERMS', 'Terms')
os.environ.pop('ADMIN_CHAT_ID', None)

from aiogram.types import Update  # noqa: E402

import bot as botmodule  # noqa: E402
import ratelimit  # noqa: E402

telegram_time = contextvars.ContextVar('telegram_time', default=None)


class FakeTelegram:
    """Every method sleeps `latency` and returns a plausible object."""
    def __init__(self, latency):
        self.latency = latency
        self.calls = 0
        self.message_id = 1000
        self.id = 1
        self.updates = []
        self.position = 0
        self.session = SimpleNamespace(close=self._noop)

    async def _noop(self, *args, **kwargs):
        return None

    async def _call(self, photo=False):
        started = time.perf_counter()
        await asyncio.sleep(self.latency)
        spent = telegram_time.get()
        if spent is not None:
            spent.append(time.perf_counter()-started)
        self.calls += 1
        self.message_id += 1
        return SimpleNamespace(message_id=self.message_id,
                               photo=[SimpleNamespace(file_id='file-'+str(self.message_id))] if photo else None)

    async def send_message(self, *args, **kwargs):
        return await self._call()

    async def send_photo(self, *args, **kwargs):
        return await self._call(True)

    async def edit_message_text(self, *args, **kwargs):
        return await self._call()

    async def edit_message_media(self, *args, **kwargs):
        return await self._call(True)

    async def delete_message(self, *args, **kwargs):
        return await self._call()

    async def send_invoice(self, *args, **kwargs):
        return await self._call()

    async def answer_callback_query(self, *args, **kwargs):
        return await self._call()

    async def answer_pre_checkout_query(self, *args, **kwargs):
        return await self._call()

    async def get_chat_member(self, *args, **kwargs):
        await self._call()
        return SimpleNamespace(status='member')

    async def get_updates(self, offset=0, **kwargs):
        if self.position >= len(self.updates):
            raise asyncio.CancelledError
        batch = self.updates[self.position:self.position+100]
        self.position += len(batch)
        await asyncio.sleep(0)
        return batch


# ---------------------------------------------------------------- data

CATEGORIES = ['Нейросети', 'Видео', 'Музыка', 'Игры', 'VPN', 'Облако', 'Дизайн', 'Офис', 'Обучение',
              'Соцсети', 'Подарочные карты', 'Сервисы']


def populate(store, products, stock, users):
    random.seed(ARGS.seed if ARGS else 7)
    store.set_currency('100', 'loadtest')
    ids = []
    for index in range(products):
        mode = 'auto' if index % 3 else 'manual'
        pid = store.save_product(dict(category=CATEGORIES[index % len(CATEGORIES)],
                                      name='Товар '+str(index)+' Premium', description='Описание товара '+str(index)*3,
                                      price=random.randint(100, 5000), mode=mode, instruction='Инструкция', active=True))
        ids.append((pid, mode))
    auto = [pid for pid, mode in ids if mode == 'auto']
    for pid in auto:
        store.import_stock(pid, '\n'.join('key-%d-%d-%s' % (pid, n, os.urandom(4).hex()) for n in range(stock)))
    for user in range(1, users+1):
        uid = 100000+user
        store.set_language(uid, 'en' if user % 5 == 0 else 'ru')
        store.remember_customer(uid, 'user'+str(user), 'Buyer', '')
        if user % 2 == 0:
            store.adjust_wallet(uid, 100000, 'loadtest', 'seed-'+str(uid), 'loadtest')
    return ids


# ---------------------------------------------------------------- scenario

class Builder:
    def __init__(self):
        self.update_id = 1

    def _next(self):
        self.update_id += 1
        return self.update_id

    def message(self, uid, text):
        return Update.model_validate({'update_id': self._next(), 'message': {
            'message_id': self.update_id, 'date': 0, 'chat': {'id': uid, 'type': 'private'},
            'from': {'id': uid, 'is_bot': False, 'first_name': 'Buyer', 'username': 'user'+str(uid)}, 'text': text}})

    def callback(self, uid, data, photo=True):
        message = {'message_id': 1, 'date': 0, 'chat': {'id': uid, 'type': 'private'}}
        if photo:
            message.update(caption='TheMarket', photo=[{'file_id': 'banner', 'file_unique_id': 'banner-u', 'width': 1200, 'height': 500}])
        else:
            message['text'] = 'screen'
        return Update.model_validate({'update_id': self._next(), 'callback_query': {
            'id': 'cb'+str(self.update_id), 'from': {'id': uid, 'is_bot': False, 'first_name': 'Buyer', 'username': 'user'+str(uid)},
            'chat_instance': 'load', 'data': data, 'message': message}})


def session(builder, uid, products, steps, categories):
    """One customer's realistic walk; returns `steps` updates."""
    pid, mode = random.choice(products)
    category = random.randrange(categories)
    qty = random.randint(1, 3)
    paths = [
        [('m', '/start'), ('c', 'catalog'), ('c', 'cat:%d' % category), ('c', 'product:%d' % pid),
         ('c', 'product:%d:%d' % (pid, qty)), ('c', 'checkout:%d:%d' % (pid, qty)), ('c', 'wbuy:%d:%d' % (pid, qty)),
         ('c', 'wpay:last')],
        [('c', 'home'), ('c', 'wallet'), ('c', 'orders'), ('c', 'catalog'), ('c', 'cat:%d:1' % category),
         ('c', 'pback:%d' % pid)],
        [('c', 'catalog'), ('c', 'cat:%d' % category), ('c', 'product:%d' % pid), ('c', 'qtyinput:%d:1' % pid),
         ('m', str(qty)), ('c', 'checkout:%d:%d' % (pid, qty)), ('c', 'buy:%d:%d' % (pid, qty))],
        [('c', 'home'), ('c', 'support'), ('c', 'write'), ('m', 'Здравствуйте, не пришёл код'),
         ('c', 'orders'), ('c', 'about'), ('c', 'profile')],
        [('c', 'home'), ('c', 'promo'), ('m', 'WELCOME'), ('c', 'product:%d' % pid), ('c', 'wallet'),
         ('c', 'deposit'), ('c', 'stars')],
    ]
    walk = random.choice(paths)[:steps]
    while len(walk) < steps:
        walk.append(random.choice([('c', 'home'), ('c', 'catalog'), ('c', 'wallet'), ('m', '/start')]))
    updates = []
    for kind, value in walk:
        if kind == 'm':
            updates.append(builder.message(uid, value))
        else:
            updates.append(builder.callback(uid, value, photo=value in ('home', 'catalog', 'wallet', 'orders', 'support', 'about')))
    return updates


def build_updates(users, steps, products, categories):
    builder = Builder()
    walks = [session(builder, 100000+user, products, steps, categories) for user in range(1, users+1)]
    # Telegram delivers updates in arrival order: interleave the customers step by step.
    ordered = []
    for step in range(steps):
        wave = [walk[step] for walk in walks if step < len(walk)]
        random.shuffle(wave)
        ordered += wave
    return ordered


# ---------------------------------------------------------------- measurement

def percentile(values, share):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered)-1, int(len(ordered)*share))]


async def ticker(stalls, stop, interval=0.005):
    loop = asyncio.get_running_loop()
    expected = loop.time()+interval
    while not stop.is_set():
        await asyncio.sleep(interval)
        now = loop.time()
        stalls.append(max(0.0, now-expected))
        expected = now+interval


async def run(args):
    store = botmodule.store
    started = time.perf_counter()
    products = populate(store, args.products, args.stock, args.users)
    categories = len({product['category'] for product in store.products(public=True)})
    print('Prepared %d products, %d customers in %.1f s' % (len(products), args.users, time.perf_counter()-started))
    random.seed(args.seed)
    updates = build_updates(args.users, args.steps, products, categories)
    fake = FakeTelegram(args.latency)
    fake.updates = updates
    # The pre-built updates are test scaffolding (a real bot holds ~100 at a time): keep them
    # out of the cyclic GC so its full collections measure the bot, not the fixture.
    gc.collect()
    gc.freeze()
    ratelimit.limiter.configure(rate=args.rate)
    transport = ratelimit.RateLimitedBot(fake)
    totals, own = [], []
    original = botmodule.handle

    async def timed(bot, u):
        if u.callback_query and u.callback_query.data == 'wpay:last':
            # The order id is only known at run time: pay the customer's newest order.
            pending = [o for o in store.orders(u.callback_query.from_user.id) if o['status'] == 'pending']
            data = 'wpay:'+pending[0]['id'] if pending else 'wallet'
            u = u.model_copy(update={'callback_query': u.callback_query.model_copy(update={'data': data})})
        spent = []
        telegram_time.set(spent)
        begin = time.perf_counter()
        await original(bot, u)
        elapsed = time.perf_counter()-begin
        totals.append(elapsed)
        own.append(max(0.0, elapsed-sum(spent)))
    botmodule.handle = timed
    stalls, stop = [], asyncio.Event()
    tick = asyncio.create_task(ticker(stalls, stop))
    service = asyncio.create_task(botmodule.service_worker(transport))
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    begin = time.perf_counter()
    try:
        await botmodule.poll(transport, SimpleNamespace(username='loadtest'))
    except asyncio.CancelledError:
        pass
    wall = time.perf_counter()-begin
    if profiler:
        profiler.disable()
    stop.set()
    service.cancel()
    await asyncio.gather(tick, service, return_exceptions=True)
    await botmodule.wait_background()
    botmodule.handle = original
    ms = 1000
    print('Updates: %d in %.2f s -> %.0f updates/s (Telegram calls: %d)' % (len(totals), wall, len(totals)/wall, fake.calls))
    print('Handler time incl. Telegram latency: p50 %.0f ms, p95 %.0f ms, p99 %.0f ms' % (
        percentile(totals, .5)*ms, percentile(totals, .95)*ms, percentile(totals, .99)*ms))
    print('Handler time excl. Telegram latency: p50 %.1f ms, p95 %.1f ms, p99 %.1f ms, mean %.1f ms' % (
        percentile(own, .5)*ms, percentile(own, .95)*ms, percentile(own, .99)*ms, statistics.mean(own)*ms))
    print('Event-loop stall: max %.1f ms, p99 %.1f ms' % (max(stalls)*ms, percentile(stalls, .99)*ms))
    limiter = ratelimit.limiter.stats
    print('Limiter: %d sends, mean wait %.1f ms, pauses %d' % (
        limiter['sent'], limiter['waited']/max(1, limiter['sent'])*ms, limiter['pauses']))
    orders = store.orders()
    print('Orders created: %d (paid %d)' % (len(orders), sum(o['status'] != 'pending' and o['status'] != 'expired' for o in orders)))
    if profiler:
        output = io.StringIO()
        pstats.Stats(profiler, stream=output).sort_stats('tottime').print_stats(25)
        print(output.getvalue())
    return {'updates_per_second': len(totals)/wall, 'p95_own': percentile(own, .95), 'stall': max(stalls)}


def main():
    import logging
    logging.basicConfig(level=logging.ERROR)
    sys.stdout.reconfigure(line_buffering=True)
    try:
        asyncio.run(run(ARGS))
    finally:
        TEMP.cleanup()


if __name__ == '__main__':
    main()
