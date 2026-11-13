import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from store import Store


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'shop.db',Fernet.generate_key())
        self.pid=self.store.save_product(dict(category='Test',name='License',price=100,mode='auto',description='',instruction='',active=True))

    def tearDown(self):self.temp.cleanup()

    def test_balance_before_after_reason_and_idempotency(self):
        self.store.adjust_wallet(42,500,'Compensation','one','owner')
        self.store.adjust_wallet(42,500,'Compensation','one','owner')
        self.store.set_wallet(42,250,'Correction','two','owner')
        events=self.store.events(category='balance')['items']
        self.assertEqual(len(events),2)
        details=json.loads(events[0]['details'])
        self.assertEqual((details['before'],details['after'],details['reason']),(500,250,'Correction'))
        self.assertEqual(events[0]['user_id'],42)

    def test_order_timeline_and_delivery_failure_recovery_without_secrets(self):
        secret='private-password-no-logging'
        self.store.import_stock(self.pid,secret)
        order=self.store.create_order(self.pid,42)
        self.store.paid(order['id'],42,100,'XTR','charge')
        delivery=self.store.deliveries()[0]
        self.store.delivery_result(delivery['id'],False)
        self.store.delivery_result(delivery['id'],True)
        events=self.store.order_info(order['id'],'owner')['events']
        actions=[event['action'] for event in events]
        self.assertLess(actions.index('order.created'),actions.index('payment.accepted'))
        self.assertLess(actions.index('delivery.retry'),actions.index('delivery.sent'))
        self.assertNotIn(secret,json.dumps(events))
        # A retried delivery is not an error: admins are alerted only when delivery gives up.
        self.assertEqual(self.store.events(level='error')['total'],0)
        self.assertEqual(self.store.events(query=order['id'])['total'],len(events))

    def test_filters_pagination_and_redaction(self):
        with patch.dict(os.environ,{'BOT_TOKEN':'secret-test-token'}):
            for i in range(30):self.store.adjust_wallet(42,1,'Reason secret-test-token',str(i),'owner')
        first=self.store.events(category='balance',query='Reason')
        self.assertEqual((first['total'],first['pages'],len(first['items'])),(30,2,25))
        self.assertEqual(len(self.store.events(category='balance',page=2)['items']),5)
        self.assertNotIn('secret-test-token',json.dumps(first))
        at=first['items'][0]['at']
        self.assertEqual(self.store.events(after=at+1)['total'],0)
        self.assertEqual(self.store.events(before=at+1,category='balance')['total'],30)
        with self.assertRaises(ValueError):self.store.events(level='wrong')

    def test_expiration_logs_once(self):
        self.store.import_stock(self.pid,'secret')
        order=self.store.create_order(self.pid,42)
        with self.store.tx() as db:db.execute('UPDATE orders SET expires=0 WHERE id=?',(order['id'],))
        self.store.products()
        self.assertEqual(sum(e['action']=='order.expired' for e in self.store.events()['items']),0)
        self.store.housekeeping();self.store.housekeeping()
        self.assertEqual(sum(e['action']=='order.expired' for e in self.store.events()['items']),1)
