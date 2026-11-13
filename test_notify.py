from datetime import datetime, timedelta
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import notify
from notify import MSK, Notify
from support import Support
from test_promo import open_store


def msk(year, month, day, hour, minute=0):
    return datetime(year,month,day,hour,minute,tzinfo=MSK).timestamp()


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = open_store(self.temp.name)
        self.notify = Notify(self.store)
        self.notify.save({'admin_chat_ids':[111,222]},'owner')

    def tearDown(self):self.temp.cleanup()

    def emit(self, kind, text, **kwargs):
        with self.store.tx() as db:return self.notify.emit(db,kind,text,**kwargs)

    def test_initialize_is_idempotent(self):
        with self.store.tx() as db:
            notify.initialize(db);notify.initialize(db)

    def test_settings_defaults_from_env_and_validation(self):
        with self.store.tx() as db:db.execute("DELETE FROM meta WHERE key='notify.settings'")
        with patch.dict(os.environ,{'ADMIN_CHAT_ID':'123456, -100777 ,123456'}):
            settings = self.notify.settings()
        self.assertEqual(settings['admin_chat_ids'],[123456,-100777])
        self.assertTrue(settings['daily_summary']);self.assertEqual(settings['daily_summary_time'],'09:00')
        self.assertEqual(settings['low_stock_threshold'],3)
        saved = self.notify.save({'admin_chat_ids':'5, 6','errors':False,'low_stock_overrides':{'7':10},
                                  'daily_summary_time':'21:30'},'owner')
        self.assertEqual(saved['admin_chat_ids'],[5,6]);self.assertFalse(self.notify.settings()['errors'])
        self.assertEqual(self.notify.settings()['low_stock_overrides'],{'7':10})
        for bad in ({'admin_chat_ids':['abc']},{'admin_chat_ids':[0]},{'admin_chat_ids':list(range(1,30))},
                    {'daily_summary_time':'25:00'},{'daily_summary_time':'9:00'},{'low_stock_threshold':-1},
                    {'low_stock_threshold':'3'},{'low_stock_overrides':{'x':1}},{'admin_chat_ids':[True]}):
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.notify.save(bad,'owner')
        # An explicit empty list is kept (env default does not come back).
        self.notify.save({'admin_chat_ids':[]},'owner')
        with patch.dict(os.environ,{'ADMIN_CHAT_ID':'1'}):self.assertEqual(self.notify.settings()['admin_chat_ids'],[])
        with self.assertRaisesRegex(ValueError,'хотя бы один чат'):self.notify.send_test('owner')

    def test_queue_encrypted_per_chat_and_delivery_results(self):
        self.assertEqual(self.emit('new_paid_order','Оплачен заказ abc123 · 100 Stars'),2)
        due = self.notify.due()
        self.assertEqual([(item['chat_id'],item['kind']) for item in due],[(111,'new_paid_order'),(222,'new_paid_order')])
        self.assertEqual(due[0]['text'],'Оплачен заказ abc123 · 100 Stars')
        with self.store.read() as db:
            raw = b''.join(row[0] for row in db.execute('SELECT body FROM notify_queue'))
        self.assertNotIn(b'abc123',raw)
        self.assertEqual(self.notify.result(due[0]['id'],True),'sent')
        self.assertIsNone(self.notify.result(due[0]['id'],False))
        self.assertEqual(self.notify.result(due[1]['id'],False),'retry')
        self.assertEqual(self.notify.due(),[])
        self.assertEqual(self.notify.pending_count(),1)
        with self.store.tx() as db:db.execute('UPDATE notify_queue SET next_try=0')
        self.assertEqual(self.notify.result(due[1]['id'],False,permanent=True),'failed')
        self.assertEqual(self.notify.pending_count(),0)
        with self.assertRaises(ValueError):self.emit('unknown','x')
        self.assertEqual(self.notify.send_test('owner'),2)

    def test_toggles_dedupe_and_rate_limit(self):
        self.notify.save({'admin_chat_ids':[111],'payment_issue':False},'owner')
        self.assertEqual(self.emit('payment_issue','x'),0)
        self.assertEqual(self.emit('low_stock','Мало: A',key='product:1'),1)
        self.assertEqual(self.emit('low_stock','Мало: A',key='product:1'),0)
        self.assertEqual(self.emit('low_stock','Мало: B',key='product:2'),1)
        with self.store.tx() as db:db.execute("UPDATE notify_dedupe SET last=last-6*3600-1 WHERE key='low_stock:product:1'")
        self.assertEqual(self.emit('low_stock','Мало: A',key='product:1'),1)
        self.assertEqual(sum(self.emit('errors','Ошибка '+str(i),key=str(i)) for i in range(30)),notify.HOURLY_LIMITS['errors'])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT dropped FROM notify_rate WHERE kind='errors'").fetchone()[0],10)
        with self.store.tx() as db:db.execute("UPDATE notify_rate SET window_start=window_start-3601 WHERE kind='errors'")
        self.assertEqual(self.emit('errors','Снова',key='new'),1)
        self.assertEqual(self.emit('new_paid_order','x'*5000),1)
        self.assertEqual(len(self.notify.due(limit=100)[-1]['text']),notify.MAX_TEXT)

    def test_emit_rolls_back_with_producer_transaction(self):
        with self.assertRaises(RuntimeError):
            with self.store.tx() as db:
                self.notify.emit(db,'new_paid_order','paid')
                raise RuntimeError('payment failed')
        self.assertEqual(self.notify.due(),[])

    def test_low_stock_after_sale(self):
        pid = self.store.save_product(dict(category='Keys',name='Steam key',description='',price=100,mode='auto',instruction='',active=True))
        self.store.import_stock(pid,'\n'.join('KEY-'+str(i) for i in range(5)))
        with self.store.tx() as db:self.assertFalse(self.notify.check_low_stock(db,pid))
        for user in (1,2):
            order = self.store.create_order(pid,user)
            self.store.adjust_wallet(user,100,'test','w'+str(user),'owner');self.store.pay_wallet(order['id'],user)
        # The store raises the alert itself after the sale; a second check is deduplicated (once per 6 h).
        self.assertTrue(any(item['kind']=='low_stock' for item in self.notify.due()))
        with self.store.tx() as db:self.assertFalse(self.notify.check_low_stock(db,pid))
        low=[item['text'] for item in self.notify.due() if item['kind']=='low_stock']
        self.assertIn('Steam key',low[0]);self.assertIn('Осталось: 3',low[0])
        manual = self.store.save_product(dict(category='Keys',name='Manual',description='',price=100,mode='manual',instruction='',active=True))
        with self.store.tx() as db:self.assertFalse(self.notify.check_low_stock(db,manual))
        other = self.store.save_product(dict(category='Keys',name='Other',description='',price=100,mode='auto',instruction='',active=True))
        self.store.import_stock(other,'\n'.join('O-'+str(i) for i in range(8)))
        self.notify.save({'admin_chat_ids':[111],'low_stock_overrides':{str(other):10}},'owner')
        with self.store.tx() as db:self.assertTrue(self.notify.check_low_stock(db,other))
        self.notify.save({'admin_chat_ids':[111],'low_stock':False},'owner')
        with self.store.tx() as db:db.execute('DELETE FROM notify_dedupe')
        with self.store.tx() as db:self.assertFalse(self.notify.check_low_stock(db,pid))

    def test_daily_summary_once_per_moscow_day(self):
        day = datetime.now(MSK).date()-timedelta(days=3)
        self.notify.save({'admin_chat_ids':[111],'daily_summary_time':'09:00'},'owner')
        early = msk(day.year,day.month,day.day,8,59)
        self.assertFalse(self.notify.tick(early)['summary'])
        nine = msk(day.year,day.month,day.day,9,0)
        self.assertTrue(self.notify.tick(nine)['summary'])
        self.assertFalse(self.notify.tick(nine+60)['summary'])
        self.assertFalse(Notify(self.store).tick(nine+3600*14)['summary'])   # another process, same day
        self.assertEqual(len([item for item in self.notify.due(limit=100) if item['kind']=='daily_summary']),1)
        following = nine+86400
        self.assertTrue(self.notify.tick(following)['summary'])
        self.notify.save({'admin_chat_ids':[111],'daily_summary':False},'owner')
        self.assertFalse(self.notify.tick(following+86400)['summary'])

    def test_summary_numbers(self):
        day = datetime.now(MSK).date()
        now = msk(day.year,day.month,day.day,10,30)
        pid = self.store.save_product(dict(category='Keys',name='Low item',description='',price=250,mode='auto',instruction='',active=True))
        self.store.import_stock(pid,'A\nB\nC\nD')
        orders = []
        for user in (5,6,7):
            order = self.store.create_order(pid,user)
            self.store.adjust_wallet(user,250,'test','w'+str(user),'owner');self.store.pay_wallet(order['id'],user)
            orders.append(order['id'])
        yesterday = now-86400
        with self.store.tx() as db:
            db.execute('UPDATE orders SET paid_at=? WHERE id=?',(yesterday,orders[0]))
            db.execute('UPDATE orders SET paid_at=? WHERE id IN (?,?)',(now-3600,orders[1],orders[2]))
            db.execute('DELETE FROM telegram_users')
            db.execute('INSERT INTO telegram_users(user_id,first_seen,last_seen) VALUES(5,?,?),(6,?,?),(7,?,?)',
                       (yesterday,yesterday,now-60,now-60,now-86400*10,now-86400*10))
        Support(self.store).open_or_append(6,'Помогите')
        summary = self.notify.summary(now)
        self.assertEqual(summary['yesterday'],{'revenue_cents':250,'orders':1,'new_customers':1})
        self.assertEqual(summary['today'],{'revenue_cents':500,'orders':2,'new_customers':1})
        self.assertEqual(summary['open_tickets'],1)
        self.assertEqual(summary['low_stock_total'],1);self.assertEqual(summary['low_stock'][0]['free'],1)
        self.assertIn('Low item — 1 шт.',summary['text']);self.assertIn('выручка 500 Stars',summary['text'])
        self.store.set_currency('100','owner')
        self.assertIn('выручка $5.00',self.notify.summary(now)['text'])

    def test_prune_old_rows(self):
        self.emit('new_paid_order','old')
        item = self.notify.due()[0];self.notify.result(item['id'],True)
        with self.store.tx() as db:db.execute('UPDATE notify_queue SET created=created-8*86400 WHERE sent=1')
        self.assertEqual(self.notify.tick(time.time())['pruned'],1)
        self.assertEqual(self.notify.due()[0]['chat_id'],222)
        self.assertEqual(self.notify.tick(time.time())['pruned'],0)   # throttled hourly


if __name__=='__main__':unittest.main()
