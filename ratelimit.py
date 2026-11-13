"""Outbound Telegram rate limiting shared by every bot send.

Telegram allows about 30 new messages per second overall and about one per second
in a single chat (short bursts are tolerated). Exceeding that returns 429
(TelegramRetryAfter) and, if repeated, longer bans. This module keeps the bot under
those limits:

* a global token bucket for new messages (default 25/s, env TELEGRAM_RATE);
* a per-chat bucket (1 message/s, burst 3) for messages and edits;
* three priority classes: INTERACTIVE (replies inside an update handler) is served
  before SERVICE (deliveries, support replies, staff notifications), which is served
  before BULK (broadcasts). Lower classes wait while a higher class is waiting;
* a global pause when Telegram answers RetryAfter.

Usage:
    bot = RateLimitedBot(Bot(token))              # wrap the transport once
    await ratelimit.acquire(chat_id, bulk=True)   # broadcasts.py, before its own send

A call to acquire() pre-pays the next send to that chat made by the same task, so a
broadcast that calls acquire() and then sends through RateLimitedBot is counted once.
"""
import asyncio
import contextvars
import os
import time

from aiogram.exceptions import TelegramRetryAfter

INTERACTIVE, SERVICE, BULK = 0, 1, 2

# Priority of sends made by the current task; the update dispatcher sets INTERACTIVE.
priority = contextvars.ContextVar('ratelimit_priority', default=SERVICE)
# (chat_id, deadline) of a send already paid for by acquire() in this task.
_prepaid = contextvars.ContextVar('ratelimit_prepaid', default=None)

# Methods that create a new message: global bucket + per-chat bucket.
SEND_METHODS = frozenset({
    'send_message', 'send_photo', 'send_document', 'send_video', 'send_animation', 'send_audio',
    'send_voice', 'send_sticker', 'send_invoice', 'send_media_group', 'copy_message', 'forward_message',
    'copy_messages', 'forward_messages', 'send_location', 'send_contact', 'send_poll', 'send_dice',
})
# Methods that change an existing message: per-chat bucket only.
EDIT_METHODS = frozenset({
    'edit_message_text', 'edit_message_media', 'edit_message_caption', 'edit_message_reply_markup',
})
# A RetryAfter shorter than this is waited out and the call retried once (not for bulk).
RETRY_INLINE_SECONDS = 10


def _env_float(name, default):
    try:
        value = float(os.getenv(name, '') or default)
    except ValueError:
        return default
    return value if value > 0 else default


class Limiter:
    def __init__(self, rate=None, burst=None, chat_rate=1.0, chat_burst=3, clock=time.monotonic):
        self.rate = float(rate or _env_float('TELEGRAM_RATE', 25))
        self.capacity = float(burst or max(1.0, self.rate))
        self.chat_interval = 1.0/float(chat_rate)
        self.chat_tolerance = (int(chat_burst)-1)*self.chat_interval
        self.clock = clock
        self.tokens = self.capacity
        self.updated = clock()
        self.paused_until = 0.0
        self.waiting = [0, 0, 0]
        self.chats = {}
        self.stats = {'sent': 0, 'waited': 0.0, 'pauses': 0}

    # ------------------------------------------------------------ configuration

    def configure(self, rate=None, burst=None, chat_rate=None, chat_burst=None):
        if rate:
            self.rate = float(rate)
            self.capacity = float(burst or max(1.0, self.rate))
            self.tokens = min(self.tokens, self.capacity)
        if chat_rate:
            burst_count = round(self.chat_tolerance/self.chat_interval)+1
            self.chat_interval = 1.0/float(chat_rate)
            self.chat_tolerance = (burst_count-1)*self.chat_interval
        if chat_burst:
            self.chat_tolerance = (int(chat_burst)-1)*self.chat_interval

    # ------------------------------------------------------------ buckets

    def pause(self, seconds):
        """Telegram said RetryAfter: nobody sends until it passes."""
        until = self.clock()+max(0.0, float(seconds))
        if until > self.paused_until:
            self.paused_until = until
            self.stats['pauses'] += 1

    def _refill(self, now):
        if now > self.updated:
            self.tokens = min(self.capacity, self.tokens+(now-self.updated)*self.rate)
            self.updated = now

    def _reserve_chat(self, chat_id, now):
        """GCRA reservation: returns how long to wait before this chat may receive."""
        if chat_id is None:
            return 0.0
        tat = max(self.chats.get(chat_id, now), now)
        wait = max(0.0, tat-self.chat_tolerance-now)
        self.chats[chat_id] = tat+self.chat_interval
        if len(self.chats) > 50000:
            self._forget_chats(now)
        return wait

    def _forget_chats(self, now):
        self.chats = {key: value for key, value in self.chats.items() if value > now}

    async def acquire(self, chat_id=None, *, bulk=False, level=None, new_message=True):
        """Wait until a send to chat_id is allowed. Returns the seconds waited."""
        level = BULK if bulk else (priority.get() if level is None else level)
        started = self.clock()
        chat_wait = self._reserve_chat(chat_id, started)
        if chat_wait > 0:
            await asyncio.sleep(chat_wait)
        while True:
            now = self.clock()
            if self.paused_until > now:
                await asyncio.sleep(self.paused_until-now)
                continue
            if not new_message:
                break
            self._refill(now)
            ahead = any(self.waiting[index] for index in range(level))
            if self.tokens >= 1 and not ahead:
                self.tokens -= 1
                break
            delay = max((1-self.tokens)/self.rate, 0.002) if not ahead else 1/self.rate
            self.waiting[level] += 1
            try:
                await asyncio.sleep(delay)
            finally:
                self.waiting[level] -= 1
        waited = self.clock()-started
        self.stats['sent'] += 1
        self.stats['waited'] += waited
        return waited


limiter = Limiter()


async def acquire(chat_id=None, bulk=False):
    """Public entry point for modules that send on their own (broadcasts.py).

    The next RateLimitedBot send to the same chat from this task is not counted again."""
    await limiter.acquire(chat_id, bulk=bulk)
    _prepaid.set((chat_id, time.monotonic()+30))


def pause(seconds):
    limiter.pause(seconds)


def _chat_of(args, kwargs):
    if 'chat_id' in kwargs:
        return kwargs['chat_id']
    return args[0] if args else None


class RateLimitedBot:
    """Transparent proxy: every send/edit waits for the limiter; everything else passes."""
    def __init__(self, bot, limiter_=None, level=None):
        self._bot = bot
        self._limiter = limiter_ or limiter
        self._level = level

    def bulk(self):
        """A view for broadcasts: same transport, BULK priority."""
        return RateLimitedBot(self._bot, self._limiter, BULK)

    @property
    def wrapped(self):
        return self._bot

    def __getattr__(self, name):
        attribute = getattr(self._bot, name)
        if name in SEND_METHODS or name in EDIT_METHODS:
            return self._limited(name, attribute)
        return attribute

    def _limited(self, name, method):
        async def call(*args, **kwargs):
            chat_id = _chat_of(args, kwargs)
            level = self._level if self._level is not None else priority.get()
            prepaid = _prepaid.get()
            if prepaid and prepaid[0] == chat_id and prepaid[1] > time.monotonic():
                _prepaid.set(None)
            else:
                await self._limiter.acquire(chat_id, level=level, new_message=name in SEND_METHODS)
            try:
                return await method(*args, **kwargs)
            except TelegramRetryAfter as exc:
                self._limiter.pause(exc.retry_after)
                if level == BULK or exc.retry_after > RETRY_INLINE_SECONDS:
                    raise
            await self._limiter.acquire(chat_id, level=level, new_message=name in SEND_METHODS)
            return await method(*args, **kwargs)
        call.__name__ = name
        return call
