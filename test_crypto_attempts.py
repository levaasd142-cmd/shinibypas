import concurrent.futures
import tempfile
import time
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
from cryptography.fernet import Fernet
from store import Store
from bep20 import VerifiedTransfer, WALLET


class CryptoAttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.key=Fernet.generate_key()
        self.store=Store(Path(self.temp.name)/'shop.db',self.key)
        self.store.set_currency('100','test')
        self.store.save_payment_method('bsc',True,WALLET,'','test')
        self.d=self.store.create_crypto_deposit(42,500)

    def tearDown(self):self.temp.cleanup()

    def proof(self):
        d=self.d
        return VerifiedTransfer('0x'+'a'*64,d['wallet'],d['token'],d['amount_raw'],d['created'],100,'block')

    def test_five_failures_cancel_persist_and_block_credit(self):
        for i in range(5):
            result=self.store.crypto_input_failure(self.d['id'],42,'ref-'+str(i),'Wrong TXID')
            self.assertEqual(result['failed_attempts'],i+1)
        self.assertTrue(result['cancelled'])
        reopened=Store(self.store.path,self.key)
        self.assertEqual(reopened.crypto_deposit(self.d['id'],42)['failed_attempts'],5)
        for operation in (
            lambda:reopened.credit_crypto(self.d['id'],42,self.proof()),
            lambda:reopened.queue_crypto_check(self.d['id'],'0x'+'a'*64),
            lambda:reopened.retry_crypto_check(self.d['id'],'owner')):
            with self.assertRaises(ValueError):operation()
        self.assertEqual(reopened.wallet(42)['balance'],0)
        self.assertNotEqual(reopened.create_crypto_deposit(42,500)['id'],self.d['id'])
        self.assertEqual(self.store.crypto_failure_notices()[0]['remaining'],0)

    def test_pending_checks_do_not_count_and_permanent_result_counts_once(self):
        txid='0x'+'a'*64
        ref=self.store.queue_crypto_check(self.d['id'],txid,'same-update')
        for _ in range(3):self.store.crypto_check_result(self.d['id'],txid,True,'Waiting',ref)
        self.assertEqual(self.store.crypto_deposit(self.d['id'],42)['failed_attempts'],0)
        for _ in range(3):self.store.crypto_check_result(self.d['id'],txid,False,'Wrong amount',ref)
        self.assertEqual(self.store.crypto_deposit(self.d['id'],42)['failed_attempts'],1)
        self.store.credit_crypto(self.d['id'],42,self.proof())
        self.assertEqual(self.store.wallet(42)['balance'],500)
        self.store.crypto_input_failure(self.d['id'],42,'after-paid','Wrong')
        self.assertEqual(self.store.crypto_deposit(self.d['id'],42)['failed_attempts'],1)

    def test_failures_are_owned_isolated_and_concurrency_safe(self):
        with self.assertRaises(ValueError):self.store.crypto_input_failure(self.d['id'],99,'wrong-owner','Wrong')
        def fail(index):return self.store.crypto_input_failure(self.d['id'],42,'attempt-'+str(index),'Wrong')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(fail,range(12)))
        self.assertEqual(self.store.crypto_deposit(self.d['id'],42)['failed_attempts'],5)
        self.assertEqual(sum(e['action']=='crypto.cancelled' for e in self.store.audits()),1)
        other=self.store.create_crypto_deposit(99,500)
        self.assertEqual(other['failed_attempts'],0)

    def test_stale_worker_error_cannot_charge_new_input(self):
        old='0x'+'a'*64;new='0x'+'b'*64
        self.store.queue_crypto_check(self.d['id'],old,'old')
        self.store.queue_crypto_check(self.d['id'],new,'new')
        self.store.crypto_check_result(self.d['id'],old,False,'Wrong','old')
        self.assertEqual(self.store.crypto_deposit(self.d['id'],42)['failed_attempts'],0)


class CryptoOverhaulTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.key=Fernet.generate_key()
        self.store=Store(Path(self.temp.name)/'shop.db',self.key)
        self.store.set_currency('100','test')
        self.store.save_payment_method('bsc',True,WALLET,'','test')

    def tearDown(self):self.temp.cleanup()

    def proof(self,d,txid='0x'+'a'*64,transfer_id=''):
        return VerifiedTransfer(txid,d['wallet'],d['token'],d['amount_raw'],d['created'],100,'block','bsc',transfer_id)

    def sql(self,query,params=()):
        with self.store.read() as db:return [tuple(r) for r in db.execute(query,params)]

    def test_amount_tails_are_reused_only_long_after_expiry(self):
        made=[self.store.create_crypto_deposit(1000+user,500) for user in range(99)]
        self.assertEqual(len({d['amount_raw'] for d in made}),99)
        with self.assertRaises(ValueError):self.store.create_crypto_deposit(5000,500)
        with self.store.tx() as db:db.execute('UPDATE crypto_deposits SET expires=? WHERE id=?',(int(time.time())-47*3600,made[0]['id']))
        with self.assertRaises(ValueError):self.store.create_crypto_deposit(5000,500)
        with self.store.tx() as db:db.execute('UPDATE crypto_deposits SET expires=? WHERE id=?',(int(time.time())-49*3600,made[0]['id']))
        reused=self.store.create_crypto_deposit(5000,500)
        self.assertEqual(reused['amount_raw'],made[0]['amount_raw'])
        # The old invoice's time window still protects against a transfer meant for the new one.
        with self.assertRaises(ValueError):self.store.credit_crypto(made[0]['id'],1000,self.proof(reused))
        self.assertTrue(self.store.credit_crypto(reused['id'],5000,self.proof(reused)))

    def test_one_transaction_can_pay_several_invoices(self):
        a=self.store.create_crypto_deposit(42,500);b=self.store.create_crypto_deposit(42,700)
        tx='0x'+'c'*64
        self.assertTrue(self.store.credit_crypto(a['id'],42,self.proof(a,tx,tx+':1')))
        self.assertTrue(self.store.credit_crypto(b['id'],42,self.proof(b,tx,tx+':2')))
        self.assertEqual(self.store.wallet(42)['balance'],1200)
        self.assertEqual({h['kind'] for h in self.store.wallet(42)['history']},{'usdt'})
        c=self.store.create_crypto_deposit(42,900)
        with self.assertRaises(ValueError):self.store.credit_crypto(c['id'],42,self.proof(c,tx,tx+':2'))
        legacy='0x'+'d'*64
        d=self.store.create_crypto_deposit(43,500);self.store.credit_crypto(d['id'],43,self.proof(d,legacy))
        e=self.store.create_crypto_deposit(43,700)
        with self.assertRaises(ValueError):self.store.credit_crypto(e['id'],43,self.proof(e,legacy,legacy+':5'))

    def test_legacy_unique_constraints_are_removed_by_migration(self):
        import sqlite3
        from contextlib import closing
        d=self.store.create_crypto_deposit(42,500)
        self.store.queue_crypto_check(d['id'],'0x'+'a'*64)
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute('PRAGMA foreign_keys=OFF');db.execute('PRAGMA legacy_alter_table=ON')
            columns=','.join(r[1] for r in db.execute('PRAGMA table_info(crypto_deposits)') if r[1]!='amount_active')
            db.execute('ALTER TABLE crypto_deposits RENAME TO old_deposits')
            db.execute('''CREATE TABLE crypto_deposits(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,wallet TEXT NOT NULL,token TEXT NOT NULL,
              amount_raw TEXT NOT NULL,credit_cents INTEGER NOT NULL CHECK(credit_cents>0),created INTEGER NOT NULL,expires INTEGER NOT NULL,
              txid TEXT UNIQUE,block_number INTEGER,block_hash TEXT,network TEXT NOT NULL DEFAULT 'bsc',failed_attempts INTEGER NOT NULL DEFAULT 0,
              cancelled REAL,order_id TEXT,applied_order INTEGER,UNIQUE(wallet,token,amount_raw))''')
            db.execute('INSERT INTO crypto_deposits('+columns+') SELECT '+columns+' FROM old_deposits')
            db.execute('DROP TABLE old_deposits');db.execute('DELETE FROM payment_methods')
            db.execute('PRAGMA user_version=0');db.commit()
        reopened=Store(self.store.path,self.key)
        self.assertEqual(self.sql("SELECT count(*) FROM pragma_index_list('crypto_deposits') WHERE origin='u'")[0][0],0)
        self.assertEqual(reopened.crypto_deposit(d['id'],42)['amount_raw'],d['amount_raw'])
        self.assertEqual(self.sql('SELECT count(*) FROM crypto_checks')[0][0],1)
        self.assertEqual(self.sql('PRAGMA foreign_key_check'),[])
        # A shop that already took BEP20 payments keeps the receiving wallet it used.
        bsc=next(m for m in reopened.payment_methods()['networks'] if m['network']=='bsc')
        self.assertTrue(bsc['enabled']);self.assertEqual(bsc['wallet'].lower(),WALLET.lower())
        Store(self.store.path,self.key)  # idempotent

    def test_new_shop_has_no_default_bsc_wallet(self):
        with patch.dict('os.environ',{}) as env:
            env.pop('BSC_WALLET',None)
            fresh=Store(Path(self.temp.name)/'fresh.db',self.key)
            bsc=next(m for m in fresh.payment_methods()['networks'] if m['network']=='bsc')
        self.assertEqual((bsc['enabled'],bsc['wallet']),(False,''))

    def test_order_invoice_extends_reservation_and_skips_approved_checkout(self):
        pid=self.store.save_product(dict(category='T',name='P',price=500,mode='manual'))
        order=self.store.create_order(pid,42)
        d=self.store.create_crypto_deposit(42,500,order_id=order['id'])
        self.assertEqual(self.sql('SELECT expires FROM orders')[0][0],d['expires'])
        self.assertEqual(self.store.create_crypto_deposit(42,500,order_id=order['id'])['id'],d['id'])
        self.store.approve(order['id'],42,order['payment_stars'],'XTR')
        with self.assertRaises(ValueError):self.store.create_crypto_deposit(42,500,order_id=order['id'])
        self.store.crypto_context(42,d['id'])
        self.store.credit_crypto(d['id'],42,self.proof(d))
        self.assertIsNone(self.store.crypto_deposit(d['id'],42)['applied_order'])
        self.assertEqual(self.store.wallet(42)['balance'],500)  # money kept on the balance
        self.assertIsNone(self.store.crypto_context(42))
        self.store.paid(order['id'],42,order['payment_stars'],'XTR','stars')
        self.assertEqual(self.store.wallet(42)['balance'],500)

    def test_crypto_context_is_cleared_on_cancel_and_explicitly(self):
        d=self.store.create_crypto_deposit(42,500)
        self.assertEqual(self.store.crypto_context(42,d['id']),d['id'])
        self.store.clear_crypto_context(42)
        self.assertIsNone(self.store.crypto_context(42))
        self.store.crypto_context(42,d['id'])
        for i in range(5):self.store.crypto_input_failure(d['id'],42,'r'+str(i),'Wrong')
        self.assertIsNone(self.store.crypto_context(42))

    def test_notification_backoff_and_give_up(self):
        d=self.store.create_crypto_deposit(42,500)
        self.store.queue_crypto_check(d['id'],'0x'+'a'*64)
        self.store.credit_crypto(d['id'],42,self.proof(d))
        with self.store.tx() as db:db.execute('UPDATE crypto_checks SET next_try=0')
        self.assertEqual(len(self.store.crypto_notifications_due()),1)
        self.store.crypto_notification(d['id'],False)
        self.assertEqual(self.store.crypto_notifications_due(),[])
        self.assertAlmostEqual(self.sql('SELECT next_try FROM crypto_checks')[0][0]-time.time(),60,delta=5)
        self.store.crypto_notification(d['id'],False,permanent=True)
        self.assertEqual(self.sql('SELECT status FROM crypto_checks')[0][0],'notify_failed')
        with self.store.tx() as db:db.execute('UPDATE crypto_checks SET next_try=0')
        self.assertEqual(self.store.crypto_notifications_due(),[])
        self.assertEqual(sum(e['action']=='crypto.notify_failed' for e in self.store.audits()),1)

    def test_failure_notice_backoff_and_give_up(self):
        d=self.store.create_crypto_deposit(42,500)
        self.store.crypto_input_failure(d['id'],42,'ref','Wrong')
        self.store.crypto_failure_notice_result('ref',False)
        self.assertEqual(self.store.crypto_failure_notices(),[])
        for _ in range(28):
            with self.store.tx() as db:db.execute('UPDATE crypto_invalid_attempts SET next_try=0')
            self.assertEqual(len(self.store.crypto_failure_notices()),1)
            self.store.crypto_failure_notice_result('ref',False)
        with self.store.tx() as db:db.execute('UPDATE crypto_invalid_attempts SET next_try=0')
        self.assertEqual(self.store.crypto_failure_notices(),[])
        self.assertEqual(sum(e['action']=='crypto.notice_failed' for e in self.store.audits()),1)

    def test_review_event_names_the_network(self):
        self.store.save_payment_method('bsc',False,'','','test')
        self.store.save_payment_method('tron',True,'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t','','test')
        d=self.store.create_crypto_deposit(42,500,network='tron')
        txid='a'*64;ref=self.store.queue_crypto_check(d['id'],txid)
        with self.store.tx() as db:db.execute('UPDATE crypto_checks SET started=0')
        self.store.crypto_check_result(d['id'],txid,True,'Waiting',ref)
        self.assertEqual(next(e for e in self.store.audits() if e['action']=='crypto.review')['actor'],'tron')
