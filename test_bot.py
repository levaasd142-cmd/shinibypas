import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiogram.types import Update
from cryptography.fernet import Fernet
from store import Store


class FakeBot:
    def __init__(self):self.calls=[];self.acks=[]
    async def send_message(self,*args,**kwargs):self.calls.append(('message',args,kwargs))
    async def answer_callback_query(self,*args,**kwargs):self.acks.append((args,kwargs))
    async def send_invoice(self,*args,**kwargs):self.calls.append(('invoice',args,kwargs))
    async def edit_message_text(self,*args,**kwargs):self.calls.append(('edit',args,kwargs))
    async def send_photo(self,*args,**kwargs):self.calls.append(('photo',args,kwargs))
    async def edit_message_media(self,*args,**kwargs):self.calls.append(('media',args,kwargs))
    async def delete_message(self,*args,**kwargs):self.calls.append(('delete',args,kwargs))


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.key=Fernet.generate_key()
        self.store=Store(Path(self.temp.name)/'shop.db',self.key)
        with patch.dict(os.environ,{'STOCK_KEY':self.key.decode(),'DATABASE_PATH':str(Path(self.temp.name)/'shop.db')}):
            self.module=importlib.import_module('bot')
        self.old_store=self.module.store;self.module.store=self.store
        self.bot=FakeBot()
        self._bsc_env=patch.dict(os.environ,{'BSC_WALLET':'0x4AB3C61e666BA119deA95e0AD78819B3681DF53b'});self._bsc_env.start();self.addCleanup(self._bsc_env.stop)

    def tearDown(self):self.module.store=self.old_store;self.temp.cleanup()

    def callback(self,data,user_id=42,photo=False):
        message={'message_id':1,'date':1 if photo else 0,'chat':{'id':user_id,'type':'private'}}
        if photo:message.update(caption='TheMarket',photo=[{'file_id':'test-photo','file_unique_id':'test-photo-unique','width':1200,'height':500}])
        return Update.model_validate({'update_id':10,'callback_query':{'id':'cb1','from':{'id':user_id,'is_bot':False,'first_name':'Buyer'},
            'chat_instance':'abc','data':data,'message':message}})

    def screen_text(self):
        name,args,kwargs=self.bot.calls[-1]
        if name=='media':return kwargs['media'].caption
        return kwargs.get('text',kwargs.get('caption',args[1] if len(args)>1 else ''))

    def alert(self):
        args,kwargs=self.bot.acks[-1]
        self.assertTrue(kwargs.get('show_alert'))
        return kwargs['text']

    def message(self,text=None,payment=None):
        m={'message_id':2,'date':0,'chat':{'id':42,'type':'private'},'from':{'id':42,'is_bot':False,'first_name':'Buyer'}}
        if text:m['text']=text
        if payment:m['successful_payment']=payment
        return Update.model_validate({'update_id':11,'message':m})

    async def test_start_renders_menu(self):
        from aiogram.types import InlineKeyboardMarkup
        self.store.set_language(42,'ru')
        await self.module.handle(self.bot,self.message('/start'))
        _, args, kwargs = self.bot.calls[-1]
        markup = InlineKeyboardMarkup.model_validate(kwargs['reply_markup'])
        self.assertEqual(markup.inline_keyboard[0][0].callback_data,'catalog')
        self.assertEqual(markup.inline_keyboard[0][0].style,'success')
        self.assertIn('Buyer',kwargs['caption'])
        self.assertEqual(kwargs['parse_mode'],'HTML')

    async def test_welcome_uses_username_and_assigned_emoji_in_photo_caption(self):
        self.store.set_language(42,'ru')
        self.store.save_emoji_pack('Welcome','Welcome',[{'id':'12345','emoji':'👋','file_id':'test'}],'owner')
        self.store.assign_emoji('welcome:greeting','12345','owner')
        self.store.assign_emoji('welcome:actions','','owner')
        update=self.message('/start')
        user=update.message.from_user.model_copy(update={'username':'buyer_test'})
        update=update.model_copy(update={'message':update.message.model_copy(update={'from_user':user})})
        await self.module.handle(self.bot,update)
        name,_,kwargs=self.bot.calls[-1]
        self.assertEqual(name,'photo')
        self.assertIn('<tg-emoji emoji-id="12345">👋</tg-emoji> Привет, <a href="https://t.me/buyer_test">@buyer_test</a>!',kwargs['caption'])
        self.assertIn('Добро пожаловать в <b>TheMarket</b>',kwargs['caption'])
        self.assertEqual(kwargs['caption'].count('<tg-emoji'),2)
        self.assertTrue(kwargs['caption'].endswith('⚡️'))

    async def test_welcome_falls_back_if_telegram_rejects_custom_emoji(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendPhoto
        from unittest.mock import AsyncMock

        self.store.set_language(42,'en')
        error=TelegramBadRequest(method=SendPhoto(chat_id=42,photo='test'),message='custom emoji unavailable')
        self.bot.send_photo=AsyncMock(side_effect=[error,None])
        await self.module.handle(self.bot,self.message('/start'))
        rich,plain=(call.kwargs for call in self.bot.send_photo.await_args_list)
        self.assertEqual(rich['caption'].count('<tg-emoji'),3)
        self.assertNotIn('<tg-emoji',plain['caption'])
        self.assertIn('Hi, Buyer!',plain['caption'])
        self.assertEqual(plain['reply_markup'],rich['reply_markup'])

    def enable_channel(self):
        from channel_gate import ChannelGate
        import time
        gate=ChannelGate(self.store)
        gate.save(True,dict(channel_ref='@TheMarketFresh',join_url='https://t.me/TheMarketFresh',chat_id=-1001234567890,title='TheMarket <Fresh>',checked_at=time.time()),'owner')
        self.bot.id=9876
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        async def membership(chat_id,user_id,**kwargs):return SimpleNamespace(status='administrator' if user_id==9876 else 'left')
        self.bot.get_chat_member=AsyncMock(side_effect=membership)
        return gate

    async def test_first_entry_language_then_channel_then_shop_in_same_message(self):
        gate=self.enable_channel()
        await self.module.handle(self.bot,self.message('/start'))
        self.assertIn('Choose your language',self.screen_text());self.bot.get_chat_member.assert_not_awaited()
        await self.module.handle(self.bot,self.callback('lang:ru',photo=True))
        self.assertIn('Подпишись',self.screen_text());self.assertIn('&lt;Fresh&gt;',self.screen_text())
        rows=self.bot.calls[-1][2]['reply_markup']['inline_keyboard']
        self.assertEqual(rows[0][0]['url'],'https://t.me/TheMarketFresh')
        self.assertEqual(rows[1][0]['callback_data'],'channel:check')
        self.assertFalse(any(b.get('callback_data')=='home' for row in rows for b in row))
        self.assertEqual(self.bot.calls[-1][0],'media')
        with self.store.tx() as db:db.execute('UPDATE channel_access SET next_check=0')
        from types import SimpleNamespace
        self.bot.get_chat_member.side_effect=None;self.bot.get_chat_member.return_value=SimpleNamespace(status='administrator')
        await self.module.handle(self.bot,self.callback('channel:check',photo=True))
        self.assertIn('Привет, Buyer',self.screen_text());self.assertIsNone(gate.required(42))
        previous=self.bot.get_chat_member.await_count
        await self.module.handle(self.bot,self.callback('home',photo=True))
        self.assertEqual(self.bot.get_chat_member.await_count,previous)

    async def test_pending_user_cannot_bypass_with_commands_or_old_product_buttons(self):
        self.enable_channel();self.store.set_language(42,'ru')
        pid=self.store.save_product(dict(category='Test',name='License',description='',price=100,mode='manual',instruction='',active=True))
        for action in ('catalog','home','wallet','checkout:'+str(pid),'buy:'+str(pid),'product:'+str(pid)):
            await self.module.handle(self.bot,self.callback(action))
            self.assertIn('Подпишись',self.screen_text())
        for command in ('/wallet','/topup 100','/usdt 5'):
            await self.module.handle(self.bot,self.message(command));self.assertIn('Подпишись',self.screen_text())
        self.assertEqual(self.store.orders(42),[]);self.bot.get_chat_member.assert_not_awaited()

    async def test_channel_check_failure_and_english_screen_do_not_unlock(self):
        gate=self.enable_channel();self.store.set_language(42,'en')
        await self.module.handle(self.bot,self.callback('channel:check',photo=True))
        self.assertIn('Join our store channel',self.screen_text());self.assertIn('not joined',self.screen_text())
        self.assertIsNotNone(gate.required(42))
        await self.module.handle(self.bot,self.callback('channel:check',photo=True));self.assertIn('wait',self.screen_text())

    async def test_existing_visitor_is_not_asked_to_subscribe(self):
        self.store.remember_customer(42);self.store.set_language(42,'ru');self.enable_channel()
        await self.module.handle(self.bot,self.message('/start'));self.assertIn('Привет, Buyer',self.screen_text())
        self.bot.get_chat_member.assert_not_awaited()

    async def test_support_and_accepted_payment_are_not_blocked_by_onboarding(self):
        gate=self.enable_channel();self.store.set_language(42,'ru')
        with patch.dict(os.environ,{'SUPPORT_CONTACT':'@TestSupport'}):
            await self.module.handle(self.bot,self.callback('support'));self.assertIn('@TestSupport',self.screen_text())
        topup=self.store.create_topup(42,100)
        payment={'currency':'XTR','total_amount':100,'invoice_payload':topup,
                 'telegram_payment_charge_id':'onboarding-charge','provider_payment_charge_id':''}
        await self.module.handle(self.bot,self.message(payment=payment))
        self.assertEqual(self.store.wallet(42)['balance'],100);self.assertIn('Зачислено',self.screen_text())
        self.assertIsNotNone(gate.required(42));self.bot.get_chat_member.assert_not_awaited()

    async def test_store_news_can_be_disabled_and_enabled_in_place(self):
        from broadcasts import Broadcasts
        self.store.set_language(42,'ru')
        await self.module.handle(self.bot,self.callback('news:off',photo=True))
        self.assertFalse(Broadcasts(self.store).preferences(42))
        self.assertIn('отключены',self.screen_text())
        self.assertEqual(self.bot.calls[-1][0],'media')
        await self.module.handle(self.bot,self.callback('news:on',photo=True))
        self.assertTrue(Broadcasts(self.store).preferences(42))
        self.assertIn('включены',self.screen_text())

    async def test_english_news_preferences_and_about_link(self):
        self.store.set_language(42,'en')
        await self.module.handle(self.bot,self.callback('about'))
        rows=self.bot.calls[-1][2]['reply_markup']['inline_keyboard']
        self.assertTrue(any(b['callback_data']=='news' and 'Notifications' in b['text'] for row in rows for b in row))
        await self.module.handle(self.bot,self.callback('news:off'))
        self.assertIn('Store news disabled',self.screen_text())

    async def test_first_start_asks_language_then_saves_choice(self):
        await self.module.handle(self.bot,self.message('/start'))
        kwargs=self.bot.calls[-1][2]
        self.assertEqual(self.store.language(42),'')
        self.assertEqual([b['callback_data'] for b in kwargs['reply_markup']['inline_keyboard'][0]],
                         ['lang:ru','lang:en'])
        await self.module.handle(self.bot,self.callback('lang:en'))
        kwargs=self.bot.calls[-1][2]
        self.assertIn('Hi, Buyer',self.screen_text())
        self.assertIn('Open catalog',str(kwargs['reply_markup']))
        self.assertIn('🌐 Language',str(kwargs['reply_markup']))
        self.assertEqual(self.store.language(42),'en')
        reopened=Store(self.store.path,self.key)
        self.assertEqual(reopened.language(42),'en')
        await self.module.handle(self.bot,self.message('/start'))
        self.assertIn('Hi, Buyer',self.screen_text())

    async def test_language_can_change_without_new_navigation_message(self):
        self.store.set_language(42,'en')
        await self.module.handle(self.bot,self.callback('language'))
        self.assertIn('Choose your language',self.screen_text())
        await self.module.handle(self.bot,self.callback('lang:ru'))
        self.assertIn('Привет, Buyer',self.screen_text())
        self.assertEqual(self.store.language(42),'ru')
        self.assertFalse(any(name=='message' for name,args,kwargs in self.bot.calls))
        await self.module.handle(self.bot,self.message('/language'))
        self.assertIn('Choose your language',self.screen_text())

    async def test_english_shop_sections_and_notifications(self):
        from i18n import language
        self.store.set_language(42,'en')
        self.store.set_currency('100','test')
        pid=self.store.save_product(dict(category='AI',name='Custom {name}',description='Описание от продавца',
            price=250,mode='manual',instruction='Seller instructions',active=True))
        for section,expected in [('catalog','Choose an item'),('wallet','Your wallet'),
                ('deposit','Choose a top-up method'),('stars','Choose an amount'),
                ('bsc','Choose a top-up amount'),('guide','How to buy'),
                ('profile','Your profile'),('checkout:'+str(pid),'Choose a payment method'),
                ('wbuy:'+str(pid),'Pay with balance'),('orders','My purchases')]:
            await self.module.handle(self.bot,self.callback(section))
            self.assertIn(expected,self.screen_text())
        await self.module.handle(self.bot,self.callback('product:'+str(pid)))
        self.assertIn('Описание от продавца',self.screen_text())
        self.assertIn('Custom {name}',self.screen_text())
        await self.module.handle(self.bot,self.callback('usdtopup:500'))
        invoice=self.bot.calls[-1][2]
        self.assertEqual(invoice['title'],'Wallet top-up')
        self.assertIn('Receive $5.00',invoice['description'])
        deposit=self.store.create_crypto_deposit(42,500)
        await self.module.check_crypto_notification(self.bot,deposit)
        self.assertIn('Transfer confirmed',self.bot.calls[-1][1][1])
        self.assertEqual(language.get(),'ru')

    async def test_language_is_isolated_and_invalid_choice_is_rejected(self):
        self.store.set_language(42,'en')
        self.assertEqual(self.store.language(99),'')
        await self.module.handle(self.bot,self.callback('lang:bad'))
        self.assertEqual(self.store.language(42),'en')
        self.assertIn('button is outdated',self.alert())
        self.store.set_language(42,'ru')
        await self.module.handle(self.bot,self.callback('wallet'))
        self.assertIn('Твой кошелёк',self.screen_text())
        with self.assertRaises(ValueError):self.store.set_language(42,'bad')

    async def test_bot_commands_include_language_in_both_languages(self):
        from unittest.mock import AsyncMock
        self.bot.set_my_commands=AsyncMock()
        await self.module.register_commands(self.bot)
        registered={call.kwargs['language_code']:call.args[0]
                    for call in self.bot.set_my_commands.call_args_list}
        self.assertEqual(registered['en'][1],{'command':'language','description':'Language'})
        self.assertEqual(registered['ru'][1],{'command':'language','description':'Язык'})

    async def test_english_errors_and_language_context_reset_on_failure(self):
        from i18n import language
        from unittest.mock import AsyncMock
        self.store.set_language(42,'en')
        pid=self.store.save_product(dict(category='AI',name='License',description='',
            price=100,mode='manual',instruction='',active=True))
        order=self.store.create_order(pid,42)
        await self.module.handle(self.bot,self.callback('wpay:'+order['id']))
        self.assertIn('Insufficient funds',self.alert())
        self.bot.edit_message_media=AsyncMock(side_effect=RuntimeError('test transport failure'))
        with self.assertLogs('themarket',level='ERROR'):
            await self.module.handle(self.bot,self.callback('wallet'))
        self.assertIn('Something went wrong',self.alert())
        self.assertEqual(language.get(),'ru')

    async def test_start_registers_customer_before_first_order(self):
        await self.module.handle(self.bot,self.message('/start'))
        customers=self.store.customers()
        self.assertEqual(len(customers),1)
        self.assertEqual(customers[0]['user_id'],42)
        self.assertEqual(customers[0]['first_name'],'Buyer')
        self.assertEqual(customers[0]['balance'],0)
        self.assertEqual(customers[0]['orders'],0)
        await self.module.handle(self.bot,self.message('/start'))
        self.assertEqual(len(self.store.customers()),1)

    async def test_callback_also_registers_returning_visitor(self):
        await self.module.handle(self.bot,self.callback('wallet'))
        self.assertEqual(self.store.customers()[0]['user_id'],42)

    async def test_custom_emoji_id_is_returned_and_used_in_catalog(self):
        from aiogram.types import InlineKeyboardMarkup, MessageEntity
        emoji_id='5368324170671202286'
        update=self.message('🎬')
        entity=MessageEntity(type='custom_emoji',offset=0,length=2,custom_emoji_id=emoji_id)
        update=update.model_copy(update={'message':update.message.model_copy(update={'entities':[entity]})})
        await self.module.handle(self.bot,update)
        self.assertNotIn(emoji_id,str(self.bot.calls))
        with patch.dict(os.environ,{'ADMIN_CHAT_ID':'42'}):
            await self.module.handle(self.bot,update)
        self.assertIn(emoji_id,self.bot.calls[-1][1][1])
        # Several ids in ADMIN_CHAT_ID, or the panel's admin chats, also count as admins.
        for env in ('777, 42', '777;42 -100123'):
            self.bot.calls.clear()
            with self.subTest(env=env),patch.dict(os.environ,{'ADMIN_CHAT_ID':env}):
                await self.module.handle(self.bot,update)
                self.assertIn(emoji_id,self.bot.calls[-1][1][1])
        self.assertFalse(self.module.is_admin(4))
        with patch.dict(os.environ,{'ADMIN_CHAT_ID':''}):
            self.assertFalse(self.module.is_admin(42))
            self.module.notify.Notify(self.store).save({'admin_chat_ids':'777, 42'},'owner')
            self.assertTrue(self.module.is_admin(42))
            self.assertFalse(self.module.is_admin(7))
            self.bot.calls.clear()
            await self.module.handle(self.bot,update)
            self.assertIn(emoji_id,self.bot.calls[-1][1][1])
        self.store.save_product(dict(category='Video',name='CapCut',description='',price=100,
                                     mode='manual',instruction='',active=True,emoji_id=emoji_id))
        await self.module.handle(self.bot,self.callback('catalog'))
        markup=InlineKeyboardMarkup.model_validate(self.bot.calls[-1][2]['reply_markup'])
        self.assertEqual(markup.inline_keyboard[0][0].icon_custom_emoji_id,emoji_id)

    async def test_library_icons_apply_to_products_and_menu(self):
        self.store.save_emoji_pack('Brands','Brands',[{'id':'12345','emoji':'🎬','file_id':'preview'}],'test')
        self.store.save_product(dict(category='Video',name='CapCut',description='',price=100,
                                     mode='manual',instruction='',active=True))
        product_id=self.store.products()[0]['id']
        self.store.assign_emoji('product:'+str(product_id),'12345','owner')
        self.store.assign_emoji('menu:catalog','12345','owner')
        await self.module.handle(self.bot,self.callback('catalog'))
        rows=self.bot.calls[-1][2]['reply_markup']['inline_keyboard']
        self.assertEqual(rows[0][0]['icon_custom_emoji_id'],'12345')
        await self.module.handle(self.bot,self.callback('home'))
        rows=self.bot.calls[-1][2]['reply_markup']['inline_keyboard']
        self.assertEqual(rows[0][0]['icon_custom_emoji_id'],'12345')

    def buttons(self):
        return [b for row in self.bot.calls[-1][2]['reply_markup']['inline_keyboard'] for b in row]

    async def test_single_category_catalog_lists_products_directly(self):
        visible=[]
        for name,active in [('CapCut',True),('ChatGPT',True),('Hidden',False)]:
            product_id=self.store.save_product(dict(category='AI',name=name,description='',price=100,
                mode='manual',instruction='',active=active))
            if active:visible.append('product:'+str(product_id))
        for callback in ('catalog','cat:0','cat:99'):
            await self.module.handle(self.bot,self.callback(callback))
            callbacks=[b['callback_data'] for b in self.buttons()]
            self.assertEqual(set(callbacks),set(visible+['home']))
            self.assertIn('Выбери товар',self.screen_text())

    async def test_categories_and_pagination(self):
        self.store.save_emoji_pack('Brands','Brands',[{'id':'12345','emoji':'🎬','file_id':''}],'owner')
        video=self.store.save_product(dict(category='Video',name='CapCut',description='',price=100,
            mode='manual',instruction='',active=True))
        ai=[self.store.save_product(dict(category='AI',name='Item '+str(i),description='',price=100,
            mode='manual',instruction='',active=True)) for i in range(19)]
        self.store.assign_emoji('category:Video','12345','owner')
        await self.module.handle(self.bot,self.callback('catalog'))
        self.assertIn('Выбери категорию',self.screen_text())
        categories={b['text']:b for b in self.buttons() if b['callback_data'].startswith('cat:')}
        self.assertEqual(set(categories),{'AI · 19','Video · 1'})
        self.assertEqual(categories['Video · 1']['icon_custom_emoji_id'],'12345')
        await self.module.handle(self.bot,self.callback(categories['AI · 19']['callback_data']))
        callbacks=[b['callback_data'] for b in self.buttons()]
        self.assertEqual([c for c in callbacks if c.startswith('product:')],['product:'+str(p) for p in ai[:8]])
        self.assertIn('catalog',callbacks)
        self.assertIn('1 / 3',[b['text'] for b in self.buttons()])
        forward=next(b['callback_data'] for b in self.buttons() if b['text']=='→')
        for _ in range(2):
            await self.module.handle(self.bot,self.callback(forward))
            forward=next((b['callback_data'] for b in self.buttons() if b['text']=='→'),None)
        callbacks=[b['callback_data'] for b in self.buttons()]
        self.assertEqual([c for c in callbacks if c.startswith('product:')],['product:'+str(p) for p in ai[16:]])
        self.assertIsNone(forward)
        self.assertTrue(all(len(c.encode())<64 for c in callbacks))
        # The product card's back button returns to the page that contains the product.
        await self.module.handle(self.bot,self.callback('product:'+str(ai[10])))
        back=next(b['callback_data'] for b in self.buttons() if b['callback_data'].startswith('pback:'))
        await self.module.handle(self.bot,self.callback(back))
        self.assertIn('product:'+str(ai[10]),[b['callback_data'] for b in self.buttons()])
        self.assertIn('2 / 3',[b['text'] for b in self.buttons()])
        await self.module.handle(self.bot,self.callback('pback:'+str(video)))
        self.assertIn('product:'+str(video),[b['callback_data'] for b in self.buttons()])

    async def test_catalog_with_more_than_a_hundred_products_stays_valid(self):
        for i in range(130):
            self.store.save_product(dict(category='Bulk',name='Product '+str(i),description='',price=100,
                mode='manual',instruction='',active=True))
        await self.module.handle(self.bot,self.callback('catalog'))
        self.assertLessEqual(len(self.buttons()),14)

    async def test_catalog_displays_stock_and_empty_product_is_red(self):
        ready=self.store.save_product(dict(category='Keys',name='Ready',description='',price=100,
            mode='auto',instruction='',active=True,emoji_id='12345'))
        empty=self.store.save_product(dict(category='Keys',name='Empty',description='',price=100,
            mode='auto',instruction='',active=True))
        manual=self.store.save_product(dict(category='Keys',name='Service',description='',price=100,
            mode='manual',instruction='',active=True))
        self.store.import_stock(ready,'first\nsecond')
        await self.module.handle(self.bot,self.callback('catalog'))
        buttons={b['callback_data']:b for row in self.bot.calls[-1][2]['reply_markup']['inline_keyboard'] for b in row}
        self.assertIn('📦 2 шт.',buttons['product:'+str(ready)]['text'])
        self.assertEqual(buttons['product:'+str(ready)]['icon_custom_emoji_id'],'12345')
        self.assertEqual(buttons['product:'+str(ready)]['style'],'success')
        self.assertIn('📦 0 шт.',buttons['product:'+str(empty)]['text'])
        self.assertEqual(buttons['product:'+str(empty)]['style'],'danger')
        self.assertIn('Под заказ',buttons['product:'+str(manual)]['text'])
        self.assertEqual(buttons['product:'+str(manual)]['style'],'primary')

    async def test_pending_txid_can_be_retried_and_notifies_once(self):
        from unittest.mock import AsyncMock
        from bep20 import PendingPayment, VerifiedTransfer
        self.store.set_currency('100','test')
        deposit=self.store.create_crypto_deposit(42,1000)
        self.store.crypto_context(42,deposit['id'])
        txid='0x'+'a'*64
        with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(side_effect=PendingPayment('Ожидаем подтверждения'))):
            await self.module.handle(self.bot,self.message(txid))
            await self.module.wait_background()
        self.assertEqual(self.store.payment_list()['crypto'][0]['submitted'],txid)
        self.assertEqual(self.store.wallet(42)['balance'],0)
        self.store.retry_crypto_check(deposit['id'],'owner')
        queued=self.store.crypto_checks_due()[0]
        proof=VerifiedTransfer(txid,deposit['wallet'],deposit['token'],deposit['amount_raw'],deposit['created'],100,'0x'+'b'*64)
        with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(return_value=proof)):
            await self.module.verify_crypto(queued,queued['submitted'])
        await self.module.check_crypto_notification(self.bot,self.store.crypto_notifications_due()[0])
        self.assertEqual(self.store.wallet(42)['balance'],1000)
        self.assertEqual(self.store.crypto_checks_due(),[])
        self.assertEqual(self.store.crypto_notifications_due(),[])

    def mismatch(self,text):
        cls=getattr(self.module,'PaymentMismatch',None) or ValueError
        return cls(text)

    async def test_wrong_transfers_use_five_attempts_and_cancel(self):
        from unittest.mock import AsyncMock
        self.store.set_currency('100','test')
        await self.module.handle(self.bot,self.callback('bsc:500'))
        did=self.store.crypto_context(42)
        self.assertTrue(any(name=='media' for name,args,kwargs in self.bot.calls))
        self.assertFalse(any(b.get('callback_data','').startswith('bscqr:') for row in self.bot.calls[-1][2]['reply_markup']['inline_keyboard'] for b in row))
        for index in range(5):
            await self.module.handle(self.bot,self.callback('bscpaid:'+did))
            self.assertEqual(self.store.input_mode(42),'txid')
            update=self.message('0x'+str(index)*64).model_copy(update={'update_id':100+index})
            with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(side_effect=self.mismatch('Wrong amount'))):
                await self.module.handle(self.bot,update)
                await self.module.wait_background()
            text=self.bot.calls[-1][1][1]
            if index<4:self.assertIn('Осталось попыток: '+str(4-index),text)
            else:self.assertIn('Пополнение отменено',text)
            if index==0:
                # The same Telegram update replayed never costs a second attempt.
                await self.module.handle(self.bot,update)
                await self.module.wait_background()
                self.assertEqual(self.store.crypto_deposit(did,42)['failed_attempts'],1)
        self.assertTrue(self.store.crypto_deposit(did,42)['cancelled'])
        self.assertEqual(self.store.wallet(42)['balance'],0)
        await self.module.handle(self.bot,self.callback('bscpaid:'+did))
        self.assertIn('Пополнение отменено',self.alert())

    async def test_chat_text_in_txid_mode_never_burns_attempts(self):
        from unittest.mock import AsyncMock
        from bep20 import PendingPayment
        self.store.set_currency('100','test');self.store.set_language(42,'ru')
        await self.module.handle(self.bot,self.callback('bsc:500'))
        did=self.store.crypto_context(42)
        await self.module.handle(self.bot,self.callback('bscpaid:'+did))
        for index,text in enumerate(('ok','когда?','0x123','not-a-txid')):
            await self.module.handle(self.bot,self.message(text).model_copy(update={'update_id':200+index}))
            self.assertIn('не похоже на TXID',self.bot.calls[-1][1][1])
            self.assertEqual(self.store.input_mode(42),'txid')
        self.assertEqual(self.store.crypto_deposit(did,42)['failed_attempts'],0)
        txid='0x'+'a'*64
        with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(side_effect=PendingPayment('Ожидаем подтверждения: 3/20. Повтори проверку позже.'))):
            await self.module.handle(self.bot,self.message(txid).model_copy(update={'update_id':210}))
            await self.module.wait_background()
        # Pending clears the TXID mode: ordinary chat afterwards is just chat.
        self.assertEqual(self.store.input_mode(42),'')
        self.assertIn('3/20',self.bot.calls[-1][1][1])
        for index in range(6):
            await self.module.handle(self.bot,self.message('ok').model_copy(update={'update_id':220+index}))
            self.assertIn('Привет',self.screen_text())
        deposit=self.store.crypto_deposit(did,42)
        self.assertEqual(deposit['failed_attempts'],0);self.assertIsNone(deposit['cancelled'])
        # Resending the queued TXID does not start another verification.
        with patch.object(self.module.BscVerifier,'verify',new=AsyncMock()) as verify:
            await self.module.handle(self.bot,self.message(txid).model_copy(update={'update_id':230}))
            await self.module.wait_background()
        verify.assert_not_awaited()
        self.assertIn('уже проверяется',self.bot.calls[-1][1][1])

    async def test_verifier_crash_is_pending_and_english_pending_detail_is_translated(self):
        from unittest.mock import AsyncMock
        self.store.set_currency('100','test');self.store.set_language(42,'en')
        deposit=self.store.create_crypto_deposit(42,500)
        self.store.crypto_context(42,deposit['id'])
        with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(side_effect=RuntimeError('boom'))):
            await self.module.handle(self.bot,self.message('0x'+'d'*64))
            await self.module.wait_background()
        self.assertEqual(self.store.crypto_deposit(deposit['id'],42)['failed_attempts'],0)
        self.assertIn('not confirmed yet',self.bot.calls[-1][1][1])
        self.assertEqual(self.store.payment_list()['crypto'][0]['status'],'pending')
        from i18n import language
        token=language.set('en')
        try:
            self.assertEqual(self.module.error_text('Ожидаем подтверждения: 3/20. Повтори проверку позже.'),
                             'Waiting for confirmations: 3/20. Please check again later.')
        finally:language.reset(token)

    async def test_with_payment_mismatch_only_it_uses_attempts(self):
        from unittest.mock import AsyncMock
        class Mismatch(ValueError):pass
        self.store.set_currency('100','test')
        deposit=self.store.create_crypto_deposit(42,500)
        self.store.crypto_context(42,deposit['id'])
        with patch.object(self.module,'PaymentMismatch',Mismatch):
            with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(side_effect=ValueError('RPC glitch'))):
                await self.module.handle(self.bot,self.message('0x'+'1'*64).model_copy(update={'update_id':301}))
                await self.module.wait_background()
            self.assertEqual(self.store.crypto_deposit(deposit['id'],42)['failed_attempts'],0)
            with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(side_effect=Mismatch('Wrong amount'))):
                await self.module.handle(self.bot,self.message('0x'+'2'*64).model_copy(update={'update_id':302}))
                await self.module.wait_background()
            self.assertEqual(self.store.crypto_deposit(deposit['id'],42)['failed_attempts'],1)

    async def test_crypto_worker_survives_unexpected_verifier_errors(self):
        from unittest.mock import AsyncMock
        self.store.set_currency('100','test')
        deposit=self.store.create_crypto_deposit(42,500)
        self.store.queue_crypto_check(deposit['id'],'0x'+'e'*64,'ref-1')
        self.store.retry_crypto_check(deposit['id'],'owner')
        with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(side_effect=KeyError('x'))):
            await self.module.crypto_round(self.bot)
        self.assertEqual(self.store.crypto_deposit(deposit['id'],42)['failed_attempts'],0)

    async def test_crypto_qr_requisites_and_buttons_share_one_message(self):
        from aiogram.types import InlineKeyboardMarkup
        self.store.set_currency('100','test')
        wallets={'ton':'0:'+'1'*64,'tron':'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t',
                 'aptos':'0x'+'1'*64}
        for network,wallet in wallets.items():
            self.store.save_payment_method(network,True,wallet,'','owner')
        pid=self.store.save_product(dict(category='Test',name='Item',description='',price=33333,
            mode='manual',instruction='',active=True))
        user_id=100
        for code in ('ru','en'):
            for network in self.module.NETWORKS:
                for purchase in (False,True):
                    with self.subTest(language=code,network=network,purchase=purchase):
                        user_id+=1
                        self.store.set_language(user_id,code)
                        self.bot.calls.clear()
                        if purchase:
                            callback=('cbuy:'+str(pid)+':3' if network=='bsc'
                                      else 'netbuy:'+network+':'+str(pid)+':3')
                        else:
                            callback='bsc:100000' if network=='bsc' else 'netamount:'+network+':100000'
                        await self.module.handle(self.bot,self.callback(callback,user_id))
                        messages=[call for call in self.bot.calls if call[0]!='ack']
                        self.assertEqual(len(messages),1)
                        name,args,kwargs=messages[0]
                        self.assertEqual(name,'media')
                        self.assertTrue(kwargs['media'].media.data.startswith(b'\x89PNG\r\n\x1a\n'))
                        deposit=self.store.crypto_deposit(self.store.crypto_context(user_id),user_id)
                        caption=kwargs['media'].caption
                        self.assertIn(deposit['wallet'],caption)
                        self.assertIn(self.module.deposit_amount(deposit),caption)
                        self.assertIn(self.module.NETWORKS[network]['label'],caption)
                        self.assertIn('15',caption)
                        self.assertIn(('Оплата заказа' if purchase else 'Пополнение') if code=='ru'
                                      else ('Order payment' if purchase else 'Top-up'),caption)
                        self.assertLessEqual(len(caption.encode('utf-16-le'))//2,1024)
                        self.assertEqual(kwargs['media'].parse_mode,'HTML')
                        markup=InlineKeyboardMarkup.model_validate(kwargs['reply_markup'])
                        self.assertEqual(markup.inline_keyboard[0][0].copy_text.text,deposit['wallet'])
                        self.assertEqual(markup.inline_keyboard[0][1].copy_text.text,self.module.deposit_amount(deposit))
                        actions=[button.callback_data for row in markup.inline_keyboard for button in row]
                        self.assertIn('bscpaid:'+deposit['id'],actions)
                        self.assertIn('wallet',actions)
                        self.assertNotIn('bscqr:'+deposit['id'],actions)
    async def test_valid_shape_wrong_transfer_counts_but_pending_does_not(self):
        from unittest.mock import AsyncMock
        from bep20 import PendingPayment
        self.store.set_currency('100','test')
        deposit=self.store.create_crypto_deposit(42,500)
        self.store.crypto_context(42,deposit['id'])
        txid='0x'+'a'*64
        with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(side_effect=PendingPayment('Waiting'))):
            await self.module.handle(self.bot,self.message(txid).model_copy(update={'update_id':101}))
            await self.module.wait_background()
        self.assertEqual(self.store.crypto_deposit(deposit['id'],42)['failed_attempts'],0)
        self.assertIn('Waiting',self.bot.calls[-1][1][1])
        other='0x'+'c'*64
        with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(side_effect=self.mismatch('Wrong amount'))):
            await self.module.handle(self.bot,self.message(other).model_copy(update={'update_id':102}))
            await self.module.wait_background()
        self.assertEqual(self.store.crypto_deposit(deposit['id'],42)['failed_attempts'],1)
        self.assertIn('Осталось попыток: 4',self.bot.calls[-1][1][1])

    async def test_custom_amount_for_each_topup_method(self):
        self.store.set_currency('100','test')
        await self.module.handle(self.bot,self.callback('custom:stars'))
        await self.module.handle(self.bot,self.message('12,50'))
        invoice=next(kwargs for name,args,kwargs in self.bot.calls if name=='invoice')
        self.assertEqual(invoice['prices'][0]['amount'],1250)
        self.assertEqual(self.store.input_mode(42),'')
        await self.module.handle(self.bot,self.callback('custom:bsc'))
        await self.module.handle(self.bot,self.message('7.25'))
        d=self.store.crypto_deposit(self.store.crypto_context(42),42)
        self.assertEqual(d['credit_cents'],725)

    async def test_checkout_offers_every_supported_method(self):
        self.store.set_currency('100','test')
        pid=self.store.save_product(dict(category='Test',name='Item',description='',price=250,mode='manual',instruction='',active=True))
        await self.module.handle(self.bot,self.callback('checkout:'+str(pid)))
        markup=next(kwargs['reply_markup'] for name,args,kwargs in self.bot.calls if name=='edit')
        callbacks={b.get('callback_data') for row in markup['inline_keyboard'] for b in row}
        for prefix in ('wbuy:','buy:','netbuy:bsc:'):self.assertIn(prefix+str(pid)+':1',callbacks)

    async def test_new_network_settings_control_bot_and_plain_txid_credit(self):
        from unittest.mock import AsyncMock
        from bep20 import VerifiedTransfer
        self.store.set_currency('100','test')
        self.store.save_payment_method('ton',True,'0:'+'1'*64,'','owner')
        await self.module.handle(self.bot,self.callback('deposit'))
        buttons=[b for row in self.bot.calls[-1][2]['reply_markup']['inline_keyboard'] for b in row]
        self.assertIn('net:ton',[b['callback_data'] for b in buttons])
        self.assertNotIn('net:tron',[b['callback_data'] for b in buttons])
        await self.module.handle(self.bot,self.callback('net:ton'))
        self.assertIn('TON',self.screen_text())
        await self.module.handle(self.bot,self.callback('netamount:ton:500'))
        deposit=self.store.crypto_deposit(self.store.crypto_context(42),42)
        self.assertEqual(deposit['network'],'ton')
        self.assertIn('USDT · TON',self.screen_text())
        txid='a'*64
        proof=VerifiedTransfer(txid,deposit['wallet'],deposit['token'],deposit['amount_raw'],deposit['created'],100,'b'*64,'ton','trace-id')
        with patch.object(self.module.NetworkVerifier,'verify',new=AsyncMock(return_value=proof)):
            await self.module.handle(self.bot,self.message(txid))
            await self.module.wait_background()
        self.assertEqual(self.store.wallet(42)['balance'],500)
        self.store.set_stars_enabled(False,'owner')
        await self.module.handle(self.bot,self.callback('deposit'))
        self.assertNotIn("'callback_data': 'stars'",str(self.bot.calls[-1][2]))

    async def test_bep20_custom_icon_on_buttons_and_payment_screens(self):
        self.store.set_currency('100','test')
        for callback in ('deposit','bsc'):
            await self.module.handle(self.bot,self.callback(callback))
            kwargs=self.bot.calls[-1][2]
            media=kwargs['media']
            entity=media.caption_entities[0]
            self.assertEqual(entity.custom_emoji_id,'5242551409232069476' if callback=='deposit' else '5463427579966929312')
            encoded=media.caption.encode('utf-16-le')
            self.assertEqual(encoded[entity.offset*2:(entity.offset+entity.length)*2].decode('utf-16-le'),'💵')
            if callback=='deposit':
                button=kwargs['reply_markup']['inline_keyboard'][1][0]
                self.assertEqual(button['icon_custom_emoji_id'],'5463427579966929312')
                self.assertNotIn('💵',button['text'])
        await self.module.handle(self.bot,self.callback('bsc:500'))
        self.assertIn('<tg-emoji emoji-id="5463427579966929312">',self.screen_text())
        pid=self.store.save_product(dict(category='Test',name='Item',description='',price=100,
            mode='manual',instruction='',active=True))
        await self.module.handle(self.bot,self.callback('checkout:'+str(pid)))
        buttons=[b for row in self.bot.calls[-1][2]['reply_markup']['inline_keyboard'] for b in row]
        self.assertEqual(next(b for b in buttons if b['callback_data'].startswith('netbuy:bsc:'))['icon_custom_emoji_id'],'5463427579966929312')

    async def test_aptos_and_tron_custom_icons_and_bep20_display_name(self):
        self.store.set_currency('100','test')
        wallets={'aptos':'0x'+'1'*64,'tron':'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'}
        icons={'aptos':'5384362450432827393','tron':'5465394026448428029'}
        for network in wallets:self.store.save_payment_method(network,True,wallets[network],'','owner')
        await self.module.handle(self.bot,self.callback('deposit'))
        buttons=[b for row in self.bot.calls[-1][2]['reply_markup']['inline_keyboard'] for b in row]
        self.assertIn('USDT · BEP20',[b['text'] for b in buttons])
        for network,icon in icons.items():
            self.assertEqual(next(b for b in buttons if b['callback_data']=='net:'+network)['icon_custom_emoji_id'],icon)
            await self.module.handle(self.bot,self.callback('net:'+network))
            self.assertEqual(self.bot.calls[-1][2]['media'].caption_entities[0].custom_emoji_id,icon)
            await self.module.handle(self.bot,self.callback('netamount:'+network+':500'))
            self.assertIn('<tg-emoji emoji-id="'+icon+'">',self.screen_text())
        await self.module.handle(self.bot,self.callback('bsc'))
        self.assertIn('USDT · BEP20',self.screen_text())
        self.assertNotIn('BEP-20',self.screen_text())

    async def test_product_quantity_buttons_show_total_and_stock_limits(self):
        self.store.set_currency('100','test')
        pid=self.store.save_product(dict(category='Keys',name='Pass <Plus>',description='For account',
            price=1230,mode='auto',instruction='',active=True))
        self.store.import_stock(pid,'key-1\nkey-2')
        await self.module.handle(self.bot,self.callback('product:'+str(pid)))
        kwargs=self.bot.calls[-1][2]
        self.assertIn('Pass &lt;Plus&gt;',kwargs['text'])
        self.assertIn('$12.30',kwargs['text'])
        rows=kwargs['reply_markup']['inline_keyboard']
        self.assertEqual(rows[0][0]['callback_data'],'noop')
        self.assertEqual(rows[0][2]['callback_data'],'product:'+str(pid)+':2')
        await self.module.handle(self.bot,self.callback('product:'+str(pid)+':2'))
        rows=self.bot.calls[-1][2]['reply_markup']['inline_keyboard']
        self.assertEqual(rows[0][2]['callback_data'],'noop')
        self.assertIn('$24.60',rows[2][0]['text'])
        self.assertEqual(rows[2][0]['callback_data'],'checkout:'+str(pid)+':2')
        self.assertEqual(self.store.orders(),[])
        self.assertFalse(any(name=='message' for name,args,kwargs in self.bot.calls))

    async def test_product_card_has_custom_emoji_with_safe_fallback(self):
        from unittest.mock import AsyncMock
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import EditMessageText
        self.store.save_emoji_pack('Brands','Brands',[{'id':'12345','emoji':'🎬','file_id':''}],'owner')
        pid=self.store.save_product(dict(category='AI',name='ChatGPT <Plus>',description='Details',price=100,
            mode='manual',instruction='',active=True,emoji_id='12345'))
        await self.module.handle(self.bot,self.callback('product:'+str(pid)))
        text=self.screen_text()
        self.assertTrue(text.startswith('<tg-emoji emoji-id="12345">🎬</tg-emoji> <b>ChatGPT &lt;Plus&gt;</b>'))
        error=TelegramBadRequest(method=EditMessageText(text='test',chat_id=42,message_id=1),message='Bad Request: custom emoji unavailable')
        self.bot.edit_message_text=AsyncMock(side_effect=[error,None])
        await self.module.handle(self.bot,self.callback('product:'+str(pid)))
        self.assertTrue(self.bot.edit_message_text.call_args.kwargs['text'].startswith('🎬 <b>ChatGPT'))

    async def test_manual_quantity_input_edits_same_card_and_rejects_bad_amounts(self):
        pid=self.store.save_product(dict(category='Keys',name='Key',description='',price=100,
            mode='auto',instruction='',active=True))
        self.store.import_stock(pid,'first\nsecond\nthird')
        await self.module.handle(self.bot,self.callback('qtyinput:'+str(pid)+':1'))
        for text in ('0','2.5','-1','4','999999999'):
            await self.module.handle(self.bot,self.message(text))
            self.assertTrue(self.store.input_mode(42).startswith('qty:'))
            self.assertEqual(self.bot.calls[-1][2]['message_id'],1)
        await self.module.handle(self.bot,self.message('3'))
        self.assertEqual(self.store.input_mode(42),'')
        rows=self.bot.calls[-1][2]['reply_markup']['inline_keyboard']
        self.assertEqual(rows[0][1]['text'],'3 шт.')
        self.assertEqual(rows[2][0]['callback_data'],'checkout:'+str(pid)+':3')
        self.assertFalse(any(name=='message' for name,args,kwargs in self.bot.calls))

    async def test_quantity_is_preserved_for_every_payment_method(self):
        self.store.set_currency('100','test')
        pid=self.store.save_product(dict(category='Service',name='Pass',description='',price=250,
            mode='manual',instruction='',active=True))
        selection=str(pid)+':3'
        await self.module.handle(self.bot,self.callback('checkout:'+selection))
        kwargs=self.bot.calls[-1][2]
        self.assertIn('$7.50',kwargs['text'])
        buttons=[b['callback_data'] for row in kwargs['reply_markup']['inline_keyboard'] for b in row]
        for prefix in ('wbuy:','buy:','netbuy:bsc:'):self.assertIn(prefix+selection,buttons)
        for prefix in ('wbuy:','buy:','cbuy:'):
            await self.module.handle(self.bot,self.callback(prefix+selection))
        invoice=next(kwargs for name,args,kwargs in self.bot.calls if name=='invoice')
        self.assertEqual(invoice['prices'][0]['amount'],750)
        deposit=self.store.crypto_deposit(self.store.crypto_context(42),42)
        self.assertEqual(deposit['credit_cents'],750)
        # Repeated clicks for the same product and quantity reuse one reserved order.
        self.assertEqual(len(self.store.orders()),1)
        self.assertTrue(all(o['price']==750 and o['quantity']==3 for o in self.store.orders()))

    async def test_bulk_delivery_is_split_without_truncation(self):
        text='Order header\n'+'🔑'*2300+'\n'+('second secret\n'*400)
        await self.module.send_result(self.bot,42,text)
        chunks=[args[1] for name,args,kwargs in self.bot.calls if name=='message']
        self.assertGreater(len(chunks),1)
        self.assertEqual(''.join(chunks),text)
        self.assertTrue(all(len(chunk.encode('utf-16-le'))//2<=3500 for chunk in chunks))
        messages=[kwargs for name,args,kwargs in self.bot.calls if name=='message']
        self.assertTrue(all('reply_markup' not in kwargs for kwargs in messages[:-1]))
        self.assertEqual(messages[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data'],'home')

    async def test_delivery_receipt_heading_is_bold_without_parsing_product_data(self):
        heading='✅ Покупка успешно завершена!'
        text=heading+'\n\nТовар: <Product> (x1)\nСписано: $4.60\n\nsecret<&>'
        await self.module.send_result(self.bot,42,text)
        _,args,kwargs=self.bot.calls[-1]
        self.assertEqual(args[1],text)
        self.assertIsNone(kwargs['parse_mode'])
        self.assertEqual(kwargs['entities'][0].type,'bold')
        self.assertEqual(kwargs['entities'][0].length,len(heading.encode('utf-16-le'))//2)
        self.assertEqual(kwargs['reply_markup']['inline_keyboard'][0][0]['text'],'🏠 Главное меню')
        self.store.set_language(42,'en')
        await self.module.send_result(self.bot,42,text)
        self.assertEqual(self.bot.calls[-1][2]['reply_markup']['inline_keyboard'][0][0]['text'],'🏠 Main menu')

    async def test_purchases_menu_only_shows_successful_purchases_and_details(self):
        self.store.set_currency('100','test')
        pid=self.store.save_product(dict(category='Keys',name='Pass <Plus>',description='',price=1230,
            mode='auto',instruction='Activate here',active=True))
        self.store.import_stock(pid,'first-secret\nsecond-secret\nthird-secret')
        order=self.store.create_order(pid,42,2)
        self.store.paid(order['id'],42,2460,'XTR','purchase-test')
        pending=self.store.create_order(pid,42)
        await self.module.handle(self.bot,self.callback('orders'))
        kwargs=self.bot.calls[-1][2]
        rows=kwargs['reply_markup']['inline_keyboard']
        self.assertIn('Выбери товар',self.screen_text())
        self.assertEqual(rows[0][0]['callback_data'],'purchase:'+order['id']+':0')
        self.assertEqual(rows[0][0]['text'],'Pass <Plus>')
        self.assertNotIn(order['id'][:8],self.screen_text())
        self.assertNotIn(pending['id'],str(kwargs))
        self.assertNotIn('first-secret',str(kwargs))
        await self.module.handle(self.bot,self.callback(rows[0][0]['callback_data']))
        kwargs=self.bot.calls[-1][2]
        self.assertIn('Pass &lt;Plus&gt;',kwargs['text'])
        self.assertIn('Количество: <b>2 шт.</b>',kwargs['text'])
        self.assertNotIn(order['id'][:8],kwargs['text'])
        self.assertEqual(kwargs['reply_markup']['inline_keyboard'][0][0]['callback_data'],'get:'+order['id'])
        self.assertFalse(any(name=='message' for name,args,kwargs in self.bot.calls))
        self.store.set_language(42,'en')
        await self.module.handle(self.bot,self.callback('purchase:'+order['id']+':0'))
        self.assertIn('Purchase completed',self.screen_text())
        await self.module.handle(self.bot,self.callback('get:'+order['id']))
        self.assertIn('second-secret',self.bot.calls[-1][1][1])

    async def test_empty_purchases_menu_has_catalog_and_no_unpaid_orders(self):
        pid=self.store.save_product(dict(category='Test',name='Unpaid product',description='',price=100,
            mode='manual',instruction='',active=True))
        order=self.store.create_order(pid,42)
        await self.module.handle(self.bot,self.callback('orders'))
        kwargs=self.bot.calls[-1][2]
        self.assertIn('Здесь будут твои успешные покупки',self.screen_text())
        self.assertNotIn('Unpaid product',str(kwargs))
        self.assertEqual(kwargs['reply_markup']['inline_keyboard'][0][0]['callback_data'],'catalog')
        await self.module.handle(self.bot,self.callback('purchase:'+order['id']+':0'))
        self.assertIn('Покупка не найдена',self.alert())
        self.assertIn('Здесь будут твои успешные покупки',self.screen_text())

    async def test_purchases_pages_have_at_most_five_named_order_buttons(self):
        pid=self.store.save_product(dict(category='Test',name='ChatGPT Plus',description='',price=100,
            mode='auto',instruction='',active=True))
        self.store.import_stock(pid,'\n'.join('secret-'+str(i) for i in range(6)))
        ids=[]
        for i in range(6):
            order=self.store.create_order(pid,42)
            ids.append(order['id'])
            self.store.paid(order['id'],42,100,'XTR','page-charge-'+str(i))
        for callback,count,indicator in [('orders',5,'1 / 2'),('orders:1',1,'2 / 2')]:
            await self.module.handle(self.bot,self.callback(callback))
            kwargs=self.bot.calls[-1][2]
            buttons=[b for row in kwargs['reply_markup']['inline_keyboard'] for b in row]
            purchases=[b for b in buttons if b['callback_data'].startswith('purchase:')]
            self.assertEqual(len(purchases),count)
            self.assertTrue(all(b['text']=='ChatGPT Plus' for b in purchases))
            self.assertIn(indicator,[b['text'] for b in buttons])
            visible=self.screen_text()+' '.join(b['text'] for b in buttons)
            self.assertTrue(all(oid[:8] not in visible for oid in ids))

    async def test_purchase_invoice_and_successful_payment(self):
        pid=self.store.save_product(dict(category='Keys',name='Long subscription name longer than invoice limit',description='',price=10,mode='auto',instruction='Use key',active=True))
        self.store.import_stock(pid,'secret-license')
        await self.module.handle(self.bot,self.callback('buy:'+str(pid)))
        invoice=next(kwargs for name,args,kwargs in self.bot.calls if name=='invoice')
        self.assertLessEqual(len(invoice['title']),32)
        self.assertEqual(invoice['currency'],'XTR')
        payment={'invoice_payload':invoice['payload'],'currency':'XTR','total_amount':10,
                 'telegram_payment_charge_id':'test-charge','provider_payment_charge_id':''}
        await self.module.handle(self.bot,self.message(payment=payment))
        await self.module.deliver(self.bot)
        self.assertIn('secret-license',str(self.bot.calls))
        self.assertEqual(self.store.orders()[0]['status'],'fulfilled')

    async def test_out_of_stock_explained(self):
        pid=self.store.save_product(dict(category='Keys',name='Key',description='',price=10,mode='auto',instruction='',active=True))
        await self.module.handle(self.bot,self.callback('buy:'+str(pid)))
        self.assertIn('Товар закончился',self.alert())
        self.assertEqual(self.store.orders(),[])

    async def test_wallet_and_topup_payment(self):
        await self.module.handle(self.bot,self.callback('wallet'))
        self.assertIn('Telegram ID: <code>42</code>',self.screen_text())
        await self.module.handle(self.bot,self.callback('topup:250'))
        invoice=next(kwargs for name,args,kwargs in self.bot.calls if name=='invoice')
        payment={'invoice_payload':invoice['payload'],'currency':'XTR','total_amount':250,
                 'telegram_payment_charge_id':'topup-charge','provider_payment_charge_id':''}
        await self.module.handle(self.bot,self.message(payment=payment))
        await self.module.handle(self.bot,self.message(payment=payment))
        self.assertEqual(self.store.wallet(42)['balance'],250)

    async def test_topup_replay_confirms_only_once(self):
        await self.module.handle(self.bot,self.callback('topup:250'))
        invoice=next(kwargs for name,args,kwargs in self.bot.calls if name=='invoice')
        payment={'invoice_payload':invoice['payload'],'currency':'XTR','total_amount':250,
                 'telegram_payment_charge_id':'replay-charge','provider_payment_charge_id':''}
        for _ in range(2):await self.module.handle(self.bot,self.message(payment=payment))
        confirmations=[c for c in self.bot.calls if 'Зачислено' in str(c)]
        self.assertEqual(len(confirmations),1)

    async def test_payment_kept_on_disk_when_database_fails_and_retried(self):
        import sqlite3
        pid=self.store.save_product(dict(category='Keys',name='Key',description='',price=10,mode='auto',instruction='',active=True))
        self.store.import_stock(pid,'disk-secret')
        await self.module.handle(self.bot,self.callback('buy:'+str(pid)))
        invoice=next(kwargs for name,args,kwargs in self.bot.calls if name=='invoice')
        payment={'invoice_payload':invoice['payload'],'currency':'XTR','total_amount':10,
                 'telegram_payment_charge_id':'locked-charge','provider_payment_charge_id':''}
        async def no_sleep(*_):pass
        with patch.object(self.store,'paid',side_effect=sqlite3.OperationalError('database is locked')),\
             patch.object(self.module.asyncio,'sleep',new=no_sleep),self.assertLogs('themarket',level='ERROR'):
            await self.module.handle(self.bot,self.message(payment=payment))
        path=self.module.pending_payments_path()
        self.assertTrue(path.exists())
        self.assertEqual(self.store.orders()[0]['status'],'pending')
        with self.assertLogs('themarket',level='WARNING'):
            await self.module.retry_pending_payments(self.bot)
        self.assertFalse(path.exists())
        self.assertEqual(self.store.orders()[0]['status'],'fulfilled')

    async def error_alerts(self):
        """Audit warnings/errors -> notify queue -> admin chat (what the service worker runs)."""
        self.module.queue_error_alerts()
        await self.module.send_staff_notifications(self.bot)

    async def test_admin_alerts_give_up_when_admin_chat_unreachable(self):
        from unittest.mock import AsyncMock
        self.module.ADMIN_ALERTS['paused_until']=0
        with patch.dict(os.environ,{'ADMIN_CHAT_ID':'777'}):
            await self.error_alerts()  # initialises the cursor
            with self.store.tx() as db:self.store.log(db,'system','channel.check.failed','channel',level='error')
            self.bot.send_message=AsyncMock(side_effect=self.forbidden())
            with self.assertLogs('themarket',level='WARNING'):
                for _ in range(5):await self.error_alerts()
        self.assertEqual(self.bot.send_message.await_count,1)
        self.assertEqual(self.module.notify.Notify(self.store).pending_count(),0)

    async def test_admin_alerts_only_new_important_events_without_secrets(self):
        with patch.dict(os.environ,{'ADMIN_CHAT_ID':'99'}):
            with self.store.tx() as db:self.store.log(db,'bot','bot.offline','telegram',level='error')
            await self.error_alerts()  # history before the first run is not announced
            with self.store.tx() as db:
                self.store.log(db,'bot','bot.offline','telegram',level='error')
                self.store.log(db,'bot','bot.online','telegram')
            await self.error_alerts()
            await self.error_alerts()
        sent=[(args,kwargs) for name,args,kwargs in self.bot.calls if name=='message']
        self.assertEqual(len(sent),1)
        self.assertEqual(sent[0][0][0],99)
        self.assertIn('потерял связь',sent[0][0][1])

    async def test_payment_issue_is_announced_once(self):
        with patch.dict(os.environ,{'ADMIN_CHAT_ID':'99'}):
            await self.error_alerts()
            self.store._record_payment_issue('order','x'*32,42,100,'XTR','charge-1','Заказ не найден')
            await self.error_alerts()
            await self.error_alerts()
        sent=[args[1] for name,args,kwargs in self.bot.calls if name=='message']
        self.assertEqual(len(sent),1)
        self.assertIn('Платёж получен, но не проведён',sent[0])

    async def test_navigation_reuses_message_across_sections(self):
        with patch.dict(os.environ,{'SUPPORT_CONTACT':'@support','SHOP_TERMS':'Terms'}):
            for section in ('catalog','wallet','deposit','profile','guide','support','terms','about','orders','home'):
                await self.module.handle(self.bot,self.callback(section))
        edits=[kwargs for name,args,kwargs in self.bot.calls if name in ('edit','media')]
        self.assertEqual(len(edits),10)
        self.assertTrue(all(k['message_id']==1 and k['chat_id']==42 for k in edits))
        self.assertFalse(any(name=='message' for name,args,kwargs in self.bot.calls))

    async def test_photo_menu_has_six_actions_and_keeps_information_accessible(self):
        self.store.set_language(42,'ru')
        await self.module.handle(self.bot,self.message('/start'))
        name,args,kwargs=self.bot.calls[-1]
        self.assertEqual(name,'photo')
        self.assertTrue(Path(args[1].path).is_file())
        actions=[button['callback_data'] for row in kwargs['reply_markup']['inline_keyboard'] for button in row]
        self.assertEqual(actions,['catalog','promo','wallet','orders','support','language','about'])
        self.assertLessEqual(len(kwargs['reply_markup']['inline_keyboard']),4)
        await self.module.handle(self.bot,self.callback('about',photo=True))
        self.assertEqual(self.bot.calls[-1][0],'media')
        actions=[button['callback_data'] for row in self.bot.calls[-1][2]['reply_markup']['inline_keyboard'] for button in row]
        self.assertTrue({'guide','profile','terms','support','home'}.issubset(actions))

    async def test_photo_navigation_changes_the_same_message_and_preserves_entities(self):
        self.store.set_currency('100','test')
        sections=('catalog','wallet','deposit','profile','guide','support','terms','about','orders','language','news','home')
        with patch.dict(os.environ,{'SUPPORT_CONTACT':'@support','SHOP_TERMS':'Terms'}):
            for section in sections:
                await self.module.handle(self.bot,self.callback(section,photo=True))
        edits=[kwargs for name,args,kwargs in self.bot.calls if name=='media']
        self.assertEqual(len(edits),len(sections))
        self.assertTrue(all(kwargs['message_id']==1 and kwargs['chat_id']==42 for kwargs in edits))
        self.assertFalse(any(name in ('photo','message','delete') for name,args,kwargs in self.bot.calls))
        expected=[self.module.MENU_BANNER_DIR/(section+'.png') for section in sections[:-1]]+[self.module.BANNER_PATH]
        self.assertEqual([Path(kwargs['media'].media.path) for kwargs in edits],expected)
        media=edits[2]['media']
        entity=media.caption_entities[0]
        self.assertEqual(entity.custom_emoji_id,'5242551409232069476')
        self.assertEqual(media.caption.encode('utf-16-le')[entity.offset*2:(entity.offset+entity.length)*2].decode('utf-16-le'),'💵')

    async def test_qr_replaces_banner_and_home_restores_banner_in_place(self):
        self.store.set_currency('100','test')
        await self.module.handle(self.bot,self.callback('bsc:500',photo=True))
        name,args,kwargs=self.bot.calls[-1]
        self.assertEqual(name,'media');self.assertTrue(kwargs['media'].media.data.startswith(b'\x89PNG'))
        await self.module.handle(self.bot,self.callback('home',photo=True))
        self.assertEqual(self.bot.calls[-1][0],'media')
        self.assertEqual(Path(self.bot.calls[-1][2]['media'].media.path),self.module.BANNER_PATH)
        self.assertFalse(any(name in ('photo','message') for name,args,kwargs in self.bot.calls))

    async def test_product_is_text_only_after_leaving_photo_catalog(self):
        for description in ('Краткое описание','Длинное описание товара. '*42):
            with self.subTest(description=description[:25]):
                self.bot.calls.clear()
                pid=self.store.save_product(dict(category='Test',name='Product',description=description,price=100,
                    mode='manual',instruction='',active=True))
                await self.module.handle(self.bot,self.callback('product:'+str(pid),photo=True))
                calls=[call for call in self.bot.calls if call[0]!='ack']
                self.assertEqual([call[0] for call in calls],['message','delete'])
                self.assertIn(description.strip(),calls[0][1][1])
                self.assertEqual(calls[1][1],(42,1))

    async def test_legacy_photo_quantity_prompt_is_replaced_with_text_card(self):
        from unittest.mock import AsyncMock
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import EditMessageText
        pid=self.store.save_product(dict(category='Test',name='Product',description='',price=100,
            mode='manual',instruction='',active=True))
        self.store.input_mode(42,f'qty:{pid}:1:1')
        self.bot.edit_message_text=AsyncMock(side_effect=TelegramBadRequest(
            method=EditMessageText(text='quantity',chat_id=42,message_id=1),message='Bad Request: there is no text in the message to edit'))
        await self.module.handle(self.bot,self.message('3'))
        self.assertEqual([name for name,_,_ in self.bot.calls],['message','delete'])
        self.assertEqual(self.bot.calls[0][2]['reply_markup']['inline_keyboard'][0][1]['text'],'3 шт.')
        self.assertEqual(self.bot.calls[1][1],(42,1))

    async def test_caption_limits_count_rendered_utf16_and_wallet_hides_disabled_methods(self):
        self.assertTrue(self.module.fits_caption('<b>'+'💵'*512+'</b>','HTML'))
        self.assertFalse(self.module.fits_caption('💵'*513))
        self.store.set_stars_enabled(False,'owner')
        self.store.save_payment_method('bsc',False,'','','owner')
        await self.module.handle(self.bot,self.callback('wallet',photo=True))
        self.assertNotIn('Telegram Stars',self.screen_text())
        self.assertNotIn('BEP20',self.screen_text())
        self.assertIn('Пополнение временно недоступно',self.screen_text())

    async def test_unchanged_screen_does_not_send_duplicate(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import EditMessageMedia
        from aiogram.types import InputMediaPhoto
        from unittest.mock import AsyncMock
        self.bot.edit_message_media=AsyncMock(side_effect=TelegramBadRequest(
            method=EditMessageMedia(media=InputMediaPhoto(media='test'),chat_id=42,message_id=1),message='Bad Request: message is not modified'))
        await self.module.handle(self.bot,self.callback('wallet'))
        self.assertFalse(any(name in ('message','photo') for name,args,kwargs in self.bot.calls))

    async def test_deleted_screen_recovers_with_new_message(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import EditMessageMedia
        from aiogram.types import InputMediaPhoto
        from unittest.mock import AsyncMock
        self.bot.edit_message_media=AsyncMock(side_effect=TelegramBadRequest(
            method=EditMessageMedia(media=InputMediaPhoto(media='test'),chat_id=42,message_id=1),message='Bad Request: message to edit not found'))
        await self.module.handle(self.bot,self.callback('wallet'))
        self.assertEqual(sum(name=='photo' for name,args,kwargs in self.bot.calls),1)

    async def test_direct_usdt_invoice_and_txid_credit(self):
        from unittest.mock import AsyncMock
        from bep20 import VerifiedTransfer
        self.store.set_currency('100','test')
        await self.module.handle(self.bot,self.callback('bsc:1000'))
        did=self.store.crypto_context(42);d=self.store.crypto_deposit(did,42)
        from aiogram.types import InlineKeyboardMarkup
        invoice=next(kwargs for name,args,kwargs in self.bot.calls if name=='media')
        markup=InlineKeyboardMarkup.model_validate(invoice['reply_markup'])
        self.assertEqual(markup.inline_keyboard[0][0].copy_text.text,d['wallet'])
        await self.module.handle(self.bot,self.callback('bscqr:'+did))
        self.assertIn('Кнопка устарела',self.alert())
        photo=next(kwargs['media'].media for name,args,kwargs in self.bot.calls if name=='media')
        self.assertTrue(photo.data.startswith(b'\x89PNG\r\n\x1a\n'))
        txid='0x'+'a'*64
        proof=VerifiedTransfer(txid,d['wallet'],d['token'],d['amount_raw'],d['created'],100,'0x'+'b'*64)
        with patch.object(self.module.BscVerifier,'verify',new=AsyncMock(return_value=proof)) as verify:
            await self.module.handle(self.bot,self.message(txid))
            self.assertIn('проверяю',self.bot.calls[-1][1][1])
            await self.module.wait_background()
            self.assertIn('Зачислено',self.bot.calls[-1][1][1])
            await self.module.handle(self.bot,self.message(txid))
            await self.module.wait_background()
            self.assertEqual(verify.await_count,1)
        self.assertEqual(self.store.wallet(42)['balance'],1000)


    # ------------------------------------------------------------ robustness

    def forbidden(self):
        from aiogram.exceptions import TelegramForbiddenError
        from aiogram.methods import SendMessage
        return TelegramForbiddenError(method=SendMessage(chat_id=42,text='x'),message='Forbidden: bot was blocked by the user')

    async def test_poll_always_advances_offset_even_when_updates_fail(self):
        import asyncio
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        self.store.set_language(42,'ru');self.store.set_language(43,'ru')
        first=self.callback('wallet').model_copy(update={'update_id':500})
        second=self.callback('home',user_id=43).model_copy(update={'update_id':501})
        self.bot.edit_message_text=AsyncMock(side_effect=self.forbidden())
        self.bot.edit_message_media=AsyncMock(side_effect=[self.forbidden(),None])
        offsets=[]
        async def get_updates(offset,**kwargs):
            offsets.append(offset)
            self.assertEqual(kwargs['timeout'],self.module.POLL_TIMEOUT)
            if len(offsets)==1:return [first,second]
            raise asyncio.CancelledError
        self.bot.get_updates=get_updates
        with self.assertRaises(asyncio.CancelledError):
            await self.module.poll(self.bot,SimpleNamespace(username='shop'))
        self.assertEqual(offsets,[0,502])
        self.assertEqual(self.store.offset(),502)
        self.assertEqual(self.bot.edit_message_media.await_count,2)

    async def test_network_failure_reports_offline_and_recovers(self):
        import asyncio
        from types import SimpleNamespace
        calls=[]
        async def get_updates(offset,**kwargs):
            calls.append(offset)
            if len(calls)==1:raise OSError('network down')
            raise asyncio.CancelledError
        self.bot.get_updates=get_updates
        import contextlib,io
        with patch.object(self.module.asyncio,'sleep',new=self.no_sleep()),self.assertRaises(asyncio.CancelledError),\
             contextlib.redirect_stdout(io.StringIO()):
            await self.module.poll(self.bot,SimpleNamespace(username='shop'))
        self.assertIn('bot.offline',str(self.store.audits()))

    def no_sleep(self):
        async def sleep(*args,**kwargs):return None
        return sleep

    async def test_slow_user_does_not_block_others_and_user_order_is_kept(self):
        import asyncio
        order=[];released=asyncio.Event()
        async def fake_handle(bot,u):
            data=u.callback_query.data
            if data=='a1':await asyncio.wait_for(released.wait(),2)
            if data=='b1':released.set()
            order.append(data)
        updates=[self.callback('a1',user_id=1),self.callback('b1',user_id=2),self.callback('a2',user_id=1)]
        with patch.object(self.module,'handle',new=fake_handle):
            await self.module.Dispatcher(self.bot).process(updates)
        self.assertEqual(order,['b1','a1','a2'])

    async def test_blocked_user_and_failed_sends_never_raise(self):
        from unittest.mock import AsyncMock
        self.store.set_language(42,'ru')
        self.bot.send_message=AsyncMock(side_effect=self.forbidden())
        self.bot.send_photo=AsyncMock(side_effect=self.forbidden())
        with self.assertLogs('themarket',level='INFO') as logs:
            await self.module.handle(self.bot,self.message('/start'))
        self.assertIn('chat unavailable',' '.join(logs.output))

    async def test_payment_mismatch_gets_polite_reply_with_reference(self):
        pid=self.store.save_product(dict(category='Keys',name='Key',description='',price=10,mode='manual',instruction='',active=True))
        order=self.store.create_order(pid,42)
        payment={'invoice_payload':order['id'],'currency':'XTR','total_amount':999,
                 'telegram_payment_charge_id':'bad-charge','provider_payment_charge_id':''}
        with self.assertLogs('themarket',level='ERROR'):
            await self.module.handle(self.bot,self.message(payment=payment))
        text=self.bot.calls[-1][1][1]
        self.assertIn('Оплата получена',text);self.assertIn(order['id'][:8],text)
        self.assertEqual(self.bot.calls[-1][2]['reply_markup']['inline_keyboard'][0][0]['callback_data'],'support')
        topup=self.store.create_topup(42,100)
        payment.update(invoice_payload=topup,total_amount=5)
        with self.assertLogs('themarket',level='ERROR'):
            await self.module.handle(self.bot,self.message(payment=payment))
        self.assertIn(topup.removeprefix('topup:')[:8],self.bot.calls[-1][1][1])

    async def test_late_pre_checkout_answer_is_contained(self):
        from unittest.mock import AsyncMock
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import AnswerPreCheckoutQuery
        from aiogram.types import Update
        topup=self.store.create_topup(42,100)
        self.bot.answer_pre_checkout_query=AsyncMock(side_effect=TelegramBadRequest(
            method=AnswerPreCheckoutQuery(pre_checkout_query_id='q',ok=True),message='Bad Request: query is too old'))
        update=Update.model_validate({'update_id':12,'pre_checkout_query':{'id':'q','from':{'id':42,'is_bot':False,'first_name':'B'},
            'currency':'XTR','total_amount':100,'invoice_payload':topup}})
        with self.assertLogs('themarket',level='WARNING'):
            await self.module.handle(self.bot,update)
        self.assertEqual(self.bot.answer_pre_checkout_query.await_args.kwargs,{'ok':True})

    async def test_delivery_to_blocked_user_is_permanent_when_store_supports_it(self):
        from unittest.mock import AsyncMock
        recorded=[]
        def result(delivery_id,ok,permanent=False):recorded.append((delivery_id,ok,permanent))
        self.bot.send_message=AsyncMock(side_effect=self.forbidden())
        with patch.object(self.store,'deliveries',return_value=[{'id':7,'user_id':42,'order_id':None,'body':self.store.encrypt('hi')}]),\
             patch.object(self.store,'delivery_result',new=result),self.assertLogs('themarket',level='WARNING'):
            await self.module.deliver(self.bot)
        self.assertEqual(recorded,[(7,False,True)])

    async def test_service_worker_runs_housekeeping_and_survives_failures(self):
        import asyncio
        calls=[]
        def housekeeping():calls.append('hk');raise RuntimeError('db busy')
        async def failing_deliver(bot):calls.append('deliver');raise RuntimeError('boom')
        sleeps=[]
        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps)>=2:raise asyncio.CancelledError
        self.store.housekeeping=housekeeping
        with patch.object(self.module,'deliver',new=failing_deliver),patch.object(self.module.asyncio,'sleep',new=sleep),\
             self.assertLogs('themarket',level='ERROR'),self.assertRaises(asyncio.CancelledError):
            await self.module.service_worker(self.bot)
        self.assertEqual(calls,['deliver','hk','deliver'])

    # ------------------------------------------------------------ buttons and payment methods

    async def test_malformed_and_stale_callbacks_show_alert_without_touching_screen(self):
        for data in ('product:abc','product:1:2:3','netbuy:bsc','purchase:zzz','bscqr:'+'a'*32,'checkout:1:0','unknown','x'*70):
            self.bot.calls.clear()
            await self.module.handle(self.bot,self.callback(data))
            self.assertIn('Кнопка устарела',self.alert())
            self.assertEqual(self.bot.calls,[])

    async def test_orders_are_not_created_for_disabled_methods(self):
        self.store.set_currency('100','test')
        pid=self.store.save_product(dict(category='T',name='Item',description='',price=100,mode='manual',instruction='',active=True))
        self.store.set_stars_enabled(False,'owner')
        self.store.save_payment_method('bsc',False,'','','owner')
        for data in ('buy:'+str(pid),'netbuy:bsc:'+str(pid),'cbuy:'+str(pid),'netbuy:tron:'+str(pid)+':2',
                     'stars','custom:stars','custom:bsc','net:ton','netamount:bsc:500','usdtopup:500','topup:100'):
            await self.module.handle(self.bot,self.callback(data))
            self.assertIn('Способ оплаты недоступен',self.alert())
        self.assertEqual(self.store.orders(),[])
        self.assertEqual(self.store.input_mode(42),'')
        await self.module.handle(self.bot,self.callback('wallet'))
        self.assertNotIn("'deposit'",str(self.bot.calls[-1][2]['reply_markup']))

    async def test_repeated_buy_clicks_reuse_or_limit_pending_orders(self):
        pid=self.store.save_product(dict(category='T',name='Item',description='',price=100,mode='manual',instruction='',active=True))
        for _ in range(5):
            await self.module.handle(self.bot,self.callback('buy:'+str(pid)))
        # Contract item 3 reuses the pending order; the current store caps at three.
        self.assertLessEqual(len(self.store.orders()),3)

    async def test_stars_currency_mode_hides_usd_flows(self):
        self.store.set_language(42,'ru')
        pid=self.store.save_product(dict(category='T',name='Item',description='',price=100,mode='manual',instruction='',active=True))
        await self.module.handle(self.bot,self.callback('deposit'))
        callbacks=[b['callback_data'] for b in self.buttons()]
        self.assertIn('stars',callbacks);self.assertFalse(any(c.startswith('net:') for c in callbacks))
        await self.module.handle(self.bot,self.callback('stars'))
        callbacks=[b['callback_data'] for b in self.buttons()]
        self.assertIn('topup:250',callbacks);self.assertFalse(any(c.startswith('usdtopup:') for c in callbacks))
        self.assertNotIn('$',self.screen_text())
        await self.module.handle(self.bot,self.callback('checkout:'+str(pid)))
        self.assertFalse(any(b['callback_data'].startswith('netbuy:') for b in self.buttons()))
        await self.module.handle(self.bot,self.callback('wallet'))
        self.assertNotIn('USDT',self.screen_text())
        await self.module.handle(self.bot,self.callback('custom:stars'))
        await self.module.handle(self.bot,self.message('250'))
        invoice=next(kwargs for name,args,kwargs in self.bot.calls if name=='invoice')
        self.assertEqual(invoice['prices'][0]['amount'],250)
        await self.module.handle(self.bot,self.callback('usdtopup:500'))
        self.assertIn('Кнопка устарела',self.alert())

    async def test_long_terms_and_support_are_split_and_escaped(self):
        terms='Пункт <1> & условия\n'*400
        with patch.dict(os.environ,{'SHOP_TERMS':terms,'SUPPORT_CONTACT':'@help <team>'}):
            await self.module.handle(self.bot,self.callback('terms'))
            sent=[call for call in self.bot.calls if call[0] in ('message','edit')]
            self.assertGreater(len(sent),1)
            texts=[kwargs.get('text') or args[1] for name,args,kwargs in sent]
            self.assertTrue(all(len(t.encode('utf-16-le'))//2<=4096 for t in texts))
            self.assertIn('<1>',''.join(texts))
            self.assertIn('reply_markup',sent[-1][2])
            await self.module.handle(self.bot,self.message('/support'))
            self.assertIn('@help &lt;team&gt;',self.screen_text())

    async def test_cancel_command_clears_input_and_is_registered(self):
        from unittest.mock import AsyncMock
        self.store.set_language(42,'ru')
        self.store.input_mode(42,'stars')
        await self.module.handle(self.bot,self.message('/cancel'))
        self.assertEqual(self.store.input_mode(42),'')
        self.assertIn('Привет',self.screen_text())
        self.bot.set_my_commands=AsyncMock()
        await self.module.register_commands(self.bot)
        commands=[item['command'] for item in self.bot.set_my_commands.call_args_list[0].args[0]]
        self.assertIn('cancel',commands)

    async def test_channel_gate_is_not_bypassed_by_txid_mode(self):
        self.enable_channel();self.store.set_language(42,'ru')
        self.store.input_mode(42,'txid')
        await self.module.handle(self.bot,self.message('hello'))
        self.assertIn('Подпишись',self.screen_text())
        self.store.input_mode(42,'txid')
        await self.module.handle(self.bot,self.message('/start'))
        self.assertIn('Подпишись',self.screen_text())

    async def test_qtyinput_on_out_of_stock_product(self):
        pid=self.store.save_product(dict(category='K',name='Key',description='',price=10,mode='auto',instruction='',active=True))
        await self.module.handle(self.bot,self.callback('qtyinput:'+str(pid)+':1'))
        self.assertIn('Товар закончился',self.alert())
        self.assertEqual(self.store.input_mode(42),'')
        await self.module.handle(self.bot,self.callback('product:'+str(pid)))
        callbacks=[b['callback_data'] for b in self.buttons()]
        self.assertFalse(any(c.startswith(('qtyinput:','checkout:')) for c in callbacks))

    # ------------------------------------------------------------ manual orders

    async def test_manual_order_details_button_flow(self):
        self.store.set_language(42,'ru')
        pid=self.store.save_product(dict(category='S',name='Setup',description='',price=100,mode='manual',instruction='',active=True))
        self.store.adjust_wallet(42,100,'gift','gift-1','owner')
        await self.module.handle(self.bot,self.callback('wbuy:'+str(pid)))
        order_id=self.store.orders(42)[0]['id']
        await self.module.handle(self.bot,self.callback('wpay:'+order_id))
        await self.module.deliver(self.bot)
        buttons=[b for row in self.bot.calls[-1][2]['reply_markup']['inline_keyboard'] for b in row]
        self.assertEqual(buttons[0]['callback_data'],'details:'+order_id)
        await self.module.handle(self.bot,self.callback('orders'))
        active=[b for b in self.buttons() if b['callback_data']=='order:'+order_id]
        self.assertEqual(len(active),1);self.assertIn('Ждём твои данные',active[0]['text'])
        await self.module.handle(self.bot,self.callback('order:'+order_id))
        self.assertIn('details:'+order_id,[b['callback_data'] for b in self.buttons()])
        await self.module.handle(self.bot,self.callback('details:'+order_id))
        self.assertEqual(self.store.input_mode(42),'details:'+order_id)
        await self.module.handle(self.bot,self.message('x'*2001))
        self.assertIn('Максимум 2000',self.bot.calls[-1][1][1])
        await self.module.handle(self.bot,self.message('login: buyer@example.com'))
        self.assertIn('Данные приняты',self.bot.calls[-1][1][1])
        self.assertEqual(self.store.orders(42)[0]['status'],'queued')
        self.assertEqual(self.store.input_mode(42),'')
        await self.module.handle(self.bot,self.callback('details:'+order_id))
        self.assertIn('уже получены',self.alert())
        # /details keeps working for buyers who prefer the command.
        other=self.store.create_order(pid,42);self.store.adjust_wallet(42,100,'gift','gift-2','owner')
        self.store.pay_wallet(other['id'],42)
        await self.module.handle(self.bot,self.message('/details '+other['id']+' my data'))
        self.assertIn('Данные приняты',self.bot.calls[-1][1][1])
        await self.module.handle(self.bot,self.callback('order:'+'f'*32))
        self.assertIn('Заказ не найден',self.alert())

    async def test_profile_counts_only_paid_orders_and_wallet_history_has_labels(self):
        self.store.set_language(42,'ru')
        pid=self.store.save_product(dict(category='K',name='Key',description='',price=10,mode='auto',instruction='',active=True))
        self.store.import_stock(pid,'one')
        order=self.store.create_order(pid,42);self.store.paid(order['id'],42,10,'XTR','c-1')
        with self.store.tx() as db:db.execute("UPDATE orders SET status='needs_review' WHERE id=?",(order['id'],))
        await self.module.handle(self.bot,self.callback('profile'))
        self.assertIn('Оплаченных заказов: <b>0</b>',self.screen_text())
        self.assertNotIn('300',self.screen_text())
        topup=self.store.create_topup(42,50);self.store.credit_topup(topup,42,50,'XTR','c-2')
        await self.module.handle(self.bot,self.callback('wallet'))
        self.assertRegex(self.screen_text(),r'\d\d\.\d\d · .+: ＋ 50 Stars')
        self.assertIn('Пополнение Stars',self.module.history_line({'delta':50,'created':0,'reference':'topup:abc'}))
        self.assertIn('Пополнение USDT',self.module.history_line({'delta':50,'created':0,'reference':'ton:abc'}))
        line=self.module.history_line({'delta':-5,'created':0,'reference':'order:abc','kind':'order'})
        self.assertIn('Покупка',line)
        self.assertIn('Операция',self.module.history_line({'delta':5,'created':0}))

    async def test_brand_name_is_used_everywhere(self):
        with patch.dict(os.environ,{'SHOP_NAME':'Candy <Shop>'}):
            await self.module.handle(self.bot,self.message('/start'))
            self.assertIn('Candy &lt;Shop&gt;',self.screen_text())
            self.enable_channel();self.store.set_language(77,'ru')
            await self.module.handle(self.bot,self.callback('wallet',user_id=77))
            self.assertIn('Чтобы открыть Candy &lt;Shop&gt;',self.screen_text())
            await self.module.handle(self.bot,self.callback('language',user_id=77))
            self.assertIn('<b>Candy &lt;Shop&gt;</b>',self.screen_text())

    async def test_english_receipt_heading_is_bold(self):
        self.store.set_language(42,'en')
        heading='✅ Purchase completed successfully!'
        await self.module.send_result(self.bot,42,heading+'\n\nItem: X')
        self.assertEqual(self.bot.calls[-1][2]['entities'][0].length,len(heading.encode('utf-16-le'))//2)

    # ------------------------------------------------------------ 3.0: photos, support, notify; 3.2: promo

    def fake_module(self, name, **attributes):
        from types import SimpleNamespace
        module = SimpleNamespace(**attributes)
        patcher = patch.object(self.module, name, module)
        patcher.start();self.addCleanup(patcher.stop)
        return module

    def png(self, name='product-images/1-abcdef12.jpg'):
        from PIL import Image
        path = Path(self.store.path).resolve().parent/name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (20, 20), (10, 120, 200)).save(path, format='PNG')
        return name

    async def test_product_photo_badge_and_old_price_with_file_id_cache(self):
        from types import SimpleNamespace
        self.store.set_currency('100', 'test')
        pid = self.store.save_product(dict(category='AI', name='Pro <Plan>', description='Long description '*80, price=800,
                                           mode='manual', instruction='', active=True))
        product = dict(self.store.product(pid), image_path=self.png(), image_file_id='', badge='Хит', old_price=1000)
        cached = []
        self.store.cache_image_file_id = lambda product_id, file_id, path: cached.append((product_id, file_id, path))
        async def edit_media(**kwargs):
            self.bot.calls.append(('media', (), kwargs))
            return SimpleNamespace(photo=[SimpleNamespace(file_id='small'), SimpleNamespace(file_id='telegram-file')])
        self.bot.edit_message_media = edit_media
        nav = self.module.Navigation(self.bot, self.callback('product:1', photo=True).callback_query.message)
        await self.module.product_card(nav, 42, product)
        media = self.bot.calls[-1][2]['media']
        self.assertTrue(self.module.fits_caption(media.caption, 'HTML'))
        self.assertIn('<b>Pro &lt;Plan&gt;</b> · 🏷 Хит', media.caption)
        self.assertIn('<s>$10.00</s> <b>$8.00</b> · −20%', media.caption)
        self.assertIn('…', media.caption)
        self.assertEqual(cached, [(pid, 'telegram-file', 'product-images/1-abcdef12.jpg')])
        # A stale cached file_id falls back to uploading the file from the data directory.
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendPhoto
        stale = TelegramBadRequest(method=SendPhoto(chat_id=42, photo='x'), message='Bad Request: wrong file identifier/HTTP URL specified')
        sent = []
        async def send_photo(chat, photo, **kwargs):
            sent.append(photo)
            if photo == 'stale-id':raise stale
        self.bot.send_photo = send_photo
        await self.module.product_card(self.bot, 42, dict(product, image_file_id='stale-id', description='Short'))
        self.assertEqual(sent[0], 'stale-id')
        self.assertTrue(str(sent[1].path).endswith('1-abcdef12.jpg'))

    async def test_product_without_photo_or_with_unsafe_path_is_text(self):
        pid = self.store.save_product(dict(category='AI', name='Plan', description='', price=100, mode='manual',
                                           instruction='', active=True))
        (Path(self.store.path).resolve().parent/'shop-copy.db').write_bytes(b'not a photo')
        for image in ('', '../../etc/passwd', 'product-images/missing.jpg', 'shop-copy.db', 'product-images/../shop-copy.db'):
            self.bot.calls.clear()
            await self.module.product_card(self.bot, 42, dict(self.store.product(pid), image_path=image))
            self.assertEqual(self.bot.calls[-1][0], 'message')
            self.assertIn('💰 Цена: <b>100 Stars</b>', self.bot.calls[-1][1][1])

    async def test_search_is_gone(self):
        self.store.set_language(42, 'ru')
        pid = self.store.save_product(dict(category='AI', name='Plan', description='', price=100, mode='manual',
                                           instruction='', active=True))
        self.store.save_product(dict(category='Video', name='Other', description='', price=100, mode='manual',
                                     instruction='', active=True))
        for screen in ('home', 'catalog', 'cat:0', 'product:'+str(pid)):
            await self.module.handle(self.bot, self.callback(screen))
            self.assertNotIn('search', [b['callback_data'] for b in self.buttons()])
            self.assertNotIn('Поиск', self.screen_text())
        await self.module.handle(self.bot, self.callback('search'))
        self.assertIn('устарела', self.alert())
        self.assertFalse(hasattr(self.module, 'search_prompt'))

    async def test_buy_again_opens_checkout_with_previous_quantity(self):
        self.store.set_currency('100', 'test')
        pid = self.store.save_product(dict(category='Keys', name='Key', description='', price=300, mode='auto',
                                           instruction='', active=True))
        self.store.import_stock(pid, 'a\nb\nc\nd')
        order = self.store.create_order(pid, 42, 2)
        self.store.paid(order['id'], 42, 600, 'XTR', 'again-charge')
        await self.module.handle(self.bot, self.callback('purchase:'+order['id']+':0'))
        self.assertIn('again:'+order['id'], [b['callback_data'] for b in self.buttons()])
        await self.module.handle(self.bot, self.callback('again:'+order['id']))
        self.assertIn('Оформление заказа', self.screen_text())
        self.assertIn('$6.00', self.screen_text())
        self.assertIn('wbuy:'+str(pid)+':2', [b['callback_data'] for b in self.buttons()])
        await self.module.handle(self.bot, self.callback('again:'+'e'*32))
        self.assertIn('Покупка не найдена', self.alert())

    def promo_code(self, code, amount, **extra):
        return self.module.promo.Promos(self.store).create(dict(code=code, amount_cents=amount, **extra), 'owner')

    def message_update(self, text, update_id):
        update = self.message(text)
        return update.model_copy(update={'update_id': update_id})

    async def test_promo_from_wallet_credits_balance(self):
        self.store.set_currency('100', 'test')
        self.promo_code('GIFT5', 500)
        await self.module.handle(self.bot, self.callback('home'))
        self.assertIn('promo', [b['callback_data'] for b in self.buttons()])
        await self.module.handle(self.bot, self.callback('wallet'))
        self.assertIn('promo', [b['callback_data'] for b in self.buttons()])
        await self.module.handle(self.bot, self.callback('promo'))
        self.assertEqual(self.store.input_mode(42), 'promo')
        self.assertIn('зачислится на баланс', self.screen_text())
        await self.module.handle(self.bot, self.message_update('wrong!', 101))
        self.assertIn('от 3 до 32', self.bot.calls[-1][1][1])
        self.assertEqual(self.module.promo.Promos(self.store).failures(42), 0)  # chat text is not an attempt
        await self.module.handle(self.bot, self.message_update('nope', 102))
        self.assertIn('❌ Промокод не найден', self.bot.calls[-1][1][1])
        self.assertEqual(self.store.input_mode(42), 'promo')
        await self.module.handle(self.bot, self.message_update('gift5', 103))
        text = self.bot.calls[-1][1][1]
        self.assertIn('✅ Промокод активирован! На баланс зачислено $5.00. Баланс: $5.00', text)
        self.assertEqual({b['callback_data'] for b in self.buttons()} >= {'catalog', 'wallet'}, True)
        self.assertEqual(self.store.input_mode(42), '')
        # A repeated delivery of the same request: no second credit, same answer.
        await self.module.handle(self.bot, self.message_update('/promo gift5', 103))
        self.assertIn('Баланс: $5.00', self.bot.calls[-1][1][1])
        self.assertEqual(self.store.wallet(42)['balance'], 500)
        await self.module.handle(self.bot, self.message_update('/promo gift5', 104))
        self.assertIn('Вы уже использовали этот промокод', self.bot.calls[-1][1][1])
        await self.module.handle(self.bot, self.callback('wallet'))
        self.assertIn('Промокод: ＋ $5.00', self.screen_text())

    async def test_promo_command_errors_lockout_and_english(self):
        import time
        from i18n import language
        self.promo_code('LATE', 100, starts_at=time.time()+3600)
        self.store.set_language(42, 'en')
        await self.module.handle(self.bot, self.message_update('/promo', 201))
        self.assertEqual(self.store.input_mode(42), 'promo')
        await self.module.handle(self.bot, self.message_update('/promo late', 202))
        self.assertIn('This promo code is not active yet', self.bot.calls[-1][1][1])
        for index in range(self.module.promo.FAIL_LIMIT):
            await self.module.handle(self.bot, self.message_update('/promo GUESS%d' % index, 210+index))
        self.assertIn('Too many attempts, please try again later', self.bot.calls[-1][1][1])
        self.assertEqual(self.store.input_mode(42), '')
        self.store.set_language(42, 'ru')
        await self.module.handle(self.bot, self.message_update('/promo LATE', 230))
        self.assertIn('Слишком много попыток, попробуй позже', self.bot.calls[-1][1][1])
        self.assertEqual(language.get(), 'ru')

    async def test_checkout_has_no_promo(self):
        self.store.set_currency('100', 'test')
        pid = self.store.save_product(dict(category='S', name='Item', description='', price=500, mode='manual',
                                           instruction='', active=True))
        await self.module.handle(self.bot, self.callback('checkout:'+str(pid)+':2'))
        self.assertIn('Итого: $10.00', self.screen_text())
        self.assertFalse(any(b['callback_data'].startswith(('promo', 'unpromo')) for b in self.buttons()))
        await self.module.handle(self.bot, self.callback('promo:'+str(pid)+':2'))
        self.assertIn('устарела', self.alert())
        await self.module.handle(self.bot, self.callback('wbuy:'+str(pid)+':2'))
        self.assertIn('Оплата с баланса', self.screen_text())
        self.assertNotIn('промокод', self.screen_text().lower())

    async def test_support_chat_text_photo_and_staff_reply(self):
        desk = self.module.support.Support(self.store)
        self.store.set_language(42, 'ru')
        with patch.dict(os.environ, {'SUPPORT_CONTACT': '@help'}):
            await self.module.handle(self.bot, self.callback('support'))
        self.assertIn('write', [b['callback_data'] for b in self.buttons()])
        await self.module.handle(self.bot, self.callback('write'))
        self.assertEqual(self.store.input_mode(42), 'support')
        await self.module.handle(self.bot, self.message('Не пришёл код'))
        self.assertIn('Сообщение отправлено', self.bot.calls[-1][1][1])
        from aiogram.types import Update
        photo = Update.model_validate({'update_id': 12, 'message': {'message_id': 3, 'date': 0, 'chat': {'id': 42, 'type': 'private'},
            'from': {'id': 42, 'is_bot': False, 'first_name': 'Buyer'}, 'caption': 'Скриншот',
            'photo': [{'file_id': 'small', 'file_unique_id': 's', 'width': 1, 'height': 1},
                      {'file_id': 'big', 'file_unique_id': 'b', 'width': 9, 'height': 9}]}})
        await self.module.handle(self.bot, photo)
        with patch.object(self.module.support, 'RATE_LIMIT', 2):
            await self.module.handle(self.bot, self.message('flood'))
        self.assertIn('Слишком много', self.bot.calls[-1][1][1])
        ticket = desk.open_ticket(42)
        messages = desk.thread(ticket)['messages']
        self.assertEqual([(m['text'], m['kind'], m['file_id']) for m in messages],
                         [('Не пришёл код', 'text', None), ('Скриншот', 'photo', 'big')])
        await self.module.handle(self.bot, self.message('/cancel'))
        self.assertEqual(self.store.input_mode(42), '')
        self.store.set_language(42, 'en')
        desk.reply(ticket, 'Код <отправлен>', 'owner')
        other = desk.open_or_append(43, 'x')
        desk.reply(other['ticket_id'], 'y', 'owner')
        from unittest.mock import AsyncMock
        sends = AsyncMock(side_effect=[None, self.forbidden()])
        self.bot.send_message = sends
        with self.assertLogs('themarket', level='WARNING'):
            await self.module.deliver_support_replies(self.bot)
        args, kwargs = sends.await_args_list[0]
        self.assertEqual(args[0], 42);self.assertIn('Support reply:', args[1]);self.assertIn('Код <отправлен>', args[1])
        self.assertIsNone(kwargs['parse_mode'])
        self.assertEqual(kwargs['reply_markup']['inline_keyboard'][0][0]['callback_data'], 'write')
        self.assertEqual(sends.await_args_list[1][0][0], 43)
        self.assertEqual(desk.outbox_due(), [])

    async def test_long_support_reply_is_one_message_and_never_repeated(self):
        desk = self.module.support.Support(self.store)
        ticket = desk.open_or_append(42, 'help')['ticket_id']
        with self.assertRaisesRegex(ValueError, 'слишком длинное'):
            desk.reply(ticket, '😀'*1800, 'owner')  # 3600 UTF-16 units
        desk.reply(ticket, 'я'*3500, 'owner')
        from unittest.mock import AsyncMock
        sends = AsyncMock(side_effect=[self.module.TelegramRetryAfter(method=None, message='flood', retry_after=1), None])
        self.bot.send_message = sends
        await self.module.deliver_support_replies(self.bot)
        with self.store.tx() as db:db.execute('UPDATE support_outbox SET next_try=0')
        await self.module.deliver_support_replies(self.bot)
        self.assertEqual(sends.await_count, 2)  # the same single message, retried once
        for call in sends.await_args_list:
            self.assertLessEqual(self.module.utf16_len(call.args[1]), 4096)
            self.assertTrue(call.args[1].endswith('я'*3500))
        self.assertEqual(desk.outbox_due(), [])

    async def test_service_worker_sends_staff_notifications_and_ticks(self):
        import asyncio
        queue = self.module.notify.Notify(self.store)
        queue.save({'admin_chat_ids': '777', 'daily_summary': False}, 'owner')
        queue.notify('new_paid_order', '🛒 Новый заказ <b>')
        self.module.ADMIN_ALERTS['paused_until'] = 0
        sent, ticks = [], []
        async def send_message(chat, text, **kwargs):sent.append((chat, text, kwargs.get('parse_mode')))
        self.bot.send_message = send_message
        original_tick = self.module.notify.Notify.tick
        def tick(notify_self, now=None):
            ticks.append(now)
            return original_tick(notify_self, now)
        sleeps = []
        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 1:  # a warning logged meanwhile reaches the admin chat on the next pass
                with self.store.tx() as db:self.store.log(db, 'bot', 'bot.offline', 'telegram', level='error')
            if len(sleeps) >= 2:raise asyncio.CancelledError
        with patch.object(self.module.notify.Notify, 'tick', new=tick), patch.object(self.module.asyncio, 'sleep', new=sleep),\
             patch.object(self.store, 'housekeeping', new=lambda: None), self.assertRaises(asyncio.CancelledError):
            await self.module.service_worker(self.bot)
        self.assertEqual(sent[0], (777, '🛒 Новый заказ <b>', None))
        self.assertEqual(len(sent), 2);self.assertIn('потерял связь', sent[1][1])
        self.assertEqual(queue.pending_count(), 0)
        self.assertEqual(len(ticks), 1)

    async def test_updates_are_interactive_for_the_rate_limiter(self):
        import ratelimit
        seen = []
        async def fake_handle(bot, u):seen.append(ratelimit.priority.get())
        with patch.object(self.module, 'handle', new=fake_handle):
            await self.module.Dispatcher(self.bot).process([self.callback('home')])
        self.assertEqual(seen, [ratelimit.INTERACTIVE])
        self.assertEqual(ratelimit.priority.get(), ratelimit.SERVICE)

    async def test_returning_visitor_is_not_rewritten_on_every_update(self):
        writes = []
        original = self.store.remember_customer
        def remember(*args):writes.append(args);return original(*args)
        self.store.remember_customer = remember
        for _ in range(3):
            await self.module.handle(self.bot, self.callback('wallet'))
        self.assertEqual(len(writes), 1)
        update = self.callback('wallet')
        user = update.callback_query.from_user.model_copy(update={'first_name': 'Renamed'})
        await self.module.handle(self.bot, update.model_copy(update={'callback_query': update.callback_query.model_copy(update={'from_user': user})}))
        self.assertEqual(len(writes), 2)

    async def test_menu_shows_balance_and_active_orders(self):
        self.store.set_language(42, 'ru')
        self.store.adjust_wallet(42, 250, 'gift', 'menu-gift', 'owner')
        pid = self.store.save_product(dict(category='S', name='Setup', description='', price=100, mode='manual',
                                           instruction='', active=True))
        order = self.store.create_order(pid, 42)
        self.store.pay_wallet(order['id'], 42)
        await self.module.handle(self.bot, self.message('/start'))
        self.assertIn('Баланс: <b>150 Stars</b>', self.screen_text())
        self.assertIn('Активных заказов: <b>1</b>', self.screen_text())

    async def test_qr_is_rendered_once_per_wallet(self):
        calls = []
        def qr(text):calls.append(text);return b'\x89PNG'
        self.module._qr_cache.clear()
        with patch.object(self.module, 'crypto_qr', new=qr):
            self.assertEqual(await self.module.qr_png('wallet-1'), b'\x89PNG')
            await self.module.qr_png('wallet-1')
        self.assertEqual(calls, ['wallet-1'])

    # ------------------------------------------------------------ 3.1: customers banned in the panel

    def ban_notices(self):
        return [c for c in self.bot.calls if c[0]=='message' and 'Доступ к магазину ограничен' in str(c[1])]

    async def test_banned_user_is_stopped_with_one_notice_per_minute(self):
        self.module._ban_notices.clear();self.store.set_language(42,'ru')
        pid=self.store.save_product(dict(category='Keys',name='Key',description='',price=10,mode='auto',instruction='',active=True))
        self.store.import_stock(pid,'k1\nk2')
        await self.module.handle(self.bot,self.callback('buy:'+str(pid)))  # creates an invoice before the ban
        invoice=next(kwargs for name,args,kwargs in self.bot.calls if name=='invoice')
        self.store.ban_customer(42,'Спам','owner')
        self.bot.calls.clear()
        await self.module.handle(self.bot,self.message('/start'))
        await self.module.handle(self.bot,self.callback('catalog'))
        await self.module.handle(self.bot,self.message('привет'))
        self.assertEqual(len(self.ban_notices()),1)
        self.assertEqual(len(self.bot.calls),1)  # nothing else was rendered
        with patch.object(self.module.time,'monotonic',return_value=self.module.time.monotonic()+61):
            await self.module.handle(self.bot,self.message('/start'))
        self.assertEqual(len(self.ban_notices()),2)
        # A payment already in flight is still accepted and delivered.
        from unittest.mock import AsyncMock
        self.bot.answer_pre_checkout_query=AsyncMock()
        update=Update.model_validate({'update_id':13,'pre_checkout_query':{'id':'q','from':{'id':42,'is_bot':False,'first_name':'B'},
            'currency':'XTR','total_amount':10,'invoice_payload':invoice['payload']}})
        await self.module.handle(self.bot,update)
        self.assertEqual(self.bot.answer_pre_checkout_query.await_args.kwargs,{'ok':True})
        payment={'invoice_payload':invoice['payload'],'currency':'XTR','total_amount':10,
                 'telegram_payment_charge_id':'banned-charge','provider_payment_charge_id':''}
        await self.module.handle(self.bot,self.message(payment=payment))
        self.assertEqual(self.store.orders()[0]['status'],'fulfilled')

    async def test_banned_user_can_still_reach_support(self):
        self.module._ban_notices.clear();self.store.set_language(42,'ru')
        self.store.remember_customer(42,'','Buyer','')
        self.store.ban_customer(42,'Спам','owner')
        await self.module.handle(self.bot,self.message('/support'))
        self.assertIn('Поддержка',self.screen_text())
        await self.module.handle(self.bot,self.callback('write'))
        await self.module.handle(self.bot,self.message('Это ошибка, разблокируйте'))
        import support
        tickets=support.Support(self.store).tickets()['items']
        self.assertEqual([t['user_id'] for t in tickets],[42])
        self.assertEqual(self.ban_notices(),[])

    async def test_unbanned_and_other_users_are_not_affected(self):
        self.module._ban_notices.clear();self.store.set_language(42,'ru')
        self.store.remember_customer(42,'','Buyer','')
        self.store.ban_customer(42,'Спам','owner')
        self.store.unban_customer(42,'owner')
        await self.module.handle(self.bot,self.message('/start'))
        self.assertEqual(self.ban_notices(),[])
        with patch.object(self.store,'is_banned',side_effect=RuntimeError('db')),self.assertLogs('themarket',level='WARNING'):
            await self.module.handle(self.bot,self.message('/start'))  # a failing check never locks buyers out
        self.assertEqual(self.ban_notices(),[])
        self.assertIn('Buyer',self.screen_text())

    # ------------------------------------------------------------ translations

    def test_every_tr_literal_is_translated_with_matching_placeholders(self):
        import ast,json,re,string
        en=json.loads(Path('translations_en.json').read_text(encoding='utf-8'))
        literals=set()
        for name in ('bot.py','store.py'):
            for node in ast.walk(ast.parse(Path(name).read_text(encoding='utf-8'))):
                if isinstance(node,ast.Call) and getattr(node.func,'id',None)=='tr' and node.args \
                        and isinstance(node.args[0],ast.Constant):
                    literals.add(node.args[0].value)
        literals.add(self.module.RECEIPT)
        literals.update(description for _,description in self.module.COMMANDS)
        def fields(text):return sorted(f for _,f,_,_ in string.Formatter().parse(text) if f is not None)
        for literal in literals:
            with self.subTest(literal=literal[:60]):
                self.assertIn(literal,en)
                self.assertEqual(fields(literal),fields(en[literal]))
                self.assertEqual(re.findall(r'</?[a-z]+',literal),re.findall(r'</?[a-z]+',en[literal]))

    def test_customer_facing_errors_have_english_text(self):
        import ast,json
        en=json.loads(Path('translations_en.json').read_text(encoding='utf-8'))
        customer_methods={'create_crypto_deposit','crypto_deposit','crypto_context','credit_crypto','queue_crypto_check',
            'create_order','approve','paid','purchase_delivery','create_topup','topup_info','approve_topup',
            'credit_topup','pay_wallet','details','purchase'}
        messages=set()
        def collect(tree):
            for node in ast.walk(tree):
                if isinstance(node,ast.Call) and getattr(node.func,'id',None) in ('ValueError','PendingPayment','PaymentMismatch') \
                        and node.args and isinstance(node.args[0],ast.Constant):
                    messages.add(node.args[0].value)
        for node in ast.walk(ast.parse(Path('store.py').read_text(encoding='utf-8'))):
            if isinstance(node,ast.FunctionDef) and node.name in customer_methods:collect(node)
        for name in ('bep20.py','crypto_networks.py','money.py'):
            tree=ast.parse(Path(name).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and ('verify' in node.name or node.name in
                        ('tx_hash','normalize_hash','request','usd_cents')):collect(node)
        for message in messages:
            with self.subTest(message=message):self.assertIn(message,en)

    def test_tr_accepts_lang_and_legacy_language_keyword(self):
        from i18n import tr
        self.assertEqual(tr('Язык',lang='en'),'Language')
        self.assertEqual(tr('Язык',language='en'),'Language')
        self.assertEqual(tr('Язык',language=''),'Язык')


if __name__=='__main__':unittest.main(verbosity=2)
