import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from aiogram.exceptions import (TelegramBadRequest,TelegramForbiddenError,TelegramNetworkError,TelegramNotFound,
                                TelegramRetryAfter,TelegramServerError)
from aiogram.methods import SendMessage
from cryptography.fernet import Fernet

from backups import inspect_database
from broadcasts import Broadcasts,delivery_entities,render_delivery,send_one
from store import Store


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.key=Fernet.generate_key()
        self.store=Store(Path(self.temp.name)/'shop.db',self.key)
        self.news=Broadcasts(self.store)

    def tearDown(self):self.temp.cleanup()

    def user(self,uid=42,language='ru'):
        self.store.remember_customer(uid,'buyer','Buyer')
        self.store.set_language(uid,language)

    def product(self,mode='auto',active=True,emoji_id=''):
        return self.store.save_product(dict(category='Subscriptions',name='Example item',description='',price=100,mode=mode,instruction='',active=active,emoji_id=emoji_id))

    def custom(self,**changes):
        data=dict(text='Новости магазина <b>как текст</b>',text_en='Store news',button_text='',target='catalog',url='',product_id=None,with_banner=False)
        data.update(changes)
        return self.news.draft(str(uuid.uuid4()),'Новости недели',data,'owner')['id']

    def ready(self):
        with self.store.tx() as db:
            db.execute('UPDATE broadcasts SET ready_at=0')
            db.execute('UPDATE news_preferences SET next_send=0')

    def test_new_auto_product_waits_for_first_stock_and_batches_uploads(self):
        self.user();pid=self.product()
        self.assertEqual(self.news.catalog()['total'],0)
        self.store.import_stock(pid,'SECRET-1\nSECRET-2')
        self.store.import_stock(pid,'SECRET-2\nSECRET-3')
        self.store.import_stock(pid,'SECRET-3')
        result=self.news.catalog();self.assertEqual(result['total'],1)
        detail=self.news.detail(result['items'][0]['id'])
        self.assertEqual(detail['kind'],'new_product');self.assertEqual(detail['content']['added'],3)
        self.assertEqual(detail['content']['available'],3)
        self.assertNotIn('SECRET',json.dumps(detail))
        self.assertIsNone(self.news.claim())
        self.ready();item=self.news.claim();self.news.result(item,'sent',11)
        self.store.import_stock(pid,'SECRET-4')
        self.assertEqual(self.news.catalog()['items'][0]['kind'],'restock')

    def test_hidden_product_announced_only_when_published(self):
        self.user();pid=self.product(active=False);self.store.import_stock(pid,'secret')
        self.assertEqual(self.news.catalog()['total'],0)
        p=self.store.products()[0];p['active']=True;self.store.save_product(p,product_id=pid)
        self.assertEqual(self.news.catalog()['total'],1)
        self.store.save_product(p,product_id=pid)
        self.assertEqual(self.news.catalog()['total'],1)

    def test_switches_disable_future_auto_jobs(self):
        self.user();self.news.save_settings(False,False,'owner')
        pid=self.product('manual');self.assertEqual(self.news.catalog()['total'],0)
        self.news.save_settings(True,True,'owner')
        self.store.save_product(self.store.products()[0],product_id=pid)
        self.assertEqual(self.news.catalog()['total'],0)
        pid=self.product();self.store.import_stock(pid,'secret')
        self.assertEqual(self.news.catalog()['total'],1)

    def test_old_products_migrate_without_retroactive_news(self):
        pid=self.product('manual')
        with self.store.tx() as db:db.execute('ALTER TABLE products DROP COLUMN news_announced')
        reopened=Store(self.store.path,self.key);self.user()
        reopened.save_product(reopened.products()[0],product_id=pid)
        self.assertEqual(Broadcasts(reopened).catalog()['total'],0)

    def test_audience_unique_optout_blocked_and_frozen_at_start(self):
        self.user();self.store.adjust_wallet(42,100,'test','test-ref','owner')
        self.user(43);self.user(44);self.news.preferences(43,False)
        with self.store.tx() as db:db.execute('INSERT INTO news_preferences(user_id,blocked) VALUES(44,1)')
        campaign=self.custom();self.assertEqual(self.news.catalog()['audience'],1)
        self.news.action(campaign,'start','owner');self.user(45)
        self.news.action(campaign,'start','owner')
        self.assertEqual(self.news.detail(campaign)['counts']['total'],1)
        self.assertEqual(self.news.claim()['user_id'],42)

    def test_empty_audience_completes_without_sending(self):
        campaign=self.custom();self.news.action(campaign,'start','owner')
        self.assertEqual(self.news.detail(campaign)['status'],'completed')
        self.assertIsNone(self.news.claim())

    def test_draft_idempotency_and_encrypted_content(self):
        reference=str(uuid.uuid4());data=dict(text='Unique confidential draft',target='none')
        first=self.news.draft(reference,'Campaign',data,'owner')
        self.assertEqual(first,self.news.draft(reference,'Campaign',data,'owner'))
        with self.assertRaises(ValueError):self.news.draft(reference,'Campaign',dict(text='Changed',target='none'),'owner')
        with self.store.tx() as db:
            content=db.execute('SELECT content FROM broadcasts').fetchone()[0]
            audit=' '.join(row[0] for row in db.execute('SELECT details FROM audit'))
        self.assertNotIn(b'Unique confidential',content);self.assertNotIn('Unique confidential',audit)
        self.assertEqual(self.news.detail(first['id'])['status'],'draft')

    def test_validation_telegram_utf16_limits_and_safe_buttons(self):
        for data in [dict(text='😀'*513,target='catalog',with_banner=True),dict(text='😀'*1751,target='catalog'),
                     dict(text='x',target='url',url='javascript:alert(1)'),dict(text='x',target='url',url='https://name:secret@example.com'),
                     dict(text='x',target='product',product_id=99),dict(text='  ',target='none')]:
            with self.subTest(data=str(data)[:80]),self.assertRaises(ValueError):self.custom(**data)
        self.custom(text='😀'*512,with_banner=True,target='url',url='https://example.com/catalog')
        self.assertEqual(self.news.catalog()['total'],1)

    def test_editing_draft_preserves_id_and_running_campaign_is_immutable(self):
        self.user();campaign=self.custom()
        self.news.draft(campaign,'Исправлено',dict(text='Новый текст',target='none'),'owner',replace=True)
        self.assertEqual(self.news.detail(campaign)['content']['text'],'Новый текст')
        self.assertEqual(self.news.catalog()['total'],1)
        self.news.action(campaign,'start','owner')
        with self.assertRaises(ValueError):self.news.draft(campaign,'Ещё правка',dict(text='Текст',target='none'),'owner',replace=True)
        self.assertEqual(self.news.detail(campaign)['content']['text'],'Новый текст')

    def test_pause_resume_cancel_keeps_sent_results(self):
        self.user();self.user(43);campaign=self.custom();self.news.action(campaign,'start','owner')
        item=self.news.claim();self.news.action(campaign,'pause','owner');self.assertIsNone(self.news.claim())
        self.news.result(item,'sent',99)
        self.news.action(campaign,'resume','owner');self.news.action(campaign,'cancel','owner')
        detail=self.news.detail(campaign)
        self.assertEqual(detail['counts']['sent'],1);self.assertEqual(detail['counts']['skipped'],1)
        self.assertEqual(detail['status'],'cancelled');self.assertIsNone(self.news.claim())

    def test_optout_after_start_skips_queued_news(self):
        self.user();campaign=self.custom();self.news.action(campaign,'start','owner');self.news.preferences(42,False)
        self.assertIsNone(self.news.claim());self.assertEqual(self.news.detail(campaign)['counts']['skipped'],1)
        self.store.remember_customer(42)
        self.assertFalse(self.news.preferences(42))

    def test_concurrent_claim_is_atomic(self):
        for uid in range(42,50):self.user(uid)
        campaign=self.custom();self.news.action(campaign,'start','owner')
        with ThreadPoolExecutor(max_workers=4) as pool:items=list(pool.map(lambda _:self.news.claim(),range(8)))
        self.assertEqual(len({item['user_id'] for item in items}),8)

    def test_expired_lease_unknown_not_retried_and_stale_result_ignored(self):
        self.user();campaign=self.custom();self.news.action(campaign,'start','owner');item=self.news.claim()
        with self.store.tx() as db:db.execute('UPDATE broadcast_deliveries SET lease_until=0')
        self.assertIsNone(self.news.claim());self.news.result(item,'sent',123)
        counts=self.news.detail(campaign)['counts'];self.assertEqual(counts['uncertain'],1);self.assertEqual(counts['sent'],0)

    def test_per_user_cooldown_between_campaigns(self):
        self.user()
        for _ in range(2):self.news.action(self.custom(),'start','owner')
        item=self.news.claim();self.assertIsNone(self.news.claim());self.news.result(item,'sent',1)
        self.assertIsNone(self.news.claim());self.ready();self.assertIsNotNone(self.news.claim())

    def test_deleted_or_sold_out_product_cancels_automatic_job(self):
        self.user();pid=self.product();self.store.import_stock(pid,'secret');self.ready()
        self.store.delete_product(pid);self.assertIsNone(self.news.claim())
        self.assertEqual(self.news.catalog()['items'][0]['status'],'cancelled')

    def test_ru_en_previews_and_product_button(self):
        self.user(language='en');pid=self.product('manual');self.ready();item=self.news.claim()
        text,keyboard,banner=render_delivery(item)
        self.assertIn('New item',text);self.assertEqual(keyboard['inline_keyboard'][0][0]['callback_data'],'product:'+str(pid))
        self.assertFalse(banner)
        campaign=self.custom(text_en='');detail=self.news.detail(campaign)
        self.assertEqual(detail['previews']['ru']['text'],detail['previews']['en']['text'])

    def test_automatic_and_custom_messages_have_only_primary_button(self):
        self.user();self.product('manual')
        auto=self.news.detail(self.news.catalog()['items'][0]['id'])
        custom=self.news.detail(self.custom())
        for detail in (auto,custom):
            for preview in detail['previews'].values():
                self.assertEqual(len(preview['keyboard']['inline_keyboard']),1)
                self.assertFalse(any(b.get('callback_data')=='news:off' for row in preview['keyboard']['inline_keyboard'] for b in row))
        no_button=self.news.detail(self.custom(target='none'))
        self.assertEqual(no_button['previews']['ru']['keyboard']['inline_keyboard'],[])

    async def test_custom_emoji_survives_queue_restart_and_uses_utf16_entities(self):
        self.user(language='en');emoji_id='5463427579966929312';fallback='👩‍💻'
        self.store.save_emoji_pack('Brands','Brands',[dict(id=emoji_id,emoji=fallback,file_id='test-file')],'owner')
        pid=self.product(emoji_id=emoji_id);self.store.import_stock(pid,'secret-1')
        campaign=self.news.catalog()['items'][0]['id']
        self.store.import_stock(pid,'secret-2')
        product=self.store.products()[0];product['emoji_id']='5384362450432827393'
        self.store.save_product(product,product_id=pid)
        reopened=Store(self.store.path,self.key);service=Broadcasts(reopened)
        detail=service.detail(campaign)
        self.assertEqual(detail['content']['emoji_id'],emoji_id)
        for language,preview in detail['previews'].items():
            entity=preview['entities'][0]
            selected=preview['text'].encode('utf-16-le')[entity['offset']*2:(entity['offset']+entity['length'])*2].decode('utf-16-le')
            self.assertEqual(selected,fallback,language)
            self.assertEqual(entity['custom_emoji_id'],emoji_id)
            self.assertEqual(preview['keyboard']['inline_keyboard'][0][0]['icon_custom_emoji_id'],emoji_id)
        self.ready();calls=[]
        class Bot:
            async def send_message(_,uid,text,**kwargs):calls.append((text,kwargs));return SimpleNamespace(message_id=17)
        await send_one(Bot(),service)
        self.assertEqual(calls[0][1]['entities'],detail['previews']['en']['entities'])
        self.assertIn(fallback+' Example item',calls[0][0]);self.assertEqual(service.detail(campaign)['counts']['sent'],1)

    def test_older_pending_campaign_picks_up_product_emoji(self):
        self.user();emoji_id='5384362450432827393';self.product('manual',emoji_id=emoji_id)
        campaign=self.news.catalog()['items'][0]['id']
        with self.store.tx() as db:
            content=json.loads(self.store.decrypt(db.execute('SELECT content FROM broadcasts WHERE id=?',(campaign,)).fetchone()[0]))
            content.pop('emoji_id');content.pop('emoji')
            db.execute('UPDATE broadcasts SET content=? WHERE id=?',(self.store.encrypt(json.dumps(content)),campaign))
        detail=self.news.detail(campaign)
        self.assertEqual(detail['previews']['ru']['entities'][0]['custom_emoji_id'],emoji_id)
        self.ready();item=self.news.claim();text,_,_=render_delivery(item)
        self.assertEqual(delivery_entities(item,text)[0]['custom_emoji_id'],emoji_id)

    async def test_custom_message_without_button_has_no_keyboard(self):
        self.user();campaign=self.custom(target='none');self.news.action(campaign,'start','owner');calls=[]
        class Bot:
            async def send_message(_,uid,text,**kwargs):calls.append(kwargs);return SimpleNamespace(message_id=1)
        await send_one(Bot(),self.news)
        self.assertIsNone(calls[0]['reply_markup']);self.assertNotIn('entities',calls[0])

    async def test_sender_plain_text_no_paid_broadcast_and_english(self):
        self.user(language='en');campaign=self.custom();self.news.action(campaign,'start','owner')
        calls=[]
        class Bot:
            async def send_message(_,uid,text,**kwargs):calls.append((uid,text,kwargs));return SimpleNamespace(message_id=10)
        self.assertTrue(await send_one(Bot(),self.news));self.assertIn('Store news',calls[0][1])
        self.assertFalse(calls[0][2]['allow_paid_broadcast']);self.assertIsNone(calls[0][2]['parse_mode'])
        self.assertEqual(self.news.detail(campaign)['counts']['sent'],1)

    async def test_sender_retry_after_block_and_ambiguous_failure(self):
        method=SendMessage(chat_id=42,text='Test')
        for error,status in [(TelegramRetryAfter(method=method,message='Retry',retry_after=5),'pending'),
            (TelegramForbiddenError(method=method,message='blocked'),'blocked'),
            (TelegramBadRequest(method=method,message='bad chat'),'failed'),(TimeoutError(),'uncertain')]:
            with self.subTest(status=status):
                with self.store.tx() as db:
                    db.execute('DELETE FROM broadcast_deliveries');db.execute('DELETE FROM broadcasts');db.execute('DELETE FROM news_preferences');db.execute("DELETE FROM meta WHERE key='news.cooldown'")
                self.user();campaign=self.custom();self.news.action(campaign,'start','owner')
                class Bot:
                    async def send_message(*args,**kwargs):raise error
                await send_one(Bot(),self.news)
                self.assertEqual(self.news.detail(campaign)['counts'][status],1)
                if status=='pending':self.assertIsNone(self.news.claim())
                if status=='blocked':
                    self.assertEqual(self.news.catalog()['audience'],0)
                    self.store.remember_customer(42);self.assertEqual(self.news.catalog()['audience'],1)

    async def test_banner_uses_single_photo_with_caption(self):
        self.user();campaign=self.custom(with_banner=True);self.news.action(campaign,'start','owner');calls=[]
        class Bot:
            async def send_photo(_,uid,photo,**kwargs):calls.append((photo,kwargs));return SimpleNamespace(message_id=1)
        await send_one(Bot(),self.news)
        self.assertEqual(len(calls),1);self.assertIn('Новости магазина',calls[0][1]['caption'])

    def test_backup_checks_campaign_encryption(self):
        self.custom()
        report=inspect_database(Path(self.store.path),self.key)
        self.assertEqual(report['tables']['broadcasts'],1)
        self.assertEqual(report['encrypted_values'],1)
        with self.store.tx() as db:db.execute("UPDATE broadcasts SET content=X'0000'")
        with self.assertRaises(ValueError):inspect_database(Path(self.store.path),self.key)


class DeliveryReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import broadcasts
        broadcasts._banner_cache.clear()
        self.temp=tempfile.TemporaryDirectory()
        self.key=Fernet.generate_key()
        self.store=Store(Path(self.temp.name)/'shop.db',self.key)
        self.news=Broadcasts(self.store)
        self.method=SendMessage(chat_id=42,text='Test')

    def tearDown(self):self.temp.cleanup()

    def start(self,users=(42,),**changes):
        for uid in users:self.store.remember_customer(uid,'buyer','Buyer');self.store.set_language(uid,'ru')
        data=dict(text='Новости',text_en='News',button_text='',target='catalog',url='',product_id=None,with_banner=False)
        data.update(changes)
        campaign=self.news.draft(str(uuid.uuid4()),'Новости',data,'owner')['id']
        self.news.action(campaign,'start','owner')
        return campaign

    def row(self,uid=42):
        with self.store.tx() as db:return dict(db.execute('SELECT * FROM broadcast_deliveries WHERE user_id=?',(uid,)).fetchone())

    def cooldown(self):
        with self.store.tx() as db:
            row=db.execute("SELECT value FROM meta WHERE key='news.cooldown'").fetchone()
        return float(row[0]) if row else 0

    def clear_cooldown(self):
        with self.store.tx() as db:
            db.execute("DELETE FROM meta WHERE key='news.cooldown'");db.execute('UPDATE news_preferences SET next_send=0')

    def failing(self,error):
        calls=[]
        class Bot:
            async def send_message(_,uid,text,**kwargs):calls.append(uid);raise error
        return Bot(),calls

    async def test_connection_refused_returns_to_pending_without_using_attempt_and_pauses_queue(self):
        from broadcasts import deliver
        campaign=self.start((42,43))
        for cause in (ConnectionRefusedError(),__import__('socket').gaierror('dns')):
            with self.subTest(cause=type(cause).__name__):
                error=TelegramNetworkError(method=self.method,message='ClientConnectorError');error.__cause__=cause
                bot,_=self.failing(error);item=self.news.claim()
                self.assertEqual(await deliver(bot,self.news,item),'outage')
                row=self.row(item['user_id'])
                self.assertEqual((row['status'],row['attempts']),('pending',0))
                self.assertGreater(self.cooldown(),time.time()+1)
                self.assertIsNone(self.news.claim())
                self.clear_cooldown()
        self.assertEqual(self.news.detail(campaign)['counts']['uncertain'],0)

    async def test_outage_does_not_burn_campaign_and_backoff_grows(self):
        from broadcasts import deliver
        campaign=self.start(range(42,52))
        bot,calls=self.failing(TelegramServerError(method=self.method,message='Bad Gateway'))
        delays=[]
        for _ in range(3):
            item=self.news.claim();self.assertIsNotNone(item)
            self.assertEqual(await deliver(bot,self.news,item),'outage')
            delays.append(round(self.cooldown()-time.time()))
            self.assertIsNone(self.news.claim())
            self.clear_cooldown()
        self.assertEqual(delays,[5,10,20])
        counts=self.news.detail(campaign)['counts']
        self.assertEqual(counts['pending'],10);self.assertEqual(counts['uncertain']+counts['failed'],0)
        self.news.healthy();self.assertEqual(self.news.backoff(),5)

    async def test_server_errors_give_up_after_attempt_cap(self):
        from broadcasts import MAX_ATTEMPTS,deliver
        campaign=self.start()
        bot,_=self.failing(TelegramServerError(method=self.method,message='Internal'))
        for _ in range(MAX_ATTEMPTS):
            self.clear_cooldown();item=self.news.claim();await deliver(bot,self.news,item)
        row=self.row();self.assertEqual((row['status'],row['error']),('failed','attempts'))
        self.assertEqual(self.news.detail(campaign)['status'],'completed')

    async def test_timeout_after_send_is_uncertain_and_never_retried(self):
        from broadcasts import deliver
        campaign=self.start()
        error=TelegramNetworkError(method=self.method,message='Request timeout error');error.__cause__=TimeoutError()
        bot,_=self.failing(error)
        self.assertEqual(await deliver(bot,self.news,self.news.claim()),'outage')
        self.clear_cooldown()
        self.assertEqual(self.news.detail(campaign)['counts']['uncertain'],1);self.assertIsNone(self.news.claim())

    async def test_chat_not_found_marks_recipient_unreachable(self):
        from broadcasts import deliver
        for error in (TelegramNotFound(method=self.method,message='Not Found'),TelegramBadRequest(method=self.method,message='Bad Request: chat not found')):
            with self.subTest(error=type(error).__name__):
                with self.store.tx() as db:
                    for table in ('broadcast_deliveries','broadcasts','news_preferences'):db.execute('DELETE FROM '+table)
                campaign=self.start();bot,_=self.failing(error)
                self.assertEqual(await deliver(bot,self.news,self.news.claim()),'chat_not_found')
                self.assertEqual(self.news.detail(campaign)['counts']['blocked'],1)
                self.assertEqual(self.cooldown(),0)

    async def test_banner_uploaded_once_then_reused_and_reuploaded_when_rejected(self):
        import broadcasts
        self.start((42,43,44),with_banner=True);photos=[];reject={'value':False}
        class Bot:
            async def send_photo(_,uid,photo,**kwargs):
                photos.append(photo)
                if isinstance(photo,str) and reject['value']:
                    reject['value']=False
                    raise TelegramBadRequest(method=self.method,message='Bad Request: wrong file identifier/HTTP URL specified')
                return SimpleNamespace(message_id=uid,photo=[SimpleNamespace(file_id='small'),SimpleNamespace(file_id='big-'+str(len(photos)))])
        self.assertTrue(await send_one(Bot(),self.news))
        self.assertIsInstance(photos[0],broadcasts.FSInputFile)
        self.clear_cooldown()
        broadcasts._banner_cache.clear()  # persisted in meta: survives a restart
        await send_one(Bot(),self.news);self.assertEqual(photos[1],'big-1')
        self.clear_cooldown();reject['value']=True
        await send_one(Bot(),self.news)
        self.assertEqual(photos[2],'big-1');self.assertIsInstance(photos[3],broadcasts.FSInputFile)
        self.assertEqual(self.news.banner_file_id(),'big-4')
        self.assertEqual(self.news.detail(self.news.catalog()['items'][0]['id'])['counts']['sent'],3)

    def test_batch_claim_and_release(self):
        campaign=self.start(range(42,57))
        items=self.news.claim_batch(10)
        self.assertEqual(len({item['user_id'] for item in items}),10)
        self.assertEqual(self.news.detail(campaign)['counts']['sending'],10)
        self.news.release(items[3:])
        counts=self.news.detail(campaign)['counts']
        self.assertEqual((counts['sending'],counts['pending']),(3,12))
        self.assertEqual(self.row(items[5]['user_id'])['attempts'],0)
        self.assertEqual(len(self.news.claim_batch(20)),12)

    def test_old_finished_campaign_rows_are_archived(self):
        campaign=self.start((42,43))
        for item in self.news.claim_batch(5):self.news.result(item,'sent',1)
        self.assertEqual(self.news.detail(campaign)['status'],'completed')
        with self.store.tx() as db:db.execute('UPDATE broadcasts SET finished=?',(time.time()-91*86400,))
        self.news.prune()
        with self.store.tx() as db:self.assertEqual(db.execute('SELECT count(*) FROM broadcast_deliveries').fetchone()[0],0)
        self.assertEqual(self.news.detail(campaign)['counts']['sent'],2)
        self.assertEqual(self.news.catalog()['items'][0]['counts']['total'],2)

    def test_token_bucket_limits_rate(self):
        from broadcasts import TokenBucket,configured_rate
        now=[0.0];bucket=TokenBucket(10,clock=lambda:now[0])
        delays=[bucket.delay() for _ in range(15)]
        self.assertEqual(delays[:10],[0.0]*10);self.assertAlmostEqual(delays[10],0.1)
        self.assertAlmostEqual(sum(delays),0.1+0.2+0.3+0.4+0.5)
        with patch.dict('os.environ',{'BROADCAST_RATE':'100'}):self.assertEqual(configured_rate(),20)
        with patch.dict('os.environ',{'BROADCAST_RATE':'junk'}):self.assertEqual(configured_rate(),10)

    async def test_worker_stops_on_outage_and_releases_rest_of_batch(self):
        from broadcasts import worker
        campaign=self.start(range(42,50))
        error=TelegramNetworkError(method=self.method,message='ClientConnectorError');error.__cause__=ConnectionRefusedError()
        bot,calls=self.failing(error)
        task=asyncio.create_task(worker(bot,self.store,rate=20,idle=0.01))
        await asyncio.sleep(0.3);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(calls),1)
        counts=self.news.detail(campaign)['counts']
        self.assertEqual((counts['pending'],counts['sending']),(8,0))


class SegmentScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'shop.db',Fernet.generate_key())
        self.news=Broadcasts(self.store)
        self.pid=self.store.save_product(dict(category='Keys',name='Key',description='',price=100,mode='manual',instruction='',active=True))
        self.other=self.store.save_product(dict(category='Keys',name='Other',description='',price=100,mode='manual',instruction='',active=True))
        # 1: bought Key (ru) · 2: bought Other (en) · 3: unpaid order only (ru) · 4: inactive for 60 days (en)
        for uid,language in ((1,'ru'),(2,'en'),(3,'ru'),(4,'en')):
            self.store.remember_customer(uid,'u'+str(uid),'U');self.store.set_language(uid,language)
        for uid,pid in ((1,self.pid),(2,self.other)):
            order=self.store.create_order(pid,uid);self.store.adjust_wallet(uid,100,'t','w'+str(uid),'owner');self.store.pay_wallet(order['id'],uid)
        self.store.create_order(self.pid,3)
        with self.store.tx() as db:db.execute('UPDATE telegram_users SET last_seen=? WHERE user_id=4',(time.time()-60*86400,))

    def tearDown(self):self.temp.cleanup()

    def draft(self,**extra):
        data=dict(text='Новости',text_en='News',button_text='',target='catalog',url='',product_id=None,with_banner=False,**extra)
        return self.news.draft(str(uuid.uuid4()),'Сегмент',data,'owner')['id']

    def recipients(self,campaign):
        with self.store.tx() as db:
            return sorted(r[0] for r in db.execute('SELECT user_id FROM broadcast_deliveries WHERE campaign_id=?',(campaign,)))

    def test_segment_preview_counts(self):
        cases=[(None,4),({'type':'all'},4),({'type':'buyers'},2),({'type':'non_buyers'},2),
               ({'type':'language','language':'en'},2),({'type':'language','language':'ru'},2),
               ({'type':'product','product_id':self.pid},1),({'type':'inactive','days':30},1),({'type':'inactive','days':90},0)]
        for segment,count in cases:
            with self.subTest(segment=segment):
                preview=self.news.preview(segment)
                self.assertEqual((preview['count'],preview['total']),(count,4))
        self.news.preferences(1,False)
        self.assertEqual(self.news.preview({'type':'buyers'})['count'],1)
        for bad in ({'type':'vip'},{'type':'language','language':'de'},{'type':'product'},{'type':'inactive','days':0},'buyers'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.news.preview(bad)

    def test_segment_frozen_at_start_and_shown_in_catalog(self):
        campaign=self.draft(segment={'type':'buyers'})
        detail=self.news.detail(campaign)
        self.assertEqual((detail['segment'],detail['audience']),({'type':'buyers'},2))
        self.news.action(campaign,'start','owner')
        self.assertEqual(self.recipients(campaign),[1,2])
        self.assertEqual(self.news.catalog()['items'][0]['segment'],{'type':'buyers'})
        en=self.draft(segment={'type':'language','language':'en'});self.news.action(en,'start','owner')
        self.assertEqual(self.recipients(en),[2,4])
        product=self.draft(segment={'type':'product','product_id':self.other});self.news.action(product,'start','owner')
        self.assertEqual(self.recipients(product),[2])
        with self.assertRaisesRegex(ValueError,'Товар для аудитории'):self.draft(segment={'type':'product','product_id':999})
        legacy=self.draft()
        self.assertEqual(self.news.detail(legacy)['segment'],{'type':'all'})

    def test_draft_idempotency_includes_segment(self):
        reference=str(uuid.uuid4())
        data=dict(text='Новости',text_en='',button_text='',target='none',url='',product_id=None,with_banner=False,segment={'type':'buyers'})
        self.news.draft(reference,'T',data,'owner');self.news.draft(reference,'T',data,'owner')
        with self.assertRaisesRegex(ValueError,'уже использован'):self.news.draft(reference,'T',{**data,'segment':{'type':'all'}},'owner')
        self.news.draft(reference,'T',{**data,'segment':{'type':'non_buyers'}},'owner',replace=True)
        self.assertEqual(self.news.detail(reference)['segment'],{'type':'non_buyers'})

    def test_scheduled_start_selects_audience_when_due(self):
        when=time.time()+3600
        campaign=self.draft(scheduled_at=when,segment={'type':'non_buyers'})
        self.assertEqual(self.news.detail(campaign)['scheduled_at'],when)
        self.news.action(campaign,'start','owner')
        self.assertEqual(self.news.detail(campaign)['status'],'scheduled')
        self.assertIsNone(self.news.claim());self.assertEqual(self.recipients(campaign),[])
        self.store.remember_customer(5,'late','Late')   # joins before the scheduled time
        with self.store.tx() as db:db.execute('UPDATE broadcasts SET ready_at=? WHERE id=?',(time.time()-1,campaign))
        item=self.news.claim()
        self.assertEqual(item['campaign_id'],campaign)
        self.assertEqual(self.recipients(campaign),[3,4,5])
        self.assertEqual(self.news.detail(campaign)['status'],'running')
        later=self.draft(scheduled_at=time.time()+7200);self.news.action(later,'start','owner')
        self.news.action(later,'cancel','owner');self.assertEqual(self.news.detail(later)['status'],'cancelled')
        with self.assertRaisesRegex(ValueError,'90 дней'):self.draft(scheduled_at=time.time()+91*86400)
        with self.assertRaises(ValueError):self.draft(scheduled_at='tomorrow')
        past=self.draft(scheduled_at=time.time()-10);self.news.action(past,'start','owner')
        self.assertEqual(self.news.detail(past)['status'],'queued')

    async def test_worker_uses_global_limiter_when_available(self):
        import broadcasts
        campaign=self.draft();self.news.action(campaign,'start','owner')
        acquired=[];sent=[]
        class Limiter:
            @staticmethod
            async def acquire(chat_id,bulk=False):acquired.append((chat_id,bulk))
        class Bot:
            async def send_message(_,uid,text,**kwargs):sent.append(uid);return SimpleNamespace(message_id=len(sent))
        with patch.object(broadcasts,'ratelimit',Limiter):
            task=asyncio.create_task(broadcasts.worker(Bot(),self.store,rate=20,idle=0.01))
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(sent)==4:break
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(sorted(sent),[1,2,3,4])
        self.assertEqual(sorted(acquired),[(1,True),(2,True),(3,True),(4,True)])
