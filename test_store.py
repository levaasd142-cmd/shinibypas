import concurrent.futures
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from contextlib import closing, contextmanager
from cryptography.fernet import Fernet
import os
import subprocess
import sys
import types
from store import AUDIT_LABELS, SCHEMA_VERSION, Store, RESERVATION_SECONDS, audit_retention_days


class ShopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key()
        self.store = Store(Path(self.temp.name)/'test.db',self.key)
        self.pid = self.product('auto')

    def tearDown(self):
        self.temp.cleanup()

    def product(self,mode):
        return self.store.save_product(dict(category='Test',name='License',description='',price=100,mode=mode,instruction='Activate',active=True))

    def stock(self):
        self.store.import_stock(self.pid,'secret-1\nsecret-2')

    def pay(self,o,charge='charge-1'):
        self.store.approve(o['id'],o['user_id'],o['price'],'XTR')
        self.store.paid(o['id'],o['user_id'],o['price'],'XTR',charge)

    def test_duplicate_import_and_encryption(self):
        self.stock()
        r=self.store.import_stock(self.pid,'secret-1\nsecret-3\nsecret-3')
        self.assertEqual(r,{'added':1,'duplicates':1})
        with self.store.tx() as db:
            self.assertNotIn(b'secret-1',db.execute('SELECT secret FROM stock LIMIT 1').fetchone()[0])

    def test_last_unit_is_reserved_once(self):
        self.store.import_stock(self.pid,'only-unit')
        def buy(uid):
            try:return self.store.create_order(self.pid,uid)
            except ValueError:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            orders=list(pool.map(buy,range(1,9)))
        self.assertEqual(sum(o is not None for o in orders),1)

    def test_current_stock_lists_free_and_reserved_without_revealing_secrets(self):
        self.store.import_stock(self.pid,'sold-secret\nreserved-secret\nfree-secret')
        sold=self.store.create_order(self.pid,5);self.pay(sold)
        reserved=self.store.create_order(self.pid,6)
        current=self.store.stock_items(self.pid)
        self.assertEqual((current['free'],current['reserved'],current['total']),(1,1,2))
        self.assertNotIn('reserved-secret',str(current))
        self.assertNotIn('secret',current['items'][0])
        held=self.store.stock_items(self.pid,'reserved')['items'][0]
        self.assertEqual(held['order_id'],reserved['id'])
        self.assertEqual(self.store.reveal_stock(self.pid,held['id'],'owner')['content'],'reserved-secret')
        other=self.product('auto')
        with self.assertRaises(ValueError):self.store.reveal_stock(other,held['id'],'owner')
        with self.store.tx() as db:
            sold_id=db.execute('SELECT id FROM stock WHERE order_id=?',(sold['id'],)).fetchone()[0]
            db.execute('UPDATE orders SET expires=0 WHERE id=?',(reserved['id'],))
        with self.assertRaises(ValueError):self.store.reveal_stock(self.pid,sold_id,'owner')
        self.assertEqual(self.store.stock_items(self.pid)['free'],2)
        self.assertNotIn('reserved-secret',str(self.store.audits()))

    def test_delete_product_preserves_orders_and_reserved_delivery(self):
        self.stock()
        paid=self.store.create_order(self.pid,5)
        self.pay(paid)
        reserved=self.store.create_order(self.pid,6)
        self.store.delete_product(self.pid,'owner')
        self.store.delete_product(self.pid,'owner')
        self.assertEqual(self.store.products(),[])
        self.assertEqual(self.store.products(public=True),[])
        with self.assertRaises(ValueError):self.store.create_order(self.pid,7)
        with self.assertRaises(ValueError):self.store.import_stock(self.pid,'another')
        with self.assertRaises(ValueError):self.store.save_product(dict(category='Test',name='Restore',
            description='',price=100,mode='auto',instruction='',active=True),product_id=self.pid)
        self.store.paid(reserved['id'],6,100,'XTR','reserved-charge')
        self.assertEqual(self.store.purchases(5)['total'],1)
        self.assertEqual(self.store.purchases(6)['total'],1)
        self.assertEqual(len(self.store.orders()),2)
        self.assertIn('secret-2',self.store.decrypt(next(d for d in self.store.deliveries() if d['user_id']==6)['body']))
        self.store.resend(paid['id'],'owner')
        self.assertTrue(any(a['action']=='product.delete' for a in self.store.audits()))

    def test_quantity_totals_stock_and_idempotent_delivery(self):
        self.store.set_currency('100','test')
        self.store.import_stock(self.pid,'first\nsecond\nthird')
        order=self.store.create_order(self.pid,5,2)
        self.assertEqual((order['price'],order['payment_stars'],order['quantity']),(200,200,2))
        self.assertEqual(self.store.products()[0]['available'],1)
        self.pay(order)
        self.store.paid(order['id'],5,200,'XTR','charge-1')
        deliveries=self.store.deliveries()
        self.assertEqual(len(deliveries),1)
        body=self.store.decrypt(deliveries[0]['body'])
        self.assertIn('1. first\n\n2. second',body)
        self.assertNotIn('third',body)
        with self.store.tx() as db:
            result=db.execute('SELECT result FROM orders WHERE id=?',(order['id'],)).fetchone()[0]
        self.assertNotIn(b'first',result)
        self.assertEqual(self.store.orders()[0]['quantity'],2)

    def test_purchases_filter_before_pagination_and_protect_ownership(self):
        self.store.import_stock(self.pid,'\n'.join('secret-'+str(i) for i in range(10)))
        successful=[]
        for i in range(6):
            order=self.store.create_order(self.pid,5)
            self.pay(order,'charge-'+str(i))
            successful.append(order['id'])
        hidden=self.store.create_order(self.pid,5)
        with self.store.tx() as db:
            db.execute('UPDATE orders SET created=created+1000 WHERE id=?',(hidden['id'],))
        first=self.store.purchases(5)
        second=self.store.purchases(5,1)
        self.assertEqual((first['total'],first['pages'],len(first['items'])),(6,2,5))
        self.assertEqual(len(second['items']),1)
        self.assertEqual({o['id'] for o in first['items']+second['items']},set(successful))
        self.assertNotIn('result',first['items'][0])
        self.assertEqual(self.store.purchases(5,999)['page'],1)
        self.assertEqual(self.store.purchases(5,-2)['page'],0)
        for status in ('pending','expired','awaiting_details','queued','processing','needs_review'):
            with self.store.tx() as db:
                db.execute('UPDATE orders SET status=?,charge=? WHERE id=?',(status,'hidden-charge',hidden['id']))
            self.assertEqual(self.store.purchases(5)['total'],6)
            with self.assertRaises(ValueError):self.store.purchase(5,hidden['id'])
        self.assertEqual(self.store.purchases(99)['total'],0)
        with self.assertRaises(ValueError):self.store.purchase(99,successful[0])

    def test_quantity_validation_and_atomic_stock_shortage(self):
        self.stock()
        for quantity in (0,-1,101,True,1.5,'2'):
            with self.assertRaises(ValueError):self.store.create_order(self.pid,5,quantity)
        with self.assertRaises(ValueError):self.store.create_order(self.pid,5,3)
        self.assertEqual(self.store.orders(),[])
        self.assertEqual(self.store.products()[0]['available'],2)

    def test_competing_bulk_orders_do_not_share_stock(self):
        self.store.import_stock(self.pid,'first\nsecond\nthird')
        def buy(uid):
            try:return self.store.create_order(self.pid,uid,2)
            except ValueError:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            orders=list(pool.map(buy,[5,6]))
        self.assertEqual(sum(o is not None for o in orders),1)
        self.assertEqual(self.store.products()[0]['available'],1)

    def test_bulk_expiry_releases_every_unit(self):
        self.stock();order=self.store.create_order(self.pid,5,2)
        with self.store.tx() as db:db.execute('UPDATE orders SET expires=0 WHERE id=?',(order['id'],))
        self.assertEqual(self.store.products()[0]['available'],2)
        self.assertEqual(self.store.orders()[0]['status'],'expired')

    def test_bulk_wallet_payment_debits_total_once(self):
        self.stock()
        self.store.adjust_wallet(5,500,'test balance','fund','test')
        order=self.store.create_order(self.pid,5,2)
        self.store.pay_wallet(order['id'],5)
        self.store.pay_wallet(order['id'],5)
        self.assertEqual(self.store.wallet(5)['balance'],300)
        self.assertEqual(self.store.orders()[0]['quantity'],2)

    def test_legacy_stock_schema_migrates_without_losing_reservations(self):
        self.stock();order=self.store.create_order(self.pid,5)
        with self.store.tx() as db:
            db.execute('''CREATE TABLE legacy_stock(id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id),fingerprint TEXT NOT NULL,
                secret BLOB NOT NULL,order_id TEXT UNIQUE REFERENCES orders(id),UNIQUE(product_id,fingerprint))''')
            db.execute('INSERT INTO legacy_stock SELECT id,product_id,fingerprint,secret,order_id FROM stock')
            db.execute('DROP TABLE stock')
            db.execute('ALTER TABLE legacy_stock RENAME TO stock')
            db.execute('ALTER TABLE orders DROP COLUMN quantity')
        reopened=Store(self.store.path,self.key)
        self.assertEqual(reopened.orders()[0]['quantity'],1)
        self.assertEqual(reopened.products()[0]['available'],1)
        self.pay(order)
        self.assertIn('secret-1',self.store.decrypt(self.store.deliveries()[0]['body']))
        reopened.import_stock(self.pid,'secret-3')
        bulk=reopened.create_order(self.pid,6,2)
        self.assertEqual(bulk['quantity'],2)

    def test_replayed_payment_has_one_delivery(self):
        self.stock();o=self.store.create_order(self.pid,5)
        self.pay(o);self.store.paid(o['id'],5,100,'XTR','charge-1')
        self.assertEqual(len(self.store.deliveries()),1)
        self.assertEqual(self.store.orders()[0]['status'],'fulfilled')

    def test_delivery_uses_saved_language_and_preserves_item_data(self):
        self.store.set_language(5,'en')
        self.stock();o=self.store.create_order(self.pid,5)
        self.pay(o)
        body=self.store.decrypt(self.store.deliveries()[0]['body'])
        self.assertTrue(body.startswith('✅ Purchase completed successfully!'))
        self.assertIn('Item: License (x1)',body)
        self.assertNotIn(o['id'][:8],body)
        self.assertIn('secret-',body)
        self.assertIn('Activate',body)
        self.store.resend(o['id'],'admin')
        self.assertTrue(self.store.decrypt(self.store.deliveries()[0]['body']).startswith('✅ Purchase completed successfully!'))

    def test_delivery_receipt_has_product_quantity_total_and_preserves_secrets(self):
        self.store.set_currency('100','test')
        pid=self.store.save_product(dict(category='AI',name='ChatGPT GO CARD (Full Warranty)',
            description='',price=460,mode='auto',instruction='Use your key',active=True))
        self.store.import_stock(pid,'<key>&password\nsecond-key')
        order=self.store.create_order(pid,5,2)
        self.store.paid(order['id'],5,920,'XTR','receipt-charge')
        body=self.store.decrypt(self.store.deliveries()[0]['body'])
        self.assertTrue(body.startswith('✅ Покупка успешно завершена!\n\n'
            'Товар: ChatGPT GO CARD (Full Warranty) (x2)\nСписано: $9.20'))
        self.assertIn('<key>&password',body)
        self.assertTrue(body.endswith('Use your key'))
        self.assertNotIn(order['id'][:8],body)
        self.assertEqual(self.store.purchase_delivery(5,order['id']),body)
        with self.assertRaises(ValueError):self.store.purchase_delivery(6,order['id'])

    def test_manual_notifications_use_customer_language_from_admin(self):
        self.store.set_language(5,'en')
        pid=self.product('manual');o=self.store.create_order(pid,5)
        self.pay(o)
        body=self.store.decrypt(self.store.deliveries()[0]['body'])
        self.assertIn('is paid.',body);self.assertIn('/details '+o['id'],body)
        self.store.details(o['id'],5,'buyer@example.com')
        self.store.claim(o['id'],'admin')
        self.store.complete(o['id'],'Данные товара','admin')
        body=self.store.decrypt(self.store.deliveries()[0]['body'])
        self.assertIn('completed',body)
        self.assertIn('Данные товара',body)

    def test_payment_mismatch_never_fulfills(self):
        self.stock();o=self.store.create_order(self.pid,5)
        for uid,amount,currency in [(6,100,'XTR'),(5,99,'XTR'),(5,100,'USD')]:
            with self.assertRaises(ValueError):self.store.paid(o['id'],uid,amount,currency,'bad')
        self.assertEqual(self.store.orders()[0]['status'],'pending')
        self.assertEqual(self.store.deliveries(),[])

    def test_expired_reservation_released(self):
        self.stock();o=self.store.create_order(self.pid,5)
        with self.store.tx() as db:db.execute('UPDATE orders SET expires=0 WHERE id=?',(o['id'],))
        self.store.deliveries()
        self.assertEqual(self.store.products()[0]['available'],2)
        with self.assertRaises(ValueError):self.store.approve(o['id'],5,100,'XTR')

    def test_approved_checkout_keeps_reservation_for_an_hour(self):
        self.stock();o=self.store.create_order(self.pid,5)
        self.store.approve(o['id'],5,100,'XTR')
        later=time.time()+30*60
        with patch('store.time.time',return_value=later):
            self.store.housekeeping()
            self.assertEqual(self.store.products()[0]['available'],1)
            self.store.paid(o['id'],5,100,'XTR','late-charge')
        self.assertEqual(self.store.orders()[0]['status'],'fulfilled')

    def test_approved_checkout_expires_after_an_hour(self):
        self.stock();o=self.store.create_order(self.pid,5)
        self.store.approve(o['id'],5,100,'XTR')
        with patch('store.time.time',return_value=time.time()+3700):
            self.assertEqual(self.store.products()[0]['available'],2)
            self.store.create_order(self.pid,5);self.store.create_order(self.pid,5,1)
            self.store.paid(o['id'],5,100,'XTR','very-late-charge')
        self.assertEqual(next(x for x in self.store.orders() if x['id']==o['id'])['status'],'needs_review')

    def test_late_unapproved_payment_requires_review(self):
        self.stock();o=self.store.create_order(self.pid,5)
        with self.store.tx() as db:db.execute('UPDATE orders SET expires=0 WHERE id=?',(o['id'],))
        self.store.deliveries();self.store.paid(o['id'],5,100,'XTR','late-charge')
        self.assertEqual(self.store.orders()[0]['status'],'needs_review')
        self.assertEqual(self.store.products()[0]['available'],2)

    def test_manual_lifecycle_and_ownership(self):
        pid=self.product('manual');o=self.store.create_order(pid,5);self.pay(o)
        with self.assertRaises(ValueError):self.store.details(o['id'],6,'wrong buyer')
        self.store.details(o['id'],5,'customer@example.com')
        self.store.claim(o['id'],'manager-a')
        with self.assertRaises(ValueError):self.store.claim(o['id'],'manager-b')
        with self.assertRaises(ValueError):self.store.complete(o['id'],'bad','manager-b')
        self.store.complete(o['id'],'Done','manager-a')
        with self.assertRaises(ValueError):self.store.complete(o['id'],'Again','manager-a')
        delivery=self.store.deliveries()[0]
        self.assertIn('Done',self.store.decrypt(delivery['body']))
        self.store.delivery_result(delivery['id'],True)
        self.assertEqual(self.store.deliveries(),[])

    def test_failed_delivery_retries_same_secret(self):
        self.stock();o=self.store.create_order(self.pid,5);self.pay(o)
        d=self.store.deliveries()[0];self.store.delivery_result(d['id'],False)
        self.assertEqual(self.store.deliveries(),[])
        with self.store.tx() as db:db.execute('UPDATE outbox SET next_try=0')
        self.assertEqual(self.store.deliveries()[0]['body'],d['body'])

    def credit(self, uid=5, amount=100, charge='deposit-charge'):
        tid=self.store.create_topup(uid,amount)
        self.store.approve_topup(tid,uid,amount,'XTR')
        self.store.credit_topup(tid,uid,amount,'XTR',charge)
        return tid

    def test_topup_is_idempotent_and_persistent(self):
        tid=self.credit()
        self.assertFalse(self.store.credit_topup(tid,5,100,'XTR','deposit-charge'))
        reopened=Store(self.store.path,self.key)
        self.assertEqual(reopened.wallet(5)['balance'],100)
        self.assertEqual(len(self.store.wallet(5)['history']),1)
        self.assertEqual(self.store.wallet(6)['balance'],0)

    def test_topup_rejects_mismatched_payment(self):
        tid=self.store.create_topup(5,100)
        for uid,amount,currency in [(6,100,'XTR'),(5,99,'XTR'),(5,100,'USD')]:
            with self.assertRaises(ValueError):self.store.credit_topup(tid,uid,amount,currency,'bad')
        self.assertEqual(self.store.wallet(5)['balance'],0)
        for amount in (0,-1,10001,1.5):
            with self.assertRaises(ValueError):self.store.create_topup(5,amount)

    def test_wallet_spend_only_once(self):
        self.credit();self.stock();o=self.store.create_order(self.pid,5)
        self.store.pay_wallet(o['id'],5);self.store.pay_wallet(o['id'],5)
        self.assertEqual(self.store.wallet(5)['balance'],0)
        self.assertEqual(len(self.store.deliveries()),1)

    def test_wallet_cannot_overspend_under_concurrency(self):
        self.credit();self.stock()
        other=self.product('manual')
        orders=[self.store.create_order(self.pid,5),self.store.create_order(other,5)]
        def pay(order):
            try:self.store.pay_wallet(order['id'],5);return True
            except ValueError:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(pay,orders)),1)
        self.assertEqual(self.store.wallet(5)['balance'],0)

    def test_topup_charge_cannot_be_reused(self):
        self.credit();tid=self.store.create_topup(6,100)
        with self.assertRaises(ValueError):self.store.credit_topup(tid,6,100,'XTR','deposit-charge')
        self.assertEqual(self.store.payment_issues()[0]['payload'],tid)
        self.assertEqual(self.store.wallet(6)['balance'],0)
        self.stock();o=self.store.create_order(self.pid,5)
        with self.assertRaises(ValueError):self.store.paid(o['id'],5,100,'XTR','deposit-charge')

    def test_charge_cannot_pay_two_orders(self):
        self.stock();a=self.store.create_order(self.pid,5);b=self.store.create_order(self.pid,6)
        self.pay(a)
        with self.assertRaises(ValueError):self.pay(b)
        self.assertEqual([i['payload'] for i in self.store.payment_issues()],[b['id']])
        self.assertEqual(next(o for o in self.store.orders() if o['id']==b['id'])['status'],'pending')

    def test_admin_adjustment_idempotent_and_audited(self):
        self.store.adjust_wallet(99,50,'Compensation','unique-id','owner')
        self.store.adjust_wallet(99,50,'Compensation','unique-id','owner')
        self.assertEqual(self.store.wallet(99)['balance'],50)
        with self.assertRaises(ValueError):self.store.adjust_wallet(99,-51,'Wrong','second-id','owner')
        with self.assertRaises(ValueError):self.store.adjust_wallet(99,60,'Changed','unique-id','owner')
        self.assertTrue(any(a['action'].startswith('wallet.adjust') for a in self.store.audits()))
        self.assertEqual(self.store.customers()[0]['user_id'],99)
        self.assertEqual(self.store.metrics()['wallet_total'],50)

    def test_usd_migration_and_frozen_topup_rate(self):
        self.credit(amount=250)
        old_topup=self.store.create_topup(6,250)
        result=self.store.set_currency('250','owner')
        self.assertTrue(Path(self.store.path).with_name(result['backup']).exists())
        self.assertEqual(self.store.wallet(5)['balance'],100)
        self.assertEqual(self.store.products()[0]['price'],40)
        self.store.set_currency('100','owner')
        self.assertEqual(self.store.wallet(5)['balance'],100)
        self.store.credit_topup(old_topup,6,250,'XTR','old-quote')
        self.assertEqual(self.store.wallet(6)['balance'],100)
        tid=self.store.create_topup(6,250)
        self.store.credit_topup(tid,6,250,'XTR','new-quote')
        self.assertEqual(self.store.wallet(6)['balance'],350)

    def test_order_provider_amount_is_frozen_after_rate_change(self):
        self.store.set_currency('250','owner');self.stock()
        order=self.store.create_order(self.pid,5)
        self.assertEqual(order['price'],40)
        self.assertEqual(order['payment_stars'],100)
        self.store.set_currency('100','owner')
        self.store.approve(order['id'],5,100,'XTR')
        self.store.paid(order['id'],5,100,'XTR','frozen-order-charge')
        self.assertEqual(self.store.orders()[0]['status'],'fulfilled')

    def test_usd_precision_and_invoice_rounding(self):
        from money import usd_cents,stars_to_cents,cents_to_stars
        self.assertEqual(usd_cents('2.50'),250)
        self.assertEqual(stars_to_cents(250,'100'),250)
        self.assertEqual(cents_to_stars(1,'250'),3)
        with self.assertRaises(ValueError):usd_cents('1.001')

    def test_customer_profile_persists_and_updates_without_duplicates(self):
        self.store.remember_customer(42,'first_user','First','Name')
        first=self.store.customers()[0]['first_seen']
        self.store.remember_customer(42,'new_username','New','Name')
        reopened=Store(self.store.path,self.key)
        customers=reopened.customers()
        self.assertEqual(len(customers),1)
        self.assertEqual(customers[0]['username'],'new_username')
        self.assertEqual(customers[0]['first_seen'],first)
        self.assertGreaterEqual(customers[0]['last_seen'],first)
        self.assertEqual(customers[0]['spent'],0)

    def test_legacy_customer_stays_visible_without_registry(self):
        self.store.create_topup(77,100)
        self.store.remember_customer(42,'visitor','Visitor')
        self.assertEqual({c['user_id'] for c in self.store.customers()},{42,77})

    def test_set_exact_balance_large_zero_and_replay(self):
        self.store.set_wallet(42,123456789,'Set exact','first','owner')
        self.assertEqual(self.store.wallet(42)['balance'],123456789)
        self.store.set_wallet(42,0,'Reset','second','owner')
        self.store.set_wallet(42,123456789,'Replay','first','owner')
        self.assertEqual(self.store.wallet(42)['balance'],0)
        with self.assertRaises(ValueError):self.store.set_wallet(42,-1,'Invalid','third','owner')

    def test_resend_preserves_stock_and_returns_order_details(self):
        self.stock();o=self.store.create_order(self.pid,5);self.pay(o)
        delivery=self.store.deliveries()[0]
        self.store.delivery_result(delivery['id'],True)
        self.store.resend(o['id'],'owner')
        self.assertEqual(len(self.store.deliveries()),1)
        self.assertEqual(self.store.products()[0]['available'],1)
        info=self.store.order_info(o['id'],'owner')
        self.assertEqual(info['result'],'secret-1')
        self.assertFalse(info['delivery']['sent'])
        self.assertEqual(self.store.metrics()['revenue'],100)
        self.assertEqual(self.store.customers()[0]['spent'],100)
        pending=self.store.create_order(self.pid,6)
        with self.assertRaises(ValueError):self.store.resend(pending['id'],'owner')


class OverhaulTests(unittest.TestCase):
    """Behaviour introduced by the data-layer overhaul."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.key=Fernet.generate_key()
        self.store=Store(Path(self.temp.name)/'shop.db',self.key)
        self.pid=self.store.save_product(dict(category='Test',name='License',description='',price=100,mode='auto',instruction='',active=True))

    def tearDown(self):self.temp.cleanup()

    def later(self,seconds):return patch('store.time.time',return_value=time.time()+seconds)

    def sql(self,query,params=()):
        with self.store.read() as db:return [tuple(row) for row in db.execute(query,params).fetchall()]

    # -- connections and reads
    def test_reads_do_not_need_the_write_lock(self):
        import sqlite3
        self.store.import_stock(self.pid,'a')
        self.assertEqual(self.sql('PRAGMA journal_mode')[0][0],'wal')
        blocker=sqlite3.connect(self.store.path,isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')
        try:
            started=time.time()
            self.assertEqual(self.store.products()[0]['available'],1)
            self.assertEqual(self.store.product(self.pid)['available'],1)
            self.assertEqual(self.store.wallet(5)['balance'],0)
            self.assertLess(time.time()-started,2)
        finally:blocker.rollback();blocker.close()

    def test_nested_transactions_use_separate_connections(self):
        with self.store.tx() as db:
            db.execute("INSERT INTO meta VALUES('nested','1')")
            self.assertEqual(self.store.language(5),'')  # read inside a write on the same thread
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key='nested'")[0][0],'1')

    def test_reads_compute_lapsed_reservations_without_writing(self):
        self.store.import_stock(self.pid,'a\nb')
        order=self.store.create_order(self.pid,5)
        with self.store.tx() as db:db.execute('UPDATE orders SET expires=? WHERE id=?',(time.time()-1,order['id']))
        self.assertEqual(self.store.products()[0]['available'],2)
        self.assertEqual(self.store.product(self.pid)['available'],2)
        stock=self.store.stock_items(self.pid)
        self.assertEqual((stock['free'],stock['reserved']),(2,0))
        self.assertTrue(all(item['order_id'] is None for item in stock['items']))
        self.assertEqual(self.store.stock_items(self.pid,'reserved')['total'],0)
        self.assertEqual(self.store.orders()[0]['status'],'expired')
        self.assertEqual(self.sql('SELECT status FROM orders')[0][0],'pending')  # nothing was written
        self.assertEqual(self.store.housekeeping()['expired'],1)
        self.assertEqual(self.sql('SELECT count(*) FROM stock WHERE order_id IS NOT NULL')[0][0],0)

    def test_product_lookup(self):
        self.store.import_stock(self.pid,'a\nb')
        self.assertEqual(self.store.product(self.pid)['available'],2)
        self.assertEqual(self.store.product(self.pid),self.store.products()[0])
        self.store.save_product(dict(category='Test',name='License',price=100,mode='auto',active=False),product_id=self.pid)
        self.assertIsNone(self.store.product(self.pid))
        self.assertEqual(self.store.product(self.pid,public=False)['id'],self.pid)
        self.assertIsNone(self.store.product(999,public=False))

    def test_indexes_exist(self):
        names={row[0] for row in self.sql("SELECT name FROM sqlite_master WHERE type='index'")}
        for name in ('stock_order','orders_pending','outbox_due','crypto_attempts_due','crypto_user',
                     'crypto_checks_due','audit_category','audit_level','topup_user','crypto_amount_active'):
            self.assertIn(name,names)
        self.assertEqual(self.sql('PRAGMA user_version')[0][0],SCHEMA_VERSION)

    # -- orders
    def test_create_order_reuses_live_unpaid_order(self):
        self.store.import_stock(self.pid,'a\nb\nc\nd')
        first=self.store.create_order(self.pid,5)
        again=self.store.create_order(self.pid,5)
        self.assertEqual(again['id'],first['id'])
        self.assertGreaterEqual(again['expires'],first['expires'])
        self.assertNotEqual(self.store.create_order(self.pid,5,2)['id'],first['id'])
        self.assertNotEqual(self.store.create_order(self.pid,6)['id'],first['id'])
        self.store.approve(first['id'],5,100,'XTR')
        with self.assertRaises(ValueError):self.store.create_order(self.pid,5)  # no stock left
        self.assertEqual(len(self.store.orders(5)),2)

    def test_changed_price_replaces_reused_order(self):
        self.store.import_stock(self.pid,'a\nb')
        first=self.store.create_order(self.pid,5)
        self.store.save_product(dict(category='Test',name='License',price=150,mode='auto'),product_id=self.pid)
        second=self.store.create_order(self.pid,5)
        self.assertNotEqual(second['id'],first['id'])
        self.assertEqual(second['price'],150)
        self.assertEqual(self.store.product(self.pid)['available'],1)

    def test_wallet_payment_refuses_approved_stars_checkout(self):
        self.store.import_stock(self.pid,'a')
        self.store.adjust_wallet(5,500,'fund','fund','owner')
        order=self.store.create_order(self.pid,5)
        self.store.approve(order['id'],5,100,'XTR')
        with self.assertRaises(ValueError):self.store.pay_wallet(order['id'],5)
        self.assertEqual(self.store.wallet(5)['balance'],500)
        self.store.paid(order['id'],5,100,'XTR','stars-charge')
        self.assertEqual(self.store.orders()[0]['status'],'fulfilled')
        self.assertEqual(self.store.payment_issues(),[])

    # -- payment issues
    def test_rejected_stars_charge_is_recorded_even_though_payment_rolls_back(self):
        self.store.import_stock(self.pid,'a')
        order=self.store.create_order(self.pid,5)
        self.store.adjust_wallet(5,100,'fund','fund','owner');self.store.pay_wallet(order['id'],5)
        for _ in range(2):  # Telegram may deliver the same update twice
            with self.assertRaises(ValueError):self.store.paid(order['id'],5,100,'XTR','real-charge')
        issues=self.store.payment_issues()
        self.assertEqual(len(issues),1)
        self.assertEqual((issues[0]['kind'],issues[0]['payload'],issues[0]['charge'],issues[0]['amount']),('order',order['id'],'real-charge',100))
        errors=self.store.events(level='error')['items']
        self.assertEqual([e['action'] for e in errors],['payment.issue'])
        self.assertEqual(errors[0]['category'],'payments')
        self.assertEqual(errors[0]['summary'],'Платёж получен, но не проведён · требуется проверка')
        self.assertEqual(self.store.metrics()['payment_issues'],1)
        with self.assertRaises(ValueError):self.store.resolve_payment_issue(issues[0]['id'],'','owner')
        self.store.resolve_payment_issue(issues[0]['id'],'Refunded in Telegram','owner')
        with self.assertRaises(ValueError):self.store.resolve_payment_issue(issues[0]['id'],'again','owner')
        self.assertEqual(self.store.payment_issues(),[])
        self.assertEqual(self.store.payment_issues(open_only=False)[0]['resolution'],'Refunded in Telegram')

    def test_rejected_topup_charge_is_recorded(self):
        tid=self.store.create_topup(5,100)
        with self.assertRaises(ValueError):self.store.credit_topup(tid,5,99,'XTR','topup-charge')
        self.assertEqual(self.store.payment_issues()[0]['kind'],'topup')
        self.assertEqual(self.store.wallet(5)['balance'],0)

    def test_balance_payment_errors_are_not_payment_issues(self):
        manual=self.store.save_product(dict(category='T',name='M',price=100,mode='manual'))
        o=self.store.create_order(manual,5)
        with self.assertRaises(ValueError):self.store.pay_wallet(o['id'],5)
        self.assertEqual(self.store.payment_issues(),[])

    # -- deliveries
    def test_delivery_backoff_and_single_error_on_give_up(self):
        self.store.import_stock(self.pid,'a')
        order=self.store.create_order(self.pid,5);self.store.paid(order['id'],5,100,'XTR','c')
        delivery=self.store.deliveries()[0]
        delays=[]
        for attempt in range(1,29):
            before=time.time()
            self.store.delivery_result(delivery['id'],False)
            delays.append(round(self.sql('SELECT next_try FROM outbox')[0][0]-before))
            with self.store.tx() as db:db.execute('UPDATE outbox SET next_try=0')
        self.assertEqual(delays[:7],[60,120,240,480,960,1920,3600])
        self.assertEqual(max(delays),3600)
        self.assertGreater(sum(delays),23*3600)
        self.assertEqual(self.store.events(level='error')['total'],0)
        self.store.delivery_result(delivery['id'],False)
        self.assertEqual(self.store.deliveries(),[])
        self.assertEqual(self.store.events(level='error')['items'][0]['action'],'delivery.failed')
        self.store.delivery_result(delivery['id'],False)
        self.assertEqual(self.store.events(level='error')['total'],1)
        self.assertEqual(self.store.metrics()['delivery_failed'],1)
        self.store.resend(order['id'],'owner')
        self.assertEqual(self.sql('SELECT attempts,failed FROM outbox')[0],(0,None))
        self.assertEqual(len(self.store.deliveries()),1)

    def test_permanent_delivery_failure_gives_up_at_once(self):
        self.store.import_stock(self.pid,'a')
        order=self.store.create_order(self.pid,5);self.store.paid(order['id'],5,100,'XTR','c')
        self.store.delivery_result(self.store.deliveries()[0]['id'],False,permanent=True)
        self.assertEqual(self.store.deliveries(),[])
        self.assertIsNotNone(self.store.order_info(order['id'],'owner')['delivery']['failed'])
        self.assertEqual(self.store.events(level='error')['total'],1)

    def test_deliveries_are_ordered_by_due_time(self):
        self.store.import_stock(self.pid,'a\nb')
        for user in (5,6):
            o=self.store.create_order(self.pid,user);self.store.paid(o['id'],user,100,'XTR','c'+str(user))
        with self.store.tx() as db:db.execute('UPDATE outbox SET next_try=CASE user_id WHEN 5 THEN 2 ELSE 1 END')
        self.assertEqual([d['user_id'] for d in self.store.deliveries()],[6,5])

    # -- housekeeping
    def test_housekeeping_prunes_inputs_and_old_outbox_bodies(self):
        self.store.remember_customer(5);self.store.remember_customer(6)
        self.store.input_mode(5,'qty:1');self.store.input_mode(6,'txid')
        with self.store.tx() as db:
            db.execute("INSERT INTO meta VALUES('input_mode:7','')")
            db.execute('UPDATE telegram_users SET last_seen=0 WHERE user_id=6')
        self.store.set_wallet(5,10,'Set','ref','owner')
        with self.store.tx() as db:db.execute("INSERT INTO meta VALUES('set:legacy','5:10')")
        self.store.import_stock(self.pid,'secret-line')
        order=self.store.create_order(self.pid,5);self.store.paid(order['id'],5,100,'XTR','c')
        self.store.delivery_result(self.store.deliveries()[0]['id'],True)
        self.store.housekeeping()
        keys={row[0] for row in self.sql('SELECT key FROM meta')}
        self.assertIn('input_mode:5',keys)
        self.assertFalse({'input_mode:6','input_mode:7','set:legacy'}&keys)
        self.assertIn('secret-line',self.store.decrypt(self.sql('SELECT body FROM outbox')[0][0]))
        with self.later(8*86400):self.assertEqual(self.store.housekeeping()['outbox'],1)
        self.assertEqual(self.store.decrypt(self.sql('SELECT body FROM outbox')[0][0]),'')
        with self.later(8*86400):self.assertEqual(self.store.housekeeping()['outbox'],0)
        self.store.set_wallet(5,10,'Replay','ref','owner')  # idempotency survives the cleanup
        with self.assertRaises(ValueError):self.store.set_wallet(5,11,'Other','ref','owner')
        self.store.resend(order['id'],'owner')
        self.assertIn('secret-line',self.store.decrypt(self.store.deliveries()[0]['body']))

    def test_input_mode_writes_only_on_change(self):
        self.store.input_mode(5,'qty:1')
        with patch.object(Store,'tx',side_effect=AssertionError('no write expected')):
            self.assertEqual(self.store.input_mode(5,'qty:1'),'qty:1')
            self.assertEqual(self.store.input_mode(5),'qty:1')
        self.assertEqual(self.store.input_mode(5,''),'')
        self.assertEqual(self.sql("SELECT count(*) FROM meta WHERE key='input_mode:5'")[0][0],0)

    # -- money and settings
    def test_currency_conversion_converts_costs_and_backs_up_once(self):
        pid=self.store.save_product(dict(category='T',name='Cost',price=250,mode='auto',cost_price=125))
        self.store.import_stock(pid,'x',unit_cost=50)
        order=self.store.create_order(pid,5);self.store.paid(order['id'],5,250,'XTR','c')
        with self.store.tx() as db:
            db.execute("""INSERT INTO finance_entries(id,kind,category,amount,note,occurred_at,created_at,actor)
                VALUES('f','expense','server',1,'n',0,0,'owner')""")
        first=self.store.set_currency('250','owner')
        self.assertTrue(first['backup'])
        self.assertEqual(self.sql('SELECT cost_price FROM products WHERE id=?',(pid,))[0][0],50)
        self.assertEqual(self.sql('SELECT unit_cost FROM stock')[0][0],20)
        self.assertEqual(self.sql('SELECT cost_total FROM orders')[0][0],20)
        self.assertEqual(self.sql('SELECT amount FROM finance_entries')[0][0],1)
        second=self.store.set_currency('100','owner')
        self.assertIsNone(second['backup'])
        self.assertEqual(len(list(Path(self.temp.name).glob('*.bak'))),1)
        self.assertEqual(self.sql('SELECT cost_price FROM products WHERE id=?',(pid,))[0][0],50)

    def test_settings_cache_is_invalidated_across_instances(self):
        other=Store(self.store.path,self.key)
        self.assertEqual(other.money(250),'250 Stars')
        self.store.set_currency('100','owner')
        self.assertEqual(other.money(250),'$2.50')
        self.store.emoji_assignments();self.store.payment_methods()
        with patch.object(Store,'read',side_effect=AssertionError('cached')):
            self.assertEqual(self.store.currency()['code'],'USD')
            self.store.emoji_assignments();self.store.payment_methods()
        cached=self.store.payment_methods();cached['networks'].clear()
        self.assertTrue(self.store.payment_methods()['networks'])
        with self.store.tx() as db:db.execute("DELETE FROM meta WHERE key='stars_per_usd'")
        self.assertFalse(self.store.currency()['active'])

    def test_topup_rounds_down_expires_and_is_limited(self):
        from money import credit_cents
        self.store.set_currency('300','owner')
        self.assertEqual(credit_cents(100,'300'),33)
        self.assertEqual(credit_cents(200,'300'),66)
        tid=self.store.create_topup(5,200)
        self.assertEqual(self.store.topup_info(tid)['credit_cents'],66)
        for _ in range(4):self.store.create_topup(5,100)
        with self.assertRaises(ValueError):self.store.create_topup(5,100)
        self.store.create_topup(6,100)
        with self.later(2*3600):
            with self.assertRaises(ValueError):self.store.approve_topup(tid,5,200,'XTR')
            self.assertTrue(self.store.credit_topup(tid,5,200,'XTR','late'))  # Stars were taken
            self.store.create_topup(5,100)
        self.assertEqual(self.store.wallet(5)['balance'],66)
        self.assertEqual(self.store.payment_issues(),[])

    def test_wallet_history_has_kind_and_reference(self):
        tid=self.store.create_topup(5,100);self.store.credit_topup(tid,5,100,'XTR','c')
        self.store.adjust_wallet(5,5,'gift','g','owner')
        o=self.store.create_order(self.store.save_product(dict(category='T',name='M',price=50,mode='manual')),5)
        self.store.pay_wallet(o['id'],5)
        history=self.store.wallet(5)['history']
        self.assertEqual([(h['kind'],h['reference']) for h in history],
                         [('order','order:'+o['id']),('adjust','admin:g'),('stars',tid)])

    def test_customers_page_filters_and_paginates(self):
        for uid in range(1,8):self.store.remember_customer(uid,'user'+str(uid),'Name'+str(uid))
        self.store.remember_customer(1234,'special','Ivan','Petrov')
        page=self.store.customers_page(page_size=3)
        self.assertEqual((page['total'],page['pages'],len(page['items'])),(8,3,3))
        self.assertEqual(set(page['items'][0])-{'tags','banned'},set(self.store.customers()[0]))
        self.assertEqual([c['user_id'] for c in self.store.customers_page('@special')['items']],[1234])
        self.assertEqual([c['user_id'] for c in self.store.customers_page('ivan petrov')['items']],[1234])
        self.assertEqual([c['user_id'] for c in self.store.customers_page('123')['items']],[1234])
        self.assertEqual(self.store.customers_page('%')['total'],0)
        self.assertEqual(self.store.customers_page(page=99,page_size=3)['page'],3)
        with self.assertRaises(ValueError):self.store.customers_page(page_size=0)

    # -- stock fingerprints and encryption
    def test_fingerprints_are_keyed_and_legacy_rows_migrate(self):
        import hashlib,sqlite3
        from store import stock_fingerprint
        self.store.import_stock(self.pid,'alpha\nbeta')
        fingerprints={row[0] for row in self.sql('SELECT fingerprint FROM stock')}
        self.assertNotIn(hashlib.sha256(b'alpha').hexdigest(),fingerprints)
        self.assertIn(stock_fingerprint(self.key,'alpha'),fingerprints)
        other=Fernet.generate_key()
        self.assertNotEqual(stock_fingerprint(other,'alpha'),stock_fingerprint(self.key,'alpha'))
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE stock SET fingerprint=? WHERE id=1",(hashlib.sha256(b'alpha').hexdigest(),))
            db.execute('PRAGMA user_version=1');db.commit()
        reopened=Store(self.store.path,self.key)
        self.assertEqual({row[0] for row in self.sql('SELECT fingerprint FROM stock')},fingerprints)
        self.assertEqual(reopened.import_stock(self.pid,'alpha\ngamma'),{'added':1,'duplicates':1})
        self.assertEqual(self.sql('PRAGMA user_version')[0][0],SCHEMA_VERSION)

    def test_wrong_key_leaves_fingerprint_migration_pending(self):
        import hashlib,sqlite3
        self.store.import_stock(self.pid,'alpha')
        legacy=hashlib.sha256(b'alpha').hexdigest()
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute('UPDATE stock SET fingerprint=?',(legacy,));db.execute('PRAGMA user_version=1');db.commit()
        Store(self.store.path,Fernet.generate_key())
        self.assertEqual(self.sql('SELECT fingerprint FROM stock')[0][0],legacy)
        self.assertEqual(self.sql('PRAGMA user_version')[0][0],1)
        Store(self.store.path,self.key)
        self.assertNotEqual(self.sql('SELECT fingerprint FROM stock')[0][0],legacy)

    def test_decrypt_failure_is_value_error(self):
        with self.assertRaises(ValueError):self.store.decrypt(b'not-a-token')
        with self.assertRaises(ValueError):Store(Path(self.temp.name)/'other.db',Fernet.generate_key()).decrypt(self.store.encrypt('x'))
        self.assertEqual(self.store.decrypt(None),'')

    # -- validation
    def test_bad_input_raises_value_error(self):
        bad_products=[dict(name='x',price=1,mode='auto'),dict(category='c',price=1,mode='auto'),
            dict(category='c',name='x',mode='auto'),dict(category='c',name='x',price=0,mode='auto'),
            dict(category='c',name='x',price=-5,mode='auto'),dict(category='c',name='x',price=1.5,mode='auto'),
            dict(category='c',name='x'*201,price=1,mode='auto'),dict(category='c',name='x',price=1,mode='other'),
            dict(category=' ',name='x',price=1,mode='auto'),dict(category='c',name='x',price=1,mode='auto',emoji_id='abc'),
            dict(category='c',name='x',price=1,mode='auto',description=5),None]
        for product in bad_products:
            with self.assertRaises(ValueError,msg=product):self.store.save_product(product)
        self.assertEqual(len(self.store.products()),1)
        operations=[lambda:self.store.adjust_wallet(5,1,None,'r','owner'),lambda:self.store.set_wallet(5,1,None,'r','owner'),
            lambda:self.store.adjust_wallet(5,1,'reason',None,'owner'),lambda:self.store.assign_emoji(None,'','owner'),
            lambda:self.store.details('x',5,None),lambda:self.store.stock_items(self.pid,page='a'),
            lambda:self.store.stock_items(self.pid,page=None),lambda:self.store.input_mode(5,7),
            lambda:self.store.import_stock(self.pid,None),lambda:self.store.events(page='x')]
        for operation in operations:
            with self.assertRaises(ValueError):operation()
        self.assertEqual(self.store.stock_items(self.pid,page='1')['page'],1)
        self.store.assign_emoji('menu:catalog',None,'owner')
        self.assertEqual(self.store.emoji_assignments()['menu:catalog'],'')

    def test_log_tolerates_unknown_network_and_non_numeric_balance_target(self):
        with self.store.tx() as db:
            db.execute("""INSERT INTO crypto_deposits(id,user_id,wallet,token,amount_raw,credit_cents,created,expires,network)
                VALUES('d',5,'w','t','1',100,0,0,'future')""")
            Store.log(db,'system','crypto.review','d')
            Store.log(db,'owner','wallet.adjust','not-a-number')
        events=self.store.events()['items']
        self.assertIn('USDT future',events[1]['details'])
        self.assertIsNone(events[0]['user_id'])

    def test_usd_cents_rejects_unsafe_amounts(self):
        from decimal import Decimal
        from money import usd_cents
        self.assertEqual(usd_cents('12.5'),1250)
        self.assertEqual(usd_cents(Decimal('3.10')),310)
        self.assertEqual(usd_cents('0'),0)
        for value in ('-1','1e3','1E2','NaN','Infinity','10000000000.01','',' ','1.001',True):
            with self.assertRaises(ValueError,msg=value):usd_cents(value)
        with self.assertRaises(ValueError):usd_cents('0',positive=True)
        self.assertEqual(usd_cents('10000000000'),10**12)


if __name__=='__main__':unittest.main(verbosity=2)


class FakeNotify:
    emitted = []
    low_stock = []
    broken = False

    def __init__(self, store):self.store = store

    def emit(self, db, kind, text, *, key='', cooldown=None):
        if self.broken:
            db.execute("INSERT INTO meta VALUES('notify.partial','x')")  # rolled back with the savepoint
            raise RuntimeError('notify table missing')
        self.emitted.append((kind,text,key))
        return 1

    def check_low_stock(self, db, product_id):
        self.low_stock.append(product_id)
        return True


@contextmanager
def swap_modules(**modules):
    """Replace only the named sys.modules entries (patch.dict would also unload every module
    imported meanwhile, e.g. aiogram, and make the next test re-import it)."""
    missing = object()
    saved = {name:sys.modules.get(name,missing) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is missing:sys.modules.pop(name,None)
            else:sys.modules[name] = value


def fake_module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():setattr(module, key, value)
    return module


class V3Tests(unittest.TestCase):
    """3.0 data layer: staff notifications, richer catalog, per-user state, server-side order
    pages, audit retention, cached balances and the customer registry (3.2: promo codes credit
    the wallet, see test_promo; orders only keep the 3.0 discount columns)."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.key=Fernet.generate_key()
        FakeNotify.emitted=[];FakeNotify.low_stock=[];FakeNotify.broken=False
        self.modules=swap_modules(notify=fake_module('notify',Notify=FakeNotify))
        self.modules.__enter__()
        self.store=Store(Path(self.temp.name)/'shop.db',self.key)
        self.pid=self.store.save_product(dict(category='Игры',name='Ключ Steam',description='Активация в клиенте',price=1000,mode='auto'))

    def tearDown(self):
        self.modules.__exit__(None,None,None);self.temp.cleanup()

    def later(self,seconds):return patch('store.time.time',return_value=time.time()+seconds)

    def sql(self,query,params=()):
        with self.store.read() as db:return [tuple(row) for row in db.execute(query,params).fetchall()]

    def pay(self,order,charge='charge-1'):
        self.store.approve(order['id'],order['user_id'],order['payment_stars'],'XTR')
        self.store.paid(order['id'],order['user_id'],order['payment_stars'],'XTR',charge)

    # -- schema
    def test_optional_modules_are_initialized_inside_the_migration(self):
        calls=[]
        module=fake_module('support',initialize=lambda db:calls.append(db.in_transaction))
        with swap_modules(support=module):Store(self.store.path,self.key)
        self.assertEqual(calls,[True])
        with swap_modules(support=None,staff=None,promo=None,notify=None):
            Store(self.store.path,self.key)  # absent modules are skipped

    def test_migrates_a_v2_database(self):
        try:
            source=subprocess.run(['git','show','v2.0:store.py'],capture_output=True,check=True,
                                  cwd=Path(__file__).resolve().parent).stdout.decode()
        except (OSError,subprocess.CalledProcessError):
            self.skipTest('git history with tag v2.0 is not available')
        legacy=types.ModuleType('store_v2');legacy.__file__='store_v2.py'
        exec(compile(source,'store_v2.py','exec'),legacy.__dict__)
        path=Path(self.temp.name)/'v2.db'
        old=legacy.Store(path,self.key)
        pid=old.save_product(dict(category='A',name='Old',price=100,mode='auto'))
        old.import_stock(pid,'one\ntwo')
        order=old.create_order(pid,501);old.approve(order['id'],501,100,'XTR');old.paid(order['id'],501,100,'XTR','c1')
        old.set_wallet(502,700,'Gift','g1','owner');old.adjust_wallet(502,-200,'Fix','g2','owner')
        old.create_topup(503,50)
        with old.tx() as db:db.execute('DELETE FROM telegram_users')  # legacy shops had no registry
        old.close();del old
        new=Store(path,self.key)
        with new.read() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
            columns={row['name'] for row in db.execute('PRAGMA table_info(orders)')}
        self.assertTrue({'subtotal','discount_cents','promo_id'}<=columns)
        self.assertEqual(new.wallet(502)['balance'],500)
        self.assertEqual(new.verify_wallet_balances(),[])
        self.assertEqual({c['user_id'] for c in new.customers_page()['items']},{501,502,503})
        self.assertEqual(new.purchases(501)['total'],1)
        self.assertEqual(new.product(pid)['image_path'],'')
        Store(path,self.key)  # idempotent
        self.assertEqual(new.customers_page()['total'],3)

    # -- promo codes (3.2: wallet credits; orders keep only the 3.0 discount columns)
    def test_orders_carry_no_promo_code(self):
        self.store.import_stock(self.pid,'k1\nk2')
        with self.assertRaises(TypeError):self.store.create_order(self.pid,42,promo_code='SALE10')
        order=self.store.create_order(self.pid,42)
        self.assertEqual((order['price'],order['subtotal'],order['discount_cents'],order['promo_id']),(1000,1000,0,None))
        self.assertEqual(self.store.create_order(self.pid,42)['id'],order['id'])
        self.pay(order)
        self.assertEqual(self.sql("SELECT count(*) FROM audit WHERE action LIKE 'promo.%'"),[(0,)])
        self.assertNotIn('промокод',FakeNotify.emitted[0][1].lower())

    def test_live_3_1_order_with_a_discount_is_replaced(self):
        self.store.import_stock(self.pid,'k1')
        order=self.store.create_order(self.pid,42)
        with self.store.tx() as db:
            db.execute('UPDATE orders SET price=900,payment_stars=900,discount_cents=100,promo_id=7 WHERE id=?',(order['id'],))
        fresh=self.store.create_order(self.pid,42)
        self.assertNotEqual(fresh['id'],order['id'])
        self.assertEqual((fresh['price'],fresh['promo_id']),(1000,None))
        self.assertEqual(self.sql('SELECT status FROM orders WHERE id=?',(order['id'],)),[('expired',)])

    def test_old_discounts_and_promo_credits_in_analytics(self):
        from analytics import Analytics
        self.store.import_stock(self.pid,'k1')
        order=self.store.create_order(self.pid,42)
        with self.store.tx() as db:  # an order discounted by a 3.1 promo code
            db.execute('UPDATE orders SET price=900,payment_stars=900,discount_cents=100,promo_id=7 WHERE id=?',(order['id'],))
        self.store.set_currency(100,'owner')  # 100 Stars per $1: 1000 Stars → 1000 cents
        order=self.store.order_info(order['id'],'owner')
        self.assertEqual((order['price'],order['subtotal'],order['discount_cents']),(900,1000,100))
        with self.store.tx() as db:
            db.execute("INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(42,500,'promo:1:42:1',?)",(time.time(),))
            db.execute("INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(43,700,'topup:t1',?)",(time.time(),))
        self.store.paid(order['id'],42,order['payment_stars'],'XTR','c1')
        summary=Analytics(self.store).report('day')['summary']
        self.assertEqual((summary['revenue'],summary['discounts'],summary['promo_credits'],summary['wallet_deposits']),(900,100,500,700))

    # -- notifications
    def test_staff_notifications_are_queued_inside_the_transactions(self):
        self.store.import_stock(self.pid,'k1')
        self.pay(self.store.create_order(self.pid,42))
        self.assertEqual([kind for kind,_,_ in FakeNotify.emitted],['new_paid_order'])
        self.assertIn('Ключ Steam × 1',FakeNotify.emitted[0][1])
        self.assertEqual(FakeNotify.low_stock,[self.pid])
        manual=self.store.save_product(dict(category='Услуги',name='Настройка',price=500,mode='manual'))
        self.pay(self.store.create_order(manual,42),'charge-2')
        self.store.details(self.sql('SELECT id FROM orders WHERE product_id=?',(manual,))[0][0],42,'secret login data')
        self.assertEqual(FakeNotify.emitted[-1][0],'manual_order_details')
        self.assertNotIn('secret login data',str(FakeNotify.emitted))
        with self.assertRaises(ValueError):self.store.paid('missing',42,1,'XTR','charge-x')
        self.assertEqual(FakeNotify.emitted[-1][0],'payment_issue')

    def test_failing_notifier_never_breaks_a_payment(self):
        FakeNotify.broken=True
        self.store.import_stock(self.pid,'k1')
        order=self.store.create_order(self.pid,42)
        with self.assertLogs('themarket.store','ERROR'):self.pay(order)
        self.assertEqual(self.sql('SELECT status FROM orders WHERE id=?',(order['id'],))[0][0],'fulfilled')
        self.assertEqual(self.sql("SELECT count(*) FROM meta WHERE key='notify.partial'")[0][0],0)
        with swap_modules(notify=None):
            self.store.import_stock(self.pid,'k2');self.pay(self.store.create_order(self.pid,43),'charge-2')

    # -- catalog
    def test_card_fields_sorting_and_validation(self):
        other=self.store.save_product(dict(category='Игры',name='Пополнение',price=500,mode='auto',sort=-5,badge=' Хит ',old_price=700))
        product=self.store.product(other)
        self.assertEqual((product['sort'],product['badge'],product['old_price']),(-5,'Хит',700))
        self.assertEqual([p['id'] for p in self.store.products(public=True)],[other,self.pid])
        self.store.reorder_products([self.pid,other],'owner')
        self.assertEqual([p['id'] for p in self.store.products()],[self.pid,other])
        for bad in (dict(old_price=400),dict(badge='x'*17),dict(sort='1'),dict(old_price=0)):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                self.store.save_product(dict(category='Игры',name='X',price=500,mode='auto',**bad),product_id=other)
        self.store.save_product(dict(category='Игры',name='Пополнение',price=800,mode='auto'),product_id=other)
        self.assertIsNone(self.store.product(other)['old_price'])  # never a fake discount
        self.assertEqual(self.store.product(other)['badge'],'Хит')  # absent keys are kept
        with self.assertRaises(ValueError):self.store.reorder_products([self.pid,999],'owner')

    def test_move_product_within_category(self):
        make=lambda name,category='Игры':self.store.save_product(dict(category=category,name=name,price=500,mode='auto'))
        b,c=make('B'),make('C');elsewhere=make('Z','Другое')
        order=lambda:[p['id'] for p in self.store.products() if p['category']=='Игры']
        self.assertEqual(order(),[self.pid,b,c])  # all sort=0: ties fall back to id
        self.assertEqual(self.store.move_product(c,'up','owner'),{'ok':True,'moved':True})
        self.assertEqual(order(),[self.pid,c,b])  # ties were renumbered first
        self.assertEqual(self.store.move_product(self.pid,'up','owner')['moved'],False)
        self.assertEqual(self.store.move_product(b,'down','owner')['moved'],False)
        self.store.move_product(self.pid,'down','owner')
        self.assertEqual(order(),[c,self.pid,b])
        self.assertEqual(self.store.move_product(elsewhere,'up','owner')['moved'],False)  # alone in its category
        sorts=[p['sort'] for p in self.store.products() if p['category']=='Игры']
        self.assertEqual(len(set(sorts)),3)
        event=self.sql("SELECT actor,target,category FROM audit WHERE action='product.move' ORDER BY id DESC")[0]
        self.assertEqual(tuple(event),('owner',str(self.pid),'products'))
        self.assertEqual(AUDIT_LABELS['product.move'],'Товар перемещён в списке')
        for bad in ((self.pid,'left'),(999,'up')):
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.store.move_product(*bad,'owner')

    def test_product_image_lifecycle(self):
        folder=self.store.data_dir()/'product-images';folder.mkdir()
        first='product-images/%d-%s.jpg'%(self.pid,'a'*8);second='product-images/%d-%s.jpg'%(self.pid,'b'*8)
        (self.store.data_dir()/first).write_bytes(b'jpeg');(self.store.data_dir()/second).write_bytes(b'jpeg')
        self.store.set_product_image(self.pid,first,'owner')
        self.assertTrue(self.store.cache_image_file_id(self.pid,'AgAD-file',first))
        self.assertEqual(self.store.product(self.pid)['image_file_id'],'AgAD-file')
        self.assertEqual(self.store.product_image(self.pid),self.store.data_dir()/first)
        self.store.set_product_image(self.pid,second,'owner')
        self.assertEqual(self.store.product(self.pid)['image_file_id'],'')
        self.assertFalse((self.store.data_dir()/first).exists())
        for bad in ('../shop.db','product-images/999-aaaaaaaa.jpg','/etc/passwd',None):
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.store.set_product_image(self.pid,bad,'owner')
        # A file_id uploaded for the replaced photo is not cached for the new one.
        self.assertFalse(self.store.cache_image_file_id(self.pid,'old-upload',first))
        self.assertEqual(self.store.product(self.pid)['image_file_id'],'')
        self.store.set_product_image(self.pid,'','owner')
        self.assertIsNone(self.store.product_image(self.pid))
        self.store.cache_image_file_id(self.pid,'stale',second)
        self.assertEqual(self.store.product(self.pid)['image_file_id'],'')
        self.assertIn('Изменено фото товара',str(self.store.audits()))

    def test_tampered_image_path_is_never_read_or_deleted(self):
        victim=self.store.data_dir()/'victim.txt';victim.write_text('keep')
        (self.store.data_dir()/'product-images').mkdir()
        for bad in ('victim.txt','product-images/../victim.txt','product-images/1-aaaaaaaa.jpg/../../victim.txt'):
            with self.store.tx() as db:db.execute('UPDATE products SET image_path=? WHERE id=?',(bad,self.pid))
            with self.subTest(bad=bad):
                self.assertIsNone(self.store.product_image(self.pid))
                self.store.set_product_image(self.pid,'','owner')
                self.assertTrue(victim.exists())
        link=self.store.data_dir()/'product-images'/('%d-%s.jpg'%(self.pid,'c'*8))
        link.symlink_to(victim)
        self.assertIsNone(self.store.image_file(self.store.data_dir(),'product-images/'+link.name))

    def test_categories_and_no_bot_search(self):
        self.store.save_product(dict(category='Подписки',name='Ёлка Премиум',description='Годовая подписка',price=100,mode='manual'))
        self.store.save_product(dict(category='Скрытые',name='Ключ тайный',price=100,mode='manual',active=False))
        self.assertEqual(self.store.categories(),[{'name':'Игры','count':1},{'name':'Подписки','count':1}])
        new=self.store.save_product(dict(category='Музыка',name='Плеер',price=100,mode='manual'))
        self.assertIn('Музыка',[c['name'] for c in self.store.categories()])  # saving invalidates the cache
        self.store.delete_product(new,'owner')
        self.assertFalse(hasattr(self.store,'search_products'))  # 3.2: the bot has no search any more

    def test_repeat_order(self):
        self.store.import_stock(self.pid,'k1\nk2')
        order=self.store.create_order(self.pid,42,2);self.pay(order)
        self.assertEqual(self.store.repeat_order(42,order['id']),(self.pid,2))
        with self.assertRaisesRegex(ValueError,'Покупка не найдена'):self.store.repeat_order(43,order['id'])
        self.store.delete_product(self.pid,'owner')
        with self.assertRaisesRegex(ValueError,'больше не продаётся'):self.store.repeat_order(42,order['id'])

    # -- per-user state
    def test_user_state(self):
        self.assertIsNone(self.store.user_state(42,'promo'))
        self.assertEqual(self.store.user_state(42,'promo','SALE10'),'SALE10')
        with patch.object(Store,'tx',side_effect=AssertionError('no write expected')):
            self.assertEqual(self.store.user_state(42,'promo','SALE10'),'SALE10')
        self.assertIsNone(self.store.user_state(43,'promo'))
        self.store.user_state(42,'checkout','x',ttl=60)
        with self.later(61):self.assertIsNone(self.store.user_state(42,'checkout'))
        self.assertIsNone(self.store.user_state(42,'promo',''))
        self.assertIsNone(self.store.user_state(42,'promo'))
        for bad in (('42','k','v'),(42,'','v'),(42,'k','x'*1001),(42,'k',5)):
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.store.user_state(*bad)
        self.store.user_state(42,'a','1');self.store.user_state(42,'b','2',ttl=10)
        with self.later(8*86400):self.store.housekeeping()
        self.assertEqual(self.sql('SELECT count(*) FROM user_state')[0][0],0)

    # -- panel lists
    def test_orders_page_filters_search_and_pagination(self):
        self.store.import_stock(self.pid,'\n'.join('k%d'%i for i in range(6)))
        self.store.remember_customer(77,'buyer_name','Покупатель')
        paid=[self.store.create_order(self.pid,uid) for uid in (77,78,79)]
        for index,order in enumerate(paid):self.pay(order,'c%d'%index)
        live=self.store.create_order(self.pid,80)
        lapsed=self.store.create_order(self.pid,81)
        with self.store.tx() as db:db.execute('UPDATE orders SET expires=0 WHERE id=?',(lapsed['id'],))
        page=self.store.orders_page(page_size=2)
        self.assertEqual((page['total'],page['pages'],len(page['items'])),(5,3,2))
        self.assertEqual(page['items'][0]['id'],lapsed['id'])
        self.assertEqual(page['items'][0]['status'],'expired')
        self.assertEqual({o['id'] for o in self.store.orders_page('pending')['items']},{live['id']})
        self.assertEqual({o['id'] for o in self.store.orders_page('expired')['items']},{lapsed['id']})
        self.assertEqual(self.store.orders_page('paid')['total'],3)
        self.assertEqual(self.store.orders_page('fulfilled')['total'],3)
        self.assertEqual(self.store.orders_page('attention')['total'],0)
        self.assertEqual([o['id'] for o in self.store.orders_page(query=paid[1]['id'][:8])['items']],[paid[1]['id']])
        self.assertEqual([o['user_id'] for o in self.store.orders_page(query='78')['items']],[78])
        found=self.store.orders_page(query='@buyer')['items']
        self.assertEqual([(o['user_id'],o['username']) for o in found],[(77,'buyer_name')])
        self.assertEqual(self.store.orders_page(query='steam')['total'],5)
        self.assertEqual(self.store.orders_page(query='100%')['total'],0)
        self.assertEqual(self.store.orders_page(page=99,page_size=2)['page'],3)
        for bad in (dict(status='bogus'),dict(page_size=500),dict(query=5),dict(page='x')):
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.store.orders_page(**bad)

    def test_event_search_uses_ids_and_cyrillic_labels(self):
        self.store.import_stock(self.pid,'k1')
        order=self.store.create_order(self.pid,123456);self.pay(order)
        found=self.store.events(query=order['id'][:8])
        self.assertGreaterEqual(found['total'],2)
        self.assertTrue(all(order['id'] in (e['order_id'],e['target']) for e in found['items']))
        self.assertGreaterEqual(self.store.events(query='123456')['total'],2)
        labels={e['summary'] for e in self.store.events(query='ОПЛАТА ПОЛУЧЕНА')['items']}
        self.assertEqual(labels,{'Оплата получена'})
        self.assertEqual(self.store.events(query='Ключ Steam',category='orders')['total'],
                         self.store.events(category='orders')['total'])
        page=self.store.events(query='Ключ',page_size=1,page=2)
        self.assertEqual((page['page'],len(page['items'])),(2,1))
        self.assertEqual(self.store.events(query='zzz-no-match'),dict(items=[],total=0,page=1,pages=1))

    # -- balances, registry and retention
    def test_cached_balances_follow_the_ledger(self):
        self.store.set_wallet(42,2000,'Gift','r1','owner')
        self.store.adjust_wallet(42,-300,'Fix','r2','owner')
        self.store.import_stock(self.pid,'k1')
        order=self.store.create_order(self.pid,42);self.store.pay_wallet(order['id'],42)
        self.assertEqual(self.store.wallet(42)['balance'],700)
        self.assertEqual(self.store.verify_wallet_balances(),[])
        with self.store.tx() as db:db.execute('UPDATE wallet_balances SET balance=5 WHERE user_id=42')
        self.assertEqual(self.store.verify_wallet_balances(),[{'user_id':42,'ledger':700,'cached':5}])
        with self.store.tx() as db:db.execute("DELETE FROM meta WHERE key='wallet.verified_at'")
        self.assertEqual(self.store.housekeeping()['wallet_repaired'],1)
        self.assertEqual(self.store.housekeeping()['wallet_repaired'],0)  # at most daily
        self.assertEqual(self.store.wallet(42)['balance'],700)
        self.assertIn('wallet.repair',[e['action'] for e in self.store.events(category='balance')['items']])
        self.store.set_currency(50,'owner')
        self.assertEqual(self.store.verify_wallet_balances(),[])
        self.assertEqual(self.store.metrics()['wallet_total'],self.store.wallet(42)['balance'])
        with self.store.tx() as db:db.execute('DELETE FROM wallet_ledger WHERE user_id=42')
        self.assertEqual((self.store.wallet(42)['balance'],self.store.verify_wallet_balances()),(0,[]))

    def test_customer_registry_covers_all_activity(self):
        self.store.adjust_wallet(9001,50,'Gift','x1','owner')
        self.store.create_topup(9002,10)
        self.assertEqual({c['user_id'] for c in self.store.customers_page()['items']},{9001,9002})
        self.assertEqual(self.store.customers_page(query='9001')['items'][0]['balance'],50)

    def test_audit_retention_prunes_old_info_events_in_batches(self):
        old=time.time()-200*86400
        rows=[(old+i,'telegram','order.created','o%d'%i,'info','orders') for i in range(1200)]
        rows+=[(old,'telegram','delivery.failed','x','error','orders'),(old,'owner','finance.entry','f','info','finance'),
               (old,'telegram','payment.accepted','p','info','orders'),(old,'owner','wallet.adjust:5','42','info','balance'),
               (time.time()-86400,'telegram','order.created','recent','info','orders')]
        with self.store.tx() as db:
            db.executemany('INSERT INTO audit(at,actor,action,target,level,category) VALUES(?,?,?,?,?,?)',rows)
        with patch('store.AUDIT_PRUNE_BATCH',500):
            self.assertLessEqual(self.store.housekeeping()['audit'],500)
            for _ in range(10):self.store.housekeeping()
        remaining={row[0] for row in self.sql('SELECT target FROM audit WHERE at<?',(time.time()-100*86400,))}
        self.assertEqual(remaining,{'x','f','p','42'})
        self.assertIn('recent',{row[0] for row in self.sql('SELECT target FROM audit')})
        with patch.dict(os.environ,{'AUDIT_RETENTION_DAYS':'0'}):
            with self.store.tx() as db:
                db.execute("DELETE FROM meta WHERE key='audit.pruned_until'")
                db.executemany('INSERT INTO audit(at,actor,action,target,level,category) VALUES(?,?,?,?,?,?)',rows[:3])
            self.assertEqual(self.store.housekeeping()['audit'],0)
        for value,days in (('abc',180),('5',30),('400',400)):
            with patch.dict(os.environ,{'AUDIT_RETENTION_DAYS':value}):self.assertEqual(audit_retention_days(),days)


class Features31Tests(unittest.TestCase):
    """3.1: customer CRM, order notes, support templates, bulk product actions, live feed, insights."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key()
        self.store = Store(Path(self.temp.name)/'test.db',self.key)
        self.pid = self.store.save_product(dict(category='Test',name='License',description='',price=100,mode='auto',instruction='',active=True))
        self.store.remember_customer(42,'buyer','Анна','Ким')

    def tearDown(self):
        self.temp.cleanup()

    def sql(self,query,params=()):
        with self.store.read() as db:return [tuple(r) for r in db.execute(query,params)]

    def paid_order(self,user_id=42,secret=None):
        self.store.import_stock(self.pid,secret or 'key-'+os.urandom(4).hex())
        o = self.store.create_order(self.pid,user_id)
        self.store.approve(o['id'],user_id,o['price'],'XTR')
        self.store.paid(o['id'],user_id,o['price'],'XTR','charge-'+o['id'])
        return o

    # ------------------------------------------------------------ CRM

    def test_schema_is_idempotent_and_versioned(self):
        Store(Path(self.temp.name)/'test.db',self.key)
        self.assertEqual(self.sql('PRAGMA user_version')[0][0],SCHEMA_VERSION)
        names = {r[0] for r in self.sql("SELECT name FROM sqlite_master")}
        for name in ('customer_meta','customer_tags','order_notes','support_templates','live_events','live_seen',
                     'live_from_audit','live_from_customer','live_from_support','orders_paid_customer'):
            self.assertIn(name,names)

    def test_tags_are_normalized_limited_and_filterable(self):
        meta = self.store.set_customer_tags(42,['  VIP ','vip','опт  клиент',''],'owner')
        self.assertEqual(meta['tags'],['VIP','опт клиент'])
        with self.assertRaisesRegex(ValueError,'24'):self.store.set_customer_tags(42,['x'*25],'owner')
        with self.assertRaisesRegex(ValueError,'10'):self.store.set_customer_tags(42,[str(i) for i in range(11)],'owner')
        with self.assertRaisesRegex(ValueError,'не найден'):self.store.set_customer_tags(999,['VIP'],'owner')
        self.store.remember_customer(43,'other','Олег','')
        page = self.store.customers_page(tag='vip')
        self.assertEqual([c['user_id'] for c in page['items']],[42])
        self.assertEqual(page['items'][0]['tags'],['VIP','опт клиент'])
        self.assertEqual(self.store.customers_page()['total'],2)
        self.assertEqual(self.store.customer_tags()[0],{'tag':'VIP','count':1})
        self.assertEqual(self.sql("SELECT count(*) FROM audit WHERE action='customer.tags'")[0][0],1)

    def test_note_and_ban_cycle_with_cached_ban_check(self):
        self.assertEqual(self.store.set_customer_note(42,' Просит счёт ','owner')['note'],'Просит счёт')
        with self.assertRaises(ValueError):self.store.set_customer_note(42,'x'*2001,'owner')
        self.assertFalse(self.store.is_banned(42))
        with self.assertRaises(ValueError):self.store.ban_customer(42,'  ','owner')
        meta = self.store.ban_customer(42,'Мошенничество','owner')
        self.assertTrue(meta['banned']);self.assertEqual((meta['ban_reason'],meta['banned_by']),('Мошенничество','owner'))
        self.assertTrue(self.store.is_banned(42))  # the local cache is dropped on ban
        self.assertEqual(self.store.customers_page(banned=True)['items'][0]['user_id'],42)
        self.assertTrue(self.store.customers_page()['items'][0]['banned'])
        # Another process (here: a second Store) sees the change within the cache lifetime.
        other = Store(Path(self.temp.name)/'test.db',self.key)
        import store as store_module
        with patch.object(store_module,'BAN_CACHE_SECONDS',0):
            store_module._BANNED.clear()
            self.assertTrue(other.is_banned(42))
            self.store.unban_customer(42,'owner')
            self.assertFalse(other.is_banned(42))
        levels = self.sql("SELECT action,level FROM audit WHERE action LIKE 'customer.%ban' ORDER BY id")
        self.assertEqual(levels,[('customer.ban','warning'),('customer.unban','warning')])
        self.assertEqual(self.store.customer_meta(42)['note'],'Просит счёт')

    # ------------------------------------------------------------ order notes & templates

    def test_order_notes(self):
        o = self.paid_order()
        self.assertEqual(self.store.order_notes(o['id']),[])
        note = self.store.add_order_note(o['id'],'  Перезвонить  ','anna')
        self.assertEqual((note['text'],note['author']),('Перезвонить','anna'))
        self.assertEqual([n['text'] for n in self.store.order_notes(o['id'])],['Перезвонить'])
        with self.assertRaises(ValueError):self.store.add_order_note(o['id'],'x'*1001,'anna')
        with self.assertRaises(ValueError):self.store.add_order_note(o['id'],'  ','anna')
        with self.assertRaisesRegex(ValueError,'не найден'):self.store.add_order_note('missing','text','anna')
        self.assertEqual(self.sql("SELECT count(*) FROM audit WHERE action='order.note' AND order_id=?",(o['id'],))[0][0],1)

    def test_support_templates_crud(self):
        t = self.store.save_support_template(' Оплата ','Здравствуйте, {name}! Заказ {order} оплачен.','anna')
        self.assertEqual(t['title'],'Оплата')
        t = self.store.save_support_template('Оплата прошла','Текст','anna',t['id'])
        self.assertEqual([x['title'] for x in self.store.support_templates()],['Оплата прошла'])
        with self.assertRaises(ValueError):self.store.save_support_template('x'*61,'Текст','anna')
        with self.assertRaises(ValueError):self.store.save_support_template('Тема','x'*3501,'anna')
        with self.assertRaisesRegex(ValueError,'не найден'):self.store.save_support_template('Тема','Текст','anna',999)
        self.store.delete_support_template(t['id'],'anna')
        self.assertEqual(self.store.support_templates(),[])
        with self.assertRaises(ValueError):self.store.delete_support_template(t['id'],'anna')

    # ------------------------------------------------------------ bulk products

    def test_bulk_actions(self):
        second = self.store.save_product(dict(category='Test',name='Second',description='',price=3,mode='manual',instruction='',
                                              active=True,old_price=4))
        ids = [self.pid,second]
        self.assertEqual(self.store.bulk_products(ids,'hide',None,'owner')['changed'],2)
        self.assertEqual([p['active'] for p in self.store.products()],[0,0])
        self.assertEqual(self.store.bulk_products(ids,'hide',None,'owner')['changed'],0)
        self.store.bulk_products(ids,'show',None,'owner')
        self.store.bulk_products(ids,'price_percent',10,'owner')
        prices = {p['id']:(p['price'],p['old_price']) for p in self.store.products()}
        self.assertEqual(prices[self.pid],(110,None))
        self.assertEqual(prices[second],(3,4))       # 3.3 rounds to 3: unchanged
        self.store.bulk_products(ids,'price_percent',50,'owner')
        prices = {p['id']:(p['price'],p['old_price']) for p in self.store.products()}
        self.assertEqual(prices[second],(5,None))    # 4.5 → 5 (half up); the stale "was" price is dropped
        self.store.bulk_products(ids,'price_percent',-90,'owner')
        self.assertEqual(sorted(p['price'] for p in self.store.products()),[1,17])  # never below 1
        for bad in (-91,501,0,True,'10'):
            with self.assertRaises(ValueError):self.store.bulk_products(ids,'price_percent',bad,'owner')
        self.store.bulk_products(ids,'category',' Новая ','owner')
        self.assertEqual({p['category'] for p in self.store.products()},{'Новая'})
        with self.assertRaisesRegex(ValueError,'не найден'):self.store.bulk_products([self.pid,999],'hide',None,'owner')
        self.assertEqual(self.store.products()[0]['active'],1)  # one transaction: nothing changed
        self.assertEqual(self.store.bulk_products(ids,'delete',None,'owner')['changed'],2)
        self.assertEqual(self.store.products(),[])
        with self.assertRaises(ValueError):self.store.bulk_products([],'hide',None,'owner')
        with self.assertRaises(ValueError):self.store.bulk_products(list(range(1,502)),'hide',None,'owner')
        with self.assertRaises(ValueError):self.store.bulk_products(ids,'explode',None,'owner')
        self.assertGreaterEqual(self.sql("SELECT count(*) FROM audit WHERE action='product.bulk'")[0][0],8)

    # ------------------------------------------------------------ live feed

    def test_live_feed_triggers_seen_and_low_stock(self):
        start = self.store.live_max_id()
        self.assertEqual([r['kind'] for r in self.store.live_recent(['customer.new'])],['customer.new'])
        self.store.remember_customer(42,'buyer2','Анна','Ким')  # upsert of a known customer: no new event
        self.assertEqual(self.store.live_max_id(),start)
        o = self.paid_order()  # the only key is sold: stock reaches the threshold
        kinds = [r['kind'] for r in self.store.live_since(start)]
        self.assertIn('payment.accepted',kinds);self.assertIn('stock.low',kinds)
        paid = next(r for r in self.store.live_since(start) if r['kind']=='payment.accepted')
        self.assertEqual((paid['order_id'],paid['user_id']),(o['id'],42))
        self.paid_order()
        self.assertEqual(sum(r['kind']=='stock.low' for r in self.store.live_since(0)),1)  # once per 6 h
        import support
        support.Support(self.store).open_or_append(42,'Помогите')
        self.assertEqual(self.store.live_recent(['support.message'])[0]['user_id'],42)
        last = self.store.live_max_id()
        self.assertEqual(self.store.live_unread(['payment.accepted'],0),2)
        self.assertEqual(self.store.set_live_seen('Owner',last+100),last)   # clamped to the newest event
        self.assertEqual(self.store.set_live_seen('owner',1),last)          # never moves back
        self.assertEqual(self.store.live_unread(['payment.accepted'],self.store.live_seen('OWNER')),0)
        self.assertEqual(self.store.customer_names([42,7]),{42:'Анна Ким'})
        with patch('store.time.time',return_value=time.time()+15*86400):
            with self.store.tx() as db:self.assertGreater(self.store._prune_live(db,time.time()+15*86400),0)
        self.assertEqual(self.store.live_since(0),[])

    def test_live_low_stock_failure_never_breaks_a_sale(self):
        with patch.object(Store,'_live_low_stock',side_effect=RuntimeError('boom')),self.assertLogs('themarket.store','ERROR'):
            o = self.paid_order()
        self.assertEqual(self.sql('SELECT status FROM orders WHERE id=?',(o['id'],))[0][0],'fulfilled')

    # ------------------------------------------------------------ insights

    def test_revenue_goal_and_insights_report(self):
        from insights import Insights
        self.assertEqual(self.store.revenue_goal(),0)
        self.store.set_revenue_goal(500,'owner')
        with self.assertRaises(ValueError):self.store.set_revenue_goal(-1,'owner')
        self.store.remember_customer(43,'','Олег','')
        for _ in range(2):self.paid_order(42)
        self.paid_order(43)
        report = Insights(self.store).report('week')
        self.assertEqual(report['heatmap']['total_orders'],3)
        self.assertEqual(report['heatmap']['total_revenue'],300)
        self.assertEqual(len(report['heatmap']['orders']),7);self.assertEqual(len(report['heatmap']['orders'][0]),24)
        steps = {s['key']:s['value'] for s in report['funnel']['steps']}
        self.assertEqual(steps,{'visitors':2,'ordered':2,'paid':2,'repeat':1})
        top = report['top_customers']
        self.assertEqual([(c['user_id'],c['revenue'],c['orders'],c['ltv']) for c in top],[(42,200,2,200),(43,100,1,100)])
        self.assertEqual(top[0]['name'],'Анна Ким')
        goal = report['goal']
        self.assertEqual((goal['goal'],goal['revenue'],goal['progress']),(500,300,60.0))
        self.assertGreaterEqual(goal['forecast'],300)
        cohort = report['cohorts']['rows'][-1]
        self.assertEqual(len(report['cohorts']['rows']),12)
        self.assertEqual((cohort['customers'],cohort['cells'][0]['share']),(2,100.0))
        self.assertEqual(report['cohorts']['rows'][0]['cells'][0]['share'],None)
        for name in ('heatmap','funnel','top_customers','goal','cohorts'):self.assertIn(name,report['timings'])
