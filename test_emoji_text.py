"""Inline custom emoji tokens ⟦🔥|ID⟧: parsing, rendering, validation, bot cards, broadcasts, panel API."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

import emoji_text as et
from store import Store

FIRE = '⟦🔥|5368324170671202286⟧'


class TokenTests(unittest.TestCase):
    def test_plain_html_and_escape(self):
        text = 'A <b>&' + FIRE + 'z'
        self.assertEqual(et.plain(text), 'A <b>&🔥z')
        self.assertEqual(et.html(text), 'A &lt;b&gt;&amp;<tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji>z')
        self.assertEqual(et.html(text, custom=False), 'A &lt;b&gt;&amp;🔥z')
        self.assertEqual(et.html('⟦<i>|1⟧'), '⟦&lt;i&gt;|1⟧')  # not a token: markup in the fallback

    def test_entities_use_utf16_offsets(self):
        text, entities = et.with_entities('😀 x' + FIRE)
        self.assertEqual(text, '😀 x🔥')
        self.assertEqual(entities, [dict(type='custom_emoji', offset=4, length=2, custom_emoji_id='5368324170671202286')])

    def test_validate(self):
        et.validate('ok ' + FIRE, 'Описание', {'5368324170671202286'})
        for bad in ('⟦🔥|abc⟧', '⟦🔥|123456789012345678901⟧', '⟦|1⟧'):
            with self.assertRaises(et.FieldError):
                et.validate(bad, 'Описание', field='description')
        with self.assertRaisesRegex(ValueError, 'нет в загруженных'):
            et.validate(FIRE, 'Описание', set())
        with self.assertRaisesRegex(ValueError, 'не больше 50'):
            et.validate(FIRE * 51, 'Описание')

    def test_normalize_id_accepts_pasted_text(self):
        self.assertEqual(et.normalize_id('ID эмодзи: 5368324170671202286 '), '5368324170671202286')
        self.assertEqual(et.normalize_id(FIRE), '5368324170671202286')
        self.assertEqual(et.normalize_id('  '), '')
        with self.assertRaises(et.FieldError):
            et.normalize_id('🔥')

    def test_split_safe(self):
        text = 'ab' + FIRE + 'cd'
        self.assertEqual(et.split_safe(text, 5), 2)
        self.assertEqual(et.split_safe(text, 2), 2)


class BotTokenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        key = Fernet.generate_key()
        self.store = Store(Path(self.temp.name) / 'shop.db', key)
        with patch.dict(os.environ, {'STOCK_KEY': key.decode(), 'DATABASE_PATH': str(Path(self.temp.name) / 'shop.db')}):
            self.module = importlib.import_module('bot')
        self.old, self.module.store = self.module.store, self.store
        self.module.forget_catalog()
        self.store.save_emoji_pack('Fire', 'Fire', [{'id': '5368324170671202286', 'emoji': '🔥', 'file_id': ''}], 'owner')

    def tearDown(self):
        self.module.store = self.old
        self.module.forget_catalog()
        self.temp.cleanup()

    def product(self, **extra):
        data = dict(category='C', name='Hot ' + FIRE + ' <deal>', description='Line ' + FIRE, price=100, mode='manual',
                    instruction='Use ' + FIRE, active=True)
        data.update(extra)
        return self.store.save_product(data)

    def test_store_rejects_unknown_and_broken_tokens(self):
        with self.assertRaisesRegex(ValueError, 'нет в загруженных'):
            self.product(name='x⟦🔥|999⟧')
        with self.assertRaisesRegex(ValueError, 'неверная вставка'):
            self.product(description='⟦🔥|12a⟧')
        pid = self.product(emoji_id='ID эмодзи: 5368324170671202286')
        self.assertEqual(self.store.product(pid)['emoji_id'], '5368324170671202286')

    async def test_card_renders_tokens_and_falls_back(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendMessage
        pid = self.product()
        bot = AsyncMock()
        product = self.module.get_product(pid)
        self.assertEqual(product['name'], 'Hot 🔥 <deal>')  # buttons, invoices, plain messages
        await self.module.product_card(bot, 42, product)
        text = bot.send_message.call_args.args[1]
        self.assertIn('<b>Hot <tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji> &lt;deal&gt;</b>', text)
        self.assertIn('Line <tg-emoji', text)
        error = TelegramBadRequest(method=SendMessage(chat_id=42, text='x'), message='Bad Request: custom emoji unavailable')
        bot = AsyncMock()
        bot.send_message.side_effect = [error, None]
        await self.module.product_card(bot, 42, product)
        fallback = bot.send_message.call_args.args[1]
        self.assertNotIn('tg-emoji', fallback)
        self.assertIn('<b>Hot 🔥 &lt;deal&gt;</b>', fallback)

    def test_caption_limit_counts_visible_text(self):
        pid = self.product(description=(FIRE + ' ') * 40 + 'x' * 900)
        product = self.module.get_product(pid)
        caption = self.module.fitted_caption(product, 1, '')
        self.assertTrue(self.module.fits_caption(caption, 'HTML'))
        self.assertEqual(caption.count('<tg-emoji'), caption.count('</tg-emoji>'))

    def test_chunks_do_not_split_tokens(self):
        text = 'a' * 9 + FIRE * 3
        chunks = list(self.module.message_chunks(text, 30))
        self.assertEqual(''.join(chunks), text)
        for chunk in chunks:
            self.assertEqual(et.plain(chunk).count('⟦'), 0, chunk)

    async def test_broadcast_entities_and_fallback(self):
        import broadcasts
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendMessage
        item = {'kind': 'custom', 'language': 'ru', 'user_id': 1, 'product_id': None,
                'content': {'text': 'Hi ' + FIRE, 'text_en': '', 'button_text': '', 'target': 'none', 'url': '',
                            'product_id': None, 'with_banner': False}}
        text, markup, banner = broadcasts.render_delivery(item)
        bot = AsyncMock()
        await broadcasts._send(bot, None, item, text, markup, banner)
        self.assertEqual(bot.send_message.call_args.args[1], 'Hi 🔥')
        self.assertEqual(bot.send_message.call_args.kwargs['entities'][0]['custom_emoji_id'], '5368324170671202286')
        bot = AsyncMock()
        bot.send_message.side_effect = [TelegramBadRequest(method=SendMessage(chat_id=1, text='x'), message='Bad Request: x'), None]
        await broadcasts._send(bot, None, item, text, markup, banner)
        self.assertNotIn('entities', bot.send_message.call_args.kwargs)
        with self.assertRaisesRegex(ValueError, 'неверная вставка'):
            broadcasts.Broadcasts.validate({'text': '⟦x|1a⟧', 'target': 'none'})


class PanelApiTests(unittest.TestCase):
    def test_product_with_tokens_and_pasted_emoji_id(self):
        import test_server as ts
        ts.server.store.save_emoji_pack('Fire', 'Fire', [{'id': '5368324170671202286', 'emoji': '🔥', 'file_id': ''}], 'owner')
        http = ts.signed_in()
        body = {'category': 'C', 'name': 'N' * 110 + FIRE, 'price': '10', 'mode': 'auto', 'description': FIRE, 'instruction': '',
                'emoji_id': 'ID эмодзи: 5368324170671202286'}
        r = http.post('/api/products', json=body, headers=ts.ORIGIN)
        self.assertEqual(r.status_code, 200, r.text)  # 111 visible chars fit the 120 limit despite the long token
        r = http.post('/api/products', json=dict(body, description='⟦🔥|77⟧'), headers=ts.ORIGIN)
        self.assertEqual((r.status_code, r.json()['field']), (422, 'description'))
        self.assertIn('нет в загруженных', r.json()['detail'])
        r = http.post('/api/products', json=dict(body, emoji_id='🔥'), headers=ts.ORIGIN)
        self.assertEqual((r.status_code, r.json()['field']), (422, 'emoji_id'))
        r = http.post('/api/products', json=dict(body, name='N' * 121), headers=ts.ORIGIN)
        self.assertEqual((r.status_code, r.json()['field']), (422, 'name'))


if __name__ == '__main__':
    unittest.main()
