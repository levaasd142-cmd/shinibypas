import asyncio
import time
import unittest

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

import ratelimit
from ratelimit import BULK, INTERACTIVE, SERVICE, Limiter, RateLimitedBot


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.failures = []
        self.session = 'session'

    async def send_message(self, chat_id, text, **kwargs):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((time.monotonic(), chat_id, text))
        return 'ok'

    async def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.sent.append((time.monotonic(), chat_id, text))

    async def answer_callback_query(self, query_id, **kwargs):
        self.sent.append((time.monotonic(), None, 'ack'))


def retry_after(seconds):
    return TelegramRetryAfter(method=SendMessage(chat_id=1, text='x'), message='Too Many Requests', retry_after=seconds)


class LimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_bucket_caps_new_messages(self):
        limiter = Limiter(rate=50, burst=5)
        started = time.monotonic()
        await asyncio.gather(*(limiter.acquire(chat) for chat in range(30)))
        elapsed = time.monotonic()-started
        # 5 immediately, 25 more at 50/s -> about 0.5 s.
        self.assertGreater(elapsed, 0.4)
        self.assertLess(elapsed, 1.5)

    async def test_per_chat_allows_burst_of_three_then_one_per_second(self):
        limiter = Limiter(rate=1000, chat_rate=20, chat_burst=3)
        started = time.monotonic()
        for _ in range(3):
            await limiter.acquire(7)
        self.assertLess(time.monotonic()-started, 0.03)
        for _ in range(4):
            await limiter.acquire(7)
        # The next four wait one interval (50 ms) each.
        self.assertGreater(time.monotonic()-started, 0.17)
        other = time.monotonic()
        await limiter.acquire(8)
        self.assertLess(time.monotonic()-other, 0.02)

    async def test_edits_skip_the_global_bucket_but_respect_pause(self):
        limiter = Limiter(rate=1, burst=1)
        await limiter.acquire(1)
        started = time.monotonic()
        for chat in range(2, 12):
            await limiter.acquire(chat, new_message=False)
        self.assertLess(time.monotonic()-started, 0.05)
        limiter.pause(0.1)
        await limiter.acquire(20, new_message=False)
        self.assertGreater(time.monotonic()-started, 0.09)

    async def test_interactive_traffic_goes_before_bulk(self):
        limiter = Limiter(rate=40, burst=1)
        await limiter.acquire(0)  # bucket empty now
        order = []

        async def send(name, level, delay=0):
            await asyncio.sleep(delay)
            await limiter.acquire(None, level=level)
            order.append(name)
        tasks = [asyncio.create_task(send('bulk'+str(i), BULK)) for i in range(5)]
        tasks.append(asyncio.create_task(send('user', INTERACTIVE, 0.01)))
        tasks.append(asyncio.create_task(send('service', SERVICE, 0.01)))
        await asyncio.gather(*tasks)
        self.assertLess(order.index('user'), 2)
        self.assertLess(order.index('service'), 3)
        self.assertEqual(len(order), 7)

    async def test_bulk_flag_and_module_acquire_prepay_the_next_send(self):
        limiter = Limiter(rate=1, burst=1)
        transport = FakeTransport()
        old = ratelimit.limiter
        ratelimit.limiter = limiter
        try:
            bot = RateLimitedBot(transport, limiter).bulk()
            started = time.monotonic()
            await ratelimit.acquire(5, bulk=True)
            await bot.send_message(5, 'campaign')
            # One token for acquire()+send, not two.
            self.assertLess(time.monotonic()-started, 0.3)
            self.assertEqual(limiter.stats['sent'], 1)
        finally:
            ratelimit.limiter = old


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_limits_sends_and_passes_other_calls(self):
        limiter = Limiter(rate=1000)
        transport = FakeTransport()
        bot = RateLimitedBot(transport, limiter)
        self.assertEqual(await bot.send_message(1, 'hi'), 'ok')
        await bot.edit_message_text(text='x', chat_id=1, message_id=3)
        await bot.answer_callback_query('q')
        self.assertEqual(bot.session, 'session')
        self.assertEqual(limiter.stats['sent'], 2)
        self.assertIs(bot.wrapped, transport)

    async def test_retry_after_pauses_everyone_and_retries_interactive_once(self):
        limiter = Limiter(rate=1000)
        transport = FakeTransport()
        transport.failures = [retry_after(0)]
        bot = RateLimitedBot(transport, limiter)
        token = ratelimit.priority.set(INTERACTIVE)
        try:
            self.assertEqual(await bot.send_message(1, 'hi'), 'ok')
        finally:
            ratelimit.priority.reset(token)
        self.assertEqual(len(transport.sent), 1)
        transport.failures = [retry_after(1)]
        with self.assertRaises(TelegramRetryAfter):
            await bot.bulk().send_message(2, 'campaign')
        self.assertGreater(limiter.paused_until, time.monotonic()+0.5)
        transport.failures = [retry_after(60)]
        limiter.paused_until = 0
        with self.assertRaises(TelegramRetryAfter):
            await bot.send_message(3, 'long ban is reported, not waited')


if __name__ == '__main__':
    unittest.main()
