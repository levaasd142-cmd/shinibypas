from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from pathlib import Path
import tempfile
import unittest
import uuid

from cryptography.fernet import Fernet
from analytics import Analytics,MSK,date_range
from store import Store


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.key=Fernet.generate_key()
        self.store=Store(Path(self.temp.name)/'shop.db',self.key)
        self.store.set_currency('100','test')
        self.analytics=Analytics(self.store)
        self.day=datetime.now(MSK).date().isoformat()
        self.product=dict(category='Test',name='License',description='',price=1000,mode='auto',instruction='',active=True,cost_price=300)
        self.pid=self.store.save_product(self.product)
        self.store.adjust_wallet(42,100000,'Test credit','credit','test')

    def tearDown(self):self.temp.cleanup()

    def sale(self,quantity=1):
        order=self.store.create_order(self.pid,42,quantity)
        self.store.pay_wallet(order['id'],42)
        return order['id']

    def report(self):return self.analytics.report('day')

    def entry(self,kind='expense',amount=100,category='server',order_id=None,reference=None,day=None):
        return self.analytics.add_entry(reference or str(uuid.uuid4()),kind,category,amount,'Test entry',day or self.day,order_id,'test')

    def test_batch_costs_frozen_at_sale_and_default_updates_do_not_rewrite_history(self):
        self.store.import_stock(self.pid,'first')
        self.store.import_stock(self.pid,'second',unit_cost=400)
        oid=self.sale(2)
        self.store.save_product({**self.product,'cost_price':999},product_id=self.pid)
        self.store.import_stock(self.pid,'third')
        self.store.paid(oid,42,2000,'XTR','wallet:'+oid,wallet=True)
        summary=self.report()['summary']
        self.assertEqual((summary['revenue'],summary['cost'],summary['profit'],summary['units']),(2000,700,1300,2))
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT unit_cost FROM stock WHERE order_id IS NULL').fetchone()[0],999)

    def test_expenses_refunds_replacements_and_voids(self):
        self.store.import_stock(self.pid,'first')
        oid=self.sale()
        self.entry(amount=100)
        refund=self.entry('refund',200,'refund',oid)
        self.entry('replacement',50,'replacement',oid)
        before=self.store.wallet(42)['balance']
        summary=self.report()['summary']
        self.assertEqual((summary['revenue'],summary['cost'],summary['expenses'],summary['refunds'],summary['profit']),(1000,300,150,200,350))
        self.analytics.void_entry(refund['id'],'Correction','test')
        self.analytics.void_entry(refund['id'],'Repeated request','test')
        self.assertEqual(self.report()['summary']['profit'],550)
        self.assertEqual(self.store.wallet(42)['balance'],before)
        self.assertEqual(self.report()['top_profit'][0]['profit'],650)
        self.assertTrue(self.store.events(category='finance')['total'])

    def test_unknown_cost_is_not_treated_as_zero_and_can_be_corrected(self):
        self.store.save_product({**self.product,'cost_price':None},product_id=self.pid)
        self.store.import_stock(self.pid,'first')
        self.store.save_product({**self.product,'cost_price':500},product_id=self.pid)
        oid=self.sale()
        summary=self.report()['summary']
        self.assertIsNone(summary['profit']);self.assertIsNone(summary['margin'])
        self.assertEqual(summary['unknown_cost_orders'],1)
        self.analytics.set_order_cost(oid,200,'Legacy invoice found','test')
        self.assertEqual(self.report()['summary']['profit'],800)
        self.assertEqual(self.store.order_info(oid,'test')['cost_total'],200)

    def test_explicit_zero_cost_is_known_and_duplicate_import_does_not_change_cost(self):
        self.store.import_stock(self.pid,'first',unit_cost=0)
        self.store.import_stock(self.pid,'first',unit_cost=900)
        self.sale()
        s=self.report()['summary']
        self.assertEqual((s['cost'],s['profit'],s['unknown_cost_orders']),(0,1000,0))

    def test_bulk_stock_cost_only_changes_free_positions(self):
        self.store.import_stock(self.pid,'sold\nreserved\nfree')
        sold=self.sale();held=self.store.create_order(self.pid,99)
        self.assertEqual(self.store.set_stock_cost(self.pid,900,'test')['updated'],1)
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT unit_cost FROM stock WHERE order_id=?',(sold,)).fetchone()[0],300)
            self.assertEqual(db.execute('SELECT unit_cost FROM stock WHERE order_id=?',(held['id'],)).fetchone()[0],300)
            self.assertEqual(db.execute('SELECT unit_cost FROM stock WHERE order_id IS NULL').fetchone()[0],900)

    def test_manual_service_cost_frozen_on_payment_revenue_on_completion(self):
        pid=self.store.save_product({**self.product,'name':'Service','mode':'manual','cost_price':250})
        order=self.store.create_order(pid,42,2)
        self.store.pay_wallet(order['id'],42)
        self.assertEqual(self.report()['summary']['revenue'],0)
        self.store.save_product({**self.product,'name':'Service','mode':'manual','cost_price':900},product_id=pid)
        self.store.details(order['id'],42,'Details');self.store.claim(order['id'],'test')
        self.store.complete(order['id'],'Done','test')
        self.assertEqual((self.report()['summary']['cost'],self.report()['summary']['profit']),(500,1500))

    def test_wallet_deposits_pending_and_failed_orders_are_not_revenue(self):
        tid=self.store.create_topup(42,500)
        self.store.credit_topup(tid,42,500,'XTR','topup-charge')
        self.store.import_stock(self.pid,'first')
        self.store.create_order(self.pid,42)
        summary=self.report()['summary']
        self.assertEqual(summary['revenue'],0)
        self.assertEqual(summary['wallet_deposits'],500)
        self.assertEqual(summary['orders'],0)

    def test_promo_credits_are_not_revenue(self):
        import promo
        promos=promo.Promos(self.store)
        promos.create(dict(code='GIFT',amount_cents=1500,per_user=1),'owner')
        promos.redeem_to_balance('GIFT',43)
        self.store.import_stock(self.pid,'k1')
        order=self.store.create_order(self.pid,43);self.store.pay_wallet(order['id'],43)  # paid with the promo money
        summary=self.report()['summary']
        self.assertEqual(summary['promo_credits'],1500)
        self.assertEqual(summary['wallet_deposits'],0)  # the 'credit' adjustment in setUp is not a deposit either
        self.assertEqual((summary['revenue'],summary['orders']),(1000,1))  # the purchase is a sale, the credit is not
        from insights import Insights
        with self.store.read() as db:self.assertEqual(Insights(self.store).goal(db)['revenue'],1000)

    def test_periods_use_moscow_midnight_and_end_is_exclusive(self):
        today=datetime.now(MSK).date()
        after=datetime.combine(today,datetime.min.time(),MSK).timestamp()
        self.store.import_stock(self.pid,'one\ntwo\nthree')
        ids=[self.sale() for _ in range(3)]
        with self.store.tx() as db:
            for oid,stamp in zip(ids,[after-1,after,after+86400]):db.execute('UPDATE orders SET fulfilled_at=? WHERE id=?',(stamp,oid))
        summary=self.report()['summary']
        self.assertEqual(summary['orders'],1)
        self.assertEqual(self.report()['sales']['items'][0]['id'],ids[1])
        first,last,_,_=date_range('week')
        self.assertEqual((last-first).days,6)
        for args in [('custom','2020-01-01','2022-01-01'),('custom','bad',self.day),('custom','9999-12-31','9999-12-31')]:
            with self.assertRaises(ValueError):date_range(*args)

    def test_refund_cap_and_idempotency_under_concurrency(self):
        self.store.import_stock(self.pid,'one');oid=self.sale()
        reference=str(uuid.uuid4())
        self.entry('refund',600,'refund',oid,reference)
        self.entry('refund',600,'refund',oid,reference)
        with self.assertRaises(ValueError):self.entry('refund',601,'refund',oid,reference)
        def refund(_):
            try:self.entry('refund',300,'refund',oid);return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(refund,range(4)))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.report()['summary']['refunds'],900)

    def test_invalid_operations_are_rejected(self):
        for kind,amount,category in [('expense',-1,'server'),('expense',0,'server'),('refund',100,'server'),('expense',100,'refund')]:
            with self.assertRaises(ValueError):self.entry(kind,amount,category)
        with self.assertRaises(ValueError):self.entry('refund',100,'refund','missing')
        tomorrow=(datetime.now(MSK).date()+timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):self.entry(day=tomorrow)
        with self.assertRaises(ValueError):self.analytics.set_order_cost('missing',100,'Reason','test')
        with self.assertRaises(ValueError):self.analytics.void_entry('missing','Reason','test')

    def test_legacy_dates_are_marked_and_deleted_products_remain_in_report(self):
        self.store.import_stock(self.pid,'one');oid=self.sale()
        with self.store.tx() as db:db.execute('UPDATE orders SET fulfilled_at=NULL WHERE id=?',(oid,))
        self.store.delete_product(self.pid)
        report=self.report()
        self.assertEqual(report['summary']['estimated_dates'],1)
        self.assertEqual(report['top_revenue'][0]['title'],'License')

    def test_pagination_and_migrations_preserve_historical_unknown_costs(self):
        self.store.import_stock(self.pid,'\n'.join('item-'+str(i) for i in range(27)))
        for _ in range(27):self.sale()
        self.assertEqual(len(self.report()['sales']['items']),25)
        self.assertEqual(len(self.analytics.report('day',page=2)['sales']['items']),2)
        with self.store.tx() as db:db.execute('UPDATE orders SET cost_total=NULL,cost_locked=0')
        reopened=Store(self.store.path,self.key)
        self.assertEqual(Analytics(reopened).report('day')['summary']['unknown_cost_orders'],27)

    def test_old_schema_recovers_completion_dates_but_keeps_cost_unknown(self):
        self.store.import_stock(self.pid,'legacy-stock');oid=self.sale()
        balance=self.store.wallet(42)['balance']
        with self.store.tx() as db:
            for index in ('orders_fulfilled','orders_paid_at','orders_sold'):db.execute('DROP INDEX '+index)
            db.execute('UPDATE orders SET created=1 WHERE id=?',(oid,))
            for column in ('cost_total','cost_locked','paid_at','fulfilled_at'):
                db.execute('ALTER TABLE orders DROP COLUMN '+column)
            db.execute('ALTER TABLE products DROP COLUMN cost_price')
            db.execute('ALTER TABLE stock DROP COLUMN unit_cost')
        reopened=Store(self.store.path,self.key)
        report=Analytics(reopened).report('day')
        self.assertEqual(report['summary']['orders'],1)
        self.assertEqual(report['summary']['unknown_cost_orders'],1)
        self.assertIsNone(report['summary']['profit'])
        self.assertEqual(report['summary']['estimated_dates'],0)
        self.assertEqual(reopened.wallet(42)['balance'],balance)
        self.assertIn('legacy-stock',reopened.purchase_delivery(42,oid))


    def test_deposit_networks_follow_crypto_registry(self):
        import time
        from unittest.mock import patch
        import crypto_networks
        from analytics import deposit_prefixes
        self.assertEqual(set(deposit_prefixes()),{'topup:',*(name+':' for name in crypto_networks.NETWORKS)})
        with self.store.tx() as db:
            for reference,delta in (('ton:dep',100),('newnet:dep',200),('adjust:x',400),('tonnage:x',800)):
                db.execute('INSERT INTO wallet_ledger(user_id,delta,reference,created) VALUES(42,?,?,?)',(delta,reference,time.time()))
        self.assertEqual(self.report()['summary']['wallet_deposits'],100)
        with patch.dict(crypto_networks.NETWORKS,{'newnet':{}}):
            self.assertEqual(self.report()['summary']['wallet_deposits'],300)

    def test_moscow_timezone_and_sql_daily_buckets(self):
        self.assertEqual(datetime(2026,1,1,tzinfo=MSK).utcoffset(),timedelta(hours=3))
        self.store.import_stock(self.pid,'a\nb\nc')
        for _ in range(3):self.sale()
        report=self.analytics.report('week')
        self.assertEqual(sum(day['orders'] for day in report['days']),3)
        self.assertEqual(report['days'][-1]['revenue'],3000)
        self.assertEqual(report['sales']['total'],3);self.assertEqual(len(report['sales']['items']),3)

    def test_dashboard_daily_sales_use_moscow_days_and_paid_at_index(self):
        now=datetime(2026,3,10,12,0,tzinfo=MSK).timestamp()
        midnight=datetime(2026,3,10,tzinfo=MSK).timestamp()
        self.store.import_stock(self.pid,'\n'.join('k%d'%i for i in range(7)))
        ids=[self.sale() for _ in range(6)]
        unpaid=self.store.create_order(self.pid,43)['id']
        stamps=[midnight,midnight-1,midnight-29*86400,midnight-30*86400,midnight-60*86400,midnight+86400]
        with self.store.tx() as db:
            for oid,stamp in zip(ids,stamps):db.execute('UPDATE orders SET paid_at=?,created=? WHERE id=?',(stamp,stamp-5,oid))
            db.execute('UPDATE orders SET paid_at=NULL,created=? WHERE id=?',(midnight+60,ids[-1]))  # legacy row: created counts
            db.execute('UPDATE orders SET created=? WHERE id=?',(midnight+10,unpaid))
            db.execute('UPDATE telegram_users SET first_seen=? WHERE user_id=42',(midnight-31*86400,))
        result=self.analytics.daily_sales(30,now=now)
        series=result['series']
        self.assertEqual(len(series),30)
        self.assertEqual((series[0]['date'],series[-1]['date']),('2026-02-09','2026-03-10'))
        self.assertEqual([series[-1]['orders'],series[-2]['orders'],series[0]['orders']],[2,1,1])
        self.assertEqual(series[-1]['revenue'],2000)
        kpi=result['kpi']
        self.assertEqual((kpi['orders']['value'],kpi['orders']['previous']),(4,1))  # the 60-day-old sale is out of range
        self.assertEqual((kpi['revenue']['value'],kpi['average']['value'],kpi['average']['previous']),(4000,1000,1000))
        self.assertEqual((kpi['customers']['value'],kpi['customers']['previous']),(0,1))
        self.assertEqual(self.analytics.daily_sales(7,now=now)['series'][-1]['date'],'2026-03-10')
        with self.assertRaises(ValueError):self.analytics.daily_sales(0)
        with self.store.read() as db:
            plan=' '.join(row[-1] for row in db.execute('EXPLAIN QUERY PLAN SELECT paid_at,price FROM orders WHERE paid_at>=? AND paid_at<? AND charge IS NOT NULL',(0,1)))
        self.assertIn('orders_paid_at',plan)

if __name__=='__main__':unittest.main()
