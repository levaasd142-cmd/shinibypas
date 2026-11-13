import os
import asyncio
from dataclasses import replace
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from store import Store
import json
import aiohttp
from bep20 import BscVerifier,PendingPayment,PaymentMismatch,VerifiedTransfer,TRANSFER,TOKEN,WALLET,MAX_BODY

TX='0x'+'a'*64
BLOCK='0x'+'b'*64


class DepositTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'shop.db',Fernet.generate_key())
        self.store.set_currency('100','test')
        self._bsc_env=patch.dict(os.environ,{'BSC_WALLET':'0x4AB3C61e666BA119deA95e0AD78819B3681DF53b'});self._bsc_env.start();self.addCleanup(self._bsc_env.stop)

    def tearDown(self):self.temp.cleanup()

    def proof(self,d):
        return VerifiedTransfer(TX,d['wallet'],d['token'],d['amount_raw'],d['created'],100,BLOCK)

    def test_invoice_unique_reused_for_same_user_only(self):
        a=self.store.create_crypto_deposit(1,1000);b=self.store.create_crypto_deposit(2,1000)
        self.assertNotEqual(a['amount_raw'],b['amount_raw'])
        self.assertEqual(self.store.create_crypto_deposit(1,1000)['id'],a['id'])
        self.assertGreater(int(a['amount_raw']),10*10**18)
        self.assertLess(int(a['amount_raw']),10*10**18+10**16)
        self.assertEqual(int(a['amount_raw'])%10**14,0)

    def test_four_decimals_and_legacy_invoice_precision(self):
        from bep20 import token_amount
        self.assertEqual(token_amount(10*10**18+12*10**14),'10.0012')
        self.assertEqual(token_amount(10*10**18+123*10**12),'10.000123')

    def test_credit_is_idempotent_and_owned(self):
        d=self.store.create_crypto_deposit(1,1000);proof=self.proof(d)
        with self.assertRaises(ValueError):self.store.credit_crypto(d['id'],2,proof)
        self.assertTrue(self.store.credit_crypto(d['id'],1,proof))
        self.assertFalse(self.store.credit_crypto(d['id'],1,proof))
        self.assertEqual(self.store.wallet(1)['balance'],1000)
        other=self.store.create_crypto_deposit(2,1000)
        with self.assertRaises(ValueError):self.store.credit_crypto(other['id'],2,self.proof(other))

    def test_wrong_or_late_proof_never_credits(self):
        d=self.store.create_crypto_deposit(1,1000);proof=self.proof(d)
        for bad in [replace(proof,wallet='0x'+'1'*40),replace(proof,token='0x'+'2'*40),
                    replace(proof,amount_raw='1'),replace(proof,block_time=d['created']-1),replace(proof,block_time=d['expires']+1)]:
            with self.assertRaises(ValueError):self.store.credit_crypto(d['id'],1,bad)
        self.assertEqual(self.store.wallet(1)['balance'],0)

    def test_crypto_checkout_fulfills_once_for_auto_and_manual(self):
        for i,mode in enumerate(('auto','manual')):
            pid=self.store.save_product(dict(category='Test',name='Product',description='',price=50,mode=mode,instruction='',active=True))
            if mode=='auto':self.store.import_stock(pid,'test-key')
            order=self.store.create_order(pid,1)
            deposit=self.store.create_crypto_deposit(1,50,order_id=order['id'])
            self.assertEqual(deposit['expires']-deposit['created'],900)
            with self.store.tx() as db:
                self.assertEqual(db.execute('SELECT expires FROM orders WHERE id=?',(order['id'],)).fetchone()[0],deposit['expires'])  # reservation follows the invoice
            proof=replace(self.proof(deposit),txid='0x'+str(i+1)*64)
            self.store.credit_crypto(deposit['id'],1,proof)
            self.store.credit_crypto(deposit['id'],1,proof)
            found=next(o for o in self.store.orders(1) if o['id']==order['id'])
            self.assertEqual(found['status'],'fulfilled' if mode=='auto' else 'awaiting_details')
            self.assertEqual(self.store.wallet(1)['balance'],0)

    def test_expired_checkout_keeps_paid_funds_in_wallet(self):
        pid=self.store.save_product(dict(category='Test',name='Product',description='',price=1000,mode='manual',instruction='',active=True))
        order=self.store.create_order(pid,1);deposit=self.store.create_crypto_deposit(1,1000,order_id=order['id'])
        with self.store.tx() as db:db.execute('UPDATE orders SET expires=0 WHERE id=?',(order['id'],))
        self.store.credit_crypto(deposit['id'],1,self.proof(deposit))
        self.assertEqual(self.store.wallet(1)['balance'],1000)


class FakeContent:
    def __init__(self,body):self.body=body
    async def iter_chunked(self,size):
        for i in range(0,len(self.body),size):yield self.body[i:i+size]


class FakeResponse:
    def __init__(self,body,status=200,headers=None):
        self.status=status;self.headers=headers or {};self.content=FakeContent(body)
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass


def rpc_body(result):return json.dumps({'jsonrpc':'2.0','id':1,'result':result}).encode()


class RpcTests(unittest.IsolatedAsyncioTestCase):
    def results(self):
        self.log={'address':TOKEN,'topics':[TRANSFER,'0x'+'0'*64,'0x'+'0'*24+WALLET[2:].lower()],
                  'data':hex(10**19),'logIndex':'0x3'}
        self.receipt={'transactionHash':TX,'status':'0x1','blockNumber':'0x64','blockHash':BLOCK,'logs':[self.log]}
        return {'eth_chainId':'0x38','eth_getTransactionReceipt':self.receipt,'eth_blockNumber':hex(120),
                'eth_getBlockByNumber':{'hash':BLOCK,'timestamp':hex(int(time.time()))}}

    async def verify(self,change=None,raw_override=None):
        results=self.results()
        if change:change(results,self.log,self.receipt)
        def request(session,method,url,**kwargs):
            name=kwargs['json']['method']
            if raw_override and name in raw_override:return raw_override[name]
            return FakeResponse(rpc_body(results[name]))
        with patch('aiohttp.ClientSession.request',new=request):
            return await BscVerifier().verify(TX,WALLET,TOKEN,str(10**19))

    async def test_valid_receipt(self):
        proof=await self.verify()
        self.assertEqual((proof.txid,proof.transfer_id,proof.network),(TX,TX+':3','bsc'))

    async def test_forged_token_failed_tx_wrong_network_or_recipient(self):
        changes=[lambda r,l,t:l.update(address='0x'+'1'*40),lambda r,l,t:t.update(status='0x0'),
                 lambda r,l,t:r.update(eth_chainId='0x1'),lambda r,l,t:l['topics'].__setitem__(2,'0x'+'0'*64),
                 lambda r,l,t:l.update(data=hex(1)),lambda r,l,t:t.update(logs=[]),lambda r,l,t:l.update(removed=True)]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(PaymentMismatch):await self.verify(change)

    async def test_pending_and_reorg_wait(self):
        for change in [lambda r,l,t:r.update(eth_blockNumber=hex(101)),lambda r,l,t:r.update(eth_getTransactionReceipt=None),
                       lambda r,l,t:r['eth_getBlockByNumber'].update(hash='0x'+'c'*64)]:
            with self.assertRaises(PendingPayment):await self.verify(change)

    async def test_malformed_responses_are_pending_not_mismatch(self):
        changes=[lambda r,l,t:l.update(data='0xZZ'),lambda r,l,t:t.update(status=None),
                 lambda r,l,t:t.pop('blockNumber'),lambda r,l,t:t.update(logs=None),
                 lambda r,l,t:r.update(eth_chainId='garbage'),lambda r,l,t:r.update(eth_getTransactionReceipt=[1,2]),
                 lambda r,l,t:l.pop('logIndex'),lambda r,l,t:r.update(eth_getBlockByNumber={'hash':BLOCK,'timestamp':None})]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(PendingPayment):await self.verify(change)
        overrides=[FakeResponse(b'<html>busy</html>'),FakeResponse(b'',status=429),FakeResponse(b'{}',status=502),
                   FakeResponse(b'{"jsonrpc":"2.0","error":{"code":-32000}}'),FakeResponse(b'null'),
                   FakeResponse(b'x'*(MAX_BODY+10)),FakeResponse(b'{}',headers={'Content-Length':str(MAX_BODY+1)})]
        for override in overrides:
            with self.assertRaises(PendingPayment):await self.verify(raw_override={'eth_getTransactionReceipt':override})

    async def test_network_error_is_pending(self):
        def request(session,method,url,**kwargs):raise aiohttp.ClientConnectionError('down')
        with patch('aiohttp.ClientSession.request',new=request):
            with self.assertRaises(PendingPayment):await BscVerifier().verify(TX,WALLET,TOKEN,str(10**19))

    async def test_bad_txid_is_definite(self):
        with self.assertRaises(PaymentMismatch):await BscVerifier().verify('0x123',WALLET,TOKEN,'1')

    async def test_batched_transfer_picks_exact_amount(self):
        def change(r,l,t):
            other=dict(l,data=hex(5*10**18),logIndex='0x1')
            foreign=dict(l,topics=[TRANSFER,'0x'+'0'*64,'0x'+'0'*24+'9'*40],logIndex='0x2')
            t['logs']=[other,foreign,dict(l,logIndex='0x7')]
        proof=await self.verify(change)
        self.assertEqual((proof.transfer_id,proof.amount_raw),(TX+':7',str(10**19)))

    def test_exception_hierarchy(self):
        self.assertTrue(issubclass(PaymentMismatch,ValueError))
        self.assertTrue(issubclass(PendingPayment,ValueError))
        self.assertFalse(issubclass(PaymentMismatch,PendingPayment))


class ThrottleTests(unittest.IsolatedAsyncioTestCase):
    async def test_throttle_is_shared_across_calls(self):
        from bep20 import throttle
        start=time.monotonic()
        for _ in range(3):await throttle('unit-test',0.05)
        self.assertGreaterEqual(time.monotonic()-start,0.09)


if __name__=='__main__':unittest.main(verbosity=2)
