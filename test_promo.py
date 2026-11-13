from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

import promo
from promo import Promos
from store import Store


def open_store(directory):
    """Store with every 3.0 feature table (Store.__init__ calls these after the DATA merge)."""
    store = Store(Path(directory)/'shop.sqlite3',Fernet.generate_key())
    import notify, staff, support
    with store.tx() as db:
        for module in (promo,support,staff,notify):module.initialize(db)
    return store


class PromoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = open_store(self.temp.name)
        self.promos = Promos(self.store)

    def tearDown(self):self.temp.cleanup()

    def code(self, **changes):
        data = dict(code='gift5',amount_cents=500)
        data.update(changes)
        return self.promos.create(data,'owner')

    def sql(self, query, params=()):
        with self.store.read() as db:
            return [tuple(row) for row in db.execute(query,params)]

    def test_initialize_is_idempotent(self):
        with self.store.tx() as db:
            promo.initialize(db);promo.initialize(db)

    def test_create_normalizes_and_validates(self):
        item = self.code(code='  spring_2026-a ')
        self.assertEqual(item['code'],'SPRING_2026-A')
        self.assertEqual((item['amount_cents'],item['used'],item['credited_cents'],item['users'],item['per_user'],item['max_uses']),
                         (500,0,0,0,1,0))
        self.assertTrue(item['active']);self.assertIsNone(item['legacy_percent'])
        for key in ('kind','value','min_order_cents','product_ids'):self.assertNotIn(key,item)
        bad = [dict(code='ab'),dict(code='bad code'),dict(code='КОД123'),dict(code='X'*33),
               dict(amount_cents=0),dict(amount_cents=-1),dict(amount_cents=1.5),dict(amount_cents=promo.MAX_AMOUNT_CENTS+1),
               dict(amount_cents=None),dict(max_uses=-1),dict(per_user=0),dict(starts_at=2000000000,ends_at=1900000000),
               dict(starts_at='soon'),dict(note='x'*201)]
        for index, changes in enumerate(bad):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.code(**{'code':'BAD'+str(index),**changes})
        with self.assertRaisesRegex(ValueError,'уже существует'):self.code(code='Spring_2026-A')
        # 3.0 scripts passed the amount as 'value': still accepted, kind is ignored.
        self.assertEqual(self.promos.create(dict(code='OLDAPI',kind='percent',value=10),'owner')['amount_cents'],10)

    def test_redeem_credits_the_wallet_once_per_customer(self):
        item = self.code(code='WELCOME')
        result = self.promos.redeem_to_balance(' welcome ',42)
        self.assertEqual((result['code'],result['amount_cents'],result['balance'],result['repeated']),('WELCOME',500,500,False))
        wallet = self.store.wallet(42)
        self.assertEqual(wallet['balance'],500)
        self.assertEqual((wallet['history'][0]['reference'],wallet['history'][0]['kind']),
                         ('promo:%d:42:1' % item['id'],'promo'))
        with self.assertRaisesRegex(ValueError,promo.ALREADY_USED):self.promos.redeem_to_balance('WELCOME',42)
        self.assertEqual(self.store.wallet(42)['balance'],500)
        event = self.sql("SELECT category,user_id,summary FROM audit WHERE action LIKE 'promo.redeemed%'")
        self.assertEqual(event,[('balance',42,'Активирован промокод · сумма зачислена на баланс')])
        stats = self.promos.get(item['id'])
        self.assertEqual((stats['used'],stats['credited_cents'],stats['users']),(1,500,1))
        self.assertEqual(self.store.verify_wallet_balances(),[])

    def test_per_user_limit_allows_several_uses(self):
        item = self.code(per_user=3,amount_cents=100)
        for n in range(3):self.assertEqual(self.promos.redeem_to_balance('GIFT5',42)['balance'],100*(n+1))
        with self.assertRaisesRegex(ValueError,promo.ALREADY_USED):self.promos.redeem_to_balance('GIFT5',42)
        self.assertEqual(self.sql('SELECT n FROM promo_credits WHERE promo_id=? ORDER BY n',(item['id'],)),[(1,),(2,),(3,)])

    def test_customer_errors(self):
        now = time.time()
        self.code(code='LATER',starts_at=now+3600)
        self.code(code='OLD',starts_at=now-7200,ends_at=now-3600)
        off = self.code(code='OFF')
        self.promos.set_active(off['id'],False,'owner')
        self.code(code='ONE',max_uses=1)
        self.promos.redeem_to_balance('ONE',1)
        cases = [('NOPE',promo.NOT_FOUND),('x',promo.NOT_FOUND),('',promo.NOT_FOUND),('OFF',promo.NOT_FOUND),
                 ('LATER',promo.NOT_STARTED),('OLD',promo.EXPIRED),('ONE',promo.EXHAUSTED)]
        for index, (code, message) in enumerate(cases):
            with self.subTest(code=code),self.assertRaisesRegex(ValueError,message):
                self.promos.redeem_to_balance(code,100+index)  # separate customers: no lockout
        with self.assertRaisesRegex(ValueError,promo.NOT_FOUND):self.promos.redeem_to_balance('GIFT5',0)
        self.assertEqual(self.store.wallet(100)['balance'],0)
        for message in promo.CUSTOMER_ERRORS:self.assertTrue(message)

    def test_wrong_codes_are_rate_limited_per_customer(self):
        self.code(code='REAL')
        for index in range(promo.FAIL_LIMIT):
            with self.assertRaisesRegex(ValueError,promo.NOT_FOUND):self.promos.redeem_to_balance('GUESS%d' % index,42)
        self.assertEqual(self.promos.failures(42),promo.FAIL_LIMIT)
        # Even the right code is refused while locked out, and the lockout is not extended.
        with self.assertRaisesRegex(ValueError,promo.TOO_MANY):self.promos.redeem_to_balance('REAL',42)
        self.assertEqual(self.promos.failures(42),promo.FAIL_LIMIT)
        self.assertEqual(self.store.wallet(42)['balance'],0)
        self.assertEqual(self.promos.redeem_to_balance('REAL',43)['amount_cents'],500)  # another customer is not affected
        later = time.time()+promo.FAIL_WINDOW+1
        with patch('promo.time.time',return_value=later):
            self.assertEqual(self.promos.redeem_to_balance('REAL',42)['balance'],500)
        self.assertEqual(self.sql('SELECT count(*) FROM promo_failures WHERE user_id=42'),[(0,)])  # old failures pruned

    def test_repeated_message_is_idempotent(self):
        self.code(code='TWICE',per_user=2)
        first = self.promos.redeem_to_balance('TWICE',42,attempt='telegram:42:1001')
        again = self.promos.redeem_to_balance('TWICE',42,attempt='telegram:42:1001')
        self.assertEqual((again['amount_cents'],again['balance'],again['repeated']),(500,500,True))
        self.assertFalse(first['repeated'])
        self.assertEqual(self.store.wallet(42)['balance'],500)
        self.assertEqual(self.promos.redeem_to_balance('TWICE',42,attempt='telegram:42:1002')['balance'],1000)
        self.code(code='OTHER')  # the same attempt id for another code is a separate request
        self.assertEqual(self.promos.redeem_to_balance('OTHER',42,attempt='telegram:42:1002')['balance'],1500)

    def test_two_customers_race_for_the_last_use(self):
        item = self.code(code='LAST',max_uses=1)
        barrier = threading.Barrier(8)

        def attempt(user_id):
            barrier.wait()
            try:return self.promos.redeem_to_balance('LAST',user_id)
            except ValueError as exc:return str(exc)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt,range(1,9)))
        winners = [r for r in results if isinstance(r,dict)]
        self.assertEqual(len(winners),1)
        self.assertEqual([r for r in results if not isinstance(r,dict)],[promo.EXHAUSTED]*7)
        self.assertEqual(self.promos.get(item['id'])['used'],1)
        self.assertEqual(self.sql("SELECT count(*),sum(delta) FROM wallet_ledger WHERE reference LIKE 'promo:%'"),[(1,500)])

    def test_same_customer_racing_gets_one_credit(self):
        self.code(code='DOUBLE')
        barrier = threading.Barrier(4)

        def attempt(_):
            barrier.wait()
            try:return self.promos.redeem_to_balance('DOUBLE',42)
            except ValueError as exc:return str(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt,range(4)))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertEqual(self.store.wallet(42)['balance'],500)

    def test_list_stats_pagination_update_and_redemptions(self):
        for index in range(5):self.code(code='CODE%d' % index,amount_cents=100*(index+1))
        page = self.promos.list(page=2,page_size=2)
        self.assertEqual((page['total'],page['pages'],page['page'],[p['code'] for p in page['items']]),(5,3,2,['CODE2','CODE1']))
        self.assertEqual(self.promos.list(query='code4')['items'][0]['code'],'CODE4')
        self.store.remember_customer(42,'buyer','Ann','Lee')
        self.promos.redeem_to_balance('CODE4',42);self.promos.redeem_to_balance('CODE0',43)
        listing = self.promos.list()
        self.assertEqual(listing['totals'],{'uses':2,'credited_cents':600,'users':2})
        code_id = [p for p in listing['items'] if p['code']=='CODE4'][0]['id']
        used = self.promos.redemptions(code_id)
        self.assertEqual((used['code'],used['total']),('CODE4',1))
        self.assertEqual({k:used['items'][0][k] for k in ('kind','user_id','amount_cents','name')},
                         {'kind':'credit','user_id':42,'amount_cents':500,'name':'Ann Lee'})
        updated = self.promos.update(code_id,{'amount_cents':250,'max_uses':3},'owner')
        self.assertEqual((updated['amount_cents'],updated['max_uses'],updated['used']),(250,3,1))
        self.assertEqual(self.promos.redeem_to_balance('CODE4',44)['amount_cents'],250)
        self.assertFalse(self.promos.set_active(code_id,False,'owner')['active'])
        self.assertNotIn(code_id,[p['id'] for p in self.promos.list(include_inactive=False)['items']])
        with self.assertRaisesRegex(ValueError,promo.NOT_FOUND):self.promos.get(999)
        with self.assertRaises(ValueError):self.promos.list(page_size=0)

    def test_legacy_codes_are_migrated(self):
        """3.1 database: a 'fixed' discount code keeps working for its amount, a 'percent' code
        is switched off with a note; old discount uses still count against the limits."""
        path = Path(self.temp.name)/'legacy.sqlite3'
        key = Fernet.generate_key()
        store = Store(path,key)
        pid = store.save_product(dict(category='A',name='Key',description='',price=1000,mode='manual',instruction='',active=True))
        oid = store.create_order(pid,42)['id']
        with store.tx() as db:
            db.execute("DELETE FROM meta WHERE key=?",(promo.MIGRATION_KEY,))
            now = time.time()
            for code, kind, value in (('FIX3','fixed',300),('PCT10','percent',10)):
                db.execute('''INSERT INTO promo_codes(code,kind,value,min_order_cents,max_uses,per_user,product_ids,note,created,updated)
                    VALUES(?,?,?,500,2,1,'[1]','канал',?,?)''',(code,kind,value,now,now))
            fixed_id = db.execute("SELECT id FROM promo_codes WHERE code='FIX3'").fetchone()[0]
            db.execute('INSERT INTO promo_redemptions(promo_id,user_id,order_id,discount_cents,created) VALUES(?,?,?,?,?)',
                       (fixed_id,42,oid,300,now))
            db.execute("INSERT INTO user_state(user_id,key,value,updated) VALUES(42,'promo','PCT10',?)",(now,))
        reopened = Store(path,key)
        promos = Promos(reopened)
        items = {p['code']:p for p in promos.list()['items']}
        self.assertTrue(items['FIX3']['active'])
        self.assertEqual((items['FIX3']['amount_cents'],items['FIX3']['used'],items['FIX3']['discount_cents']),(300,1,300))
        self.assertFalse(items['PCT10']['active'])
        self.assertEqual((items['PCT10']['amount_cents'],items['PCT10']['legacy_percent']),(None,10))
        self.assertTrue(items['PCT10']['note'].startswith('[3.2]'));self.assertTrue(items['PCT10']['note'].endswith('канал'))
        self.assertIsNone(reopened.user_state(42,'promo'))
        with self.assertRaisesRegex(ValueError,promo.ALREADY_USED):promos.redeem_to_balance('FIX3',42)
        self.assertEqual(promos.redeem_to_balance('fix3',43)['amount_cents'],300)  # min_order and products ignored
        with self.assertRaisesRegex(ValueError,promo.EXHAUSTED):promos.redeem_to_balance('FIX3',44)
        with self.assertRaisesRegex(ValueError,promo.NOT_FOUND):promos.redeem_to_balance('PCT10',45)
        with self.assertRaisesRegex(ValueError,'процентные коды'):promos.set_active(items['PCT10']['id'],True,'owner')
        history = promos.redemptions(items['FIX3']['id'])['items']
        self.assertEqual([(r['kind'],r['user_id']) for r in history],[('credit',43),('discount',42)])
        # Editing a percent code with an amount turns it into a balance code.
        converted = promos.update(items['PCT10']['id'],{'amount_cents':150,'active':True},'owner')
        self.assertEqual((converted['amount_cents'],converted['active'],converted['legacy_percent']),(150,True,None))
        # The migration runs once: a later restart keeps codes the owner switched back on.
        Store(path,key)
        self.assertTrue(promos.get(items['PCT10']['id'])['active'])
        with closing_db(path) as db:
            self.assertEqual(db.execute("SELECT min_order_cents,product_ids FROM promo_codes WHERE code='PCT10'").fetchone(),(0,'[]'))

    def test_convert_to_usd(self):
        item = self.code(code='STARS',amount_cents=100)
        self.promos.redeem_to_balance('STARS',42)
        self.store.set_currency(50,'owner')  # 50 Stars per dollar: 100 Stars → $2
        self.assertEqual(self.promos.get(item['id'])['amount_cents'],200)
        self.assertEqual(self.promos.get(item['id'])['credited_cents'],200)
        self.assertEqual(self.store.wallet(42)['balance'],200)


def closing_db(path):
    from contextlib import closing
    return closing(sqlite3.connect(path))


if __name__=='__main__':unittest.main()
