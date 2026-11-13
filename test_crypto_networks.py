import base64
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock,patch
from cryptography.fernet import Fernet
from store import Store
from bep20 import PendingPayment,PaymentMismatch,VerifiedTransfer,TRANSFER
import crypto_networks
from crypto_networks import NETWORKS,NetworkVerifier,normalize_hash,ton_raw,tron_hex,aptos_address,amount

TX='a'*64
TON='0:'+'1'*64
APT='0x'+'1'*64
TRON=NETWORKS['tron']['token']


class NetworkSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.key=Fernet.generate_key();self.store=Store(Path(self.temp.name)/'shop.db',self.key)
        self.store.set_currency('100','test')

    def tearDown(self):self.temp.cleanup()

    def test_config_validation_keys_encrypted_and_preserved(self):
        self.store.save_payment_method('ton',True,TON,'private-api-key','owner')
        self.assertNotIn('private-api-key',json.dumps(self.store.payment_methods()))
        with self.store.tx() as db:
            secret=db.execute("SELECT api_key FROM payment_methods WHERE network='ton'").fetchone()[0]
        self.assertNotIn(b'private-api-key',secret)
        self.store.save_payment_method('ton',False,TON,'','owner')
        self.assertEqual(next(m for m in self.store.payment_methods(True)['networks'] if m['network']=='ton')['api_key'],'private-api-key')
        with self.assertRaises(ValueError):self.store.save_payment_method('tron',True,'','','owner')
        with self.assertRaises(ValueError):self.store.save_payment_method('aptos',True,'0x'+'a'*40,'','owner')
        self.store.save_payment_method('ton',False,TON,'','owner',True)
        self.assertFalse(next(m for m in self.store.payment_methods()['networks'] if m['network']=='ton')['has_api_key'])

    def test_invoice_network_decimals_and_replay_aliases(self):
        self.store.save_payment_method('ton',True,TON,'','owner')
        deposit=self.store.create_crypto_deposit(1,1000,network='ton')
        self.assertRegex(amount(deposit['amount_raw'],'ton'),r'^10\.\d{4}$')
        proof=VerifiedTransfer(TX,deposit['wallet'],deposit['token'],deposit['amount_raw'],deposit['created'],100,'block','ton','canonical-trace')
        with self.assertRaises(ValueError):self.store.credit_crypto(deposit['id'],1,replace(proof,network='bsc'))
        self.store.credit_crypto(deposit['id'],1,proof)
        self.assertFalse(self.store.credit_crypto(deposit['id'],1,proof))
        second=self.store.create_crypto_deposit(2,1000,network='ton')
        with self.assertRaises(ValueError):self.store.credit_crypto(second['id'],2,replace(proof,txid='b'*64,amount_raw=second['amount_raw']))
        self.assertEqual(self.store.wallet(1)['balance'],1000)
        self.assertEqual(self.store.wallet(2)['balance'],0)
        self.store.save_payment_method('ton',False,TON,'','owner')
        with self.assertRaises(ValueError):self.store.create_crypto_deposit(3,1000,network='ton')


def boc(bits,index=False):
    """Single-cell BOC without crc (optionally with an index) for parser tests."""
    padded=bits+('1'+'0'*((-len(bits)-1)%8) if len(bits)%8 else '')
    data=int(padded,2).to_bytes(len(padded)//8,'big') if padded else b''
    cell=bytes([0,len(bits)//8+(len(bits)+7)//8])+data
    header=bytes.fromhex('b5ee9c72')+bytes([0x81 if index else 0x01,1,1,1,0,len(cell),0])
    return base64.b64encode(header+(bytes([len(cell)]) if index else b'')+cell).decode()


def address_bits(raw):
    wc,account=raw.split(':')
    return '100'+format(int(wc)&0xFF,'08b')+format(int(account,16),'0256b')


def transfer_body(value):
    length=(value.bit_length()+7)//8
    bits=format(0x178d4519,'032b')+'0'*64+format(length,'04b')+(format(value,'0%db'%(length*8)) if length else '')
    return boc(bits+address_bits('0:'+'3'*64)+'00'+'0000',index=True)


class FakeContent:
    def __init__(self,body):self.body=body
    async def iter_chunked(self,size):yield self.body


class FakeResponse:
    def __init__(self,body,status=200):self.status=status;self.headers={};self.content=FakeContent(body)
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass


class NetworkVerificationTests(unittest.IsolatedAsyncioTestCase):
    def tron_receipt(self,logs=None):
        token=NETWORKS['tron']['token']
        log={'address':tron_hex(token)[2:],'topics':[TRANSFER[2:],'0'*64,'0'*24+tron_hex(TRON)[2:]],'data':hex(1000100)[2:]}
        return {'id':TX,'receipt':{'result':'SUCCESS'},'blockTimeStamp':1000000,'blockNumber':5,'log':logs or [log]}

    async def test_tron_final_receipt_and_exact_transfer(self):
        token=NETWORKS['tron']['token'];receipt=self.tron_receipt()
        v=NetworkVerifier('tron');v.request=AsyncMock(return_value=receipt)
        proof=await v.verify(TX,TRON,token,'1000100')
        self.assertEqual((proof.network,proof.block_time,proof.transfer_id),('tron',1000,TX+':0'))
        v.request=AsyncMock(return_value={})
        with self.assertRaises(PendingPayment):await v.verify(TX,TRON,token,'1000100')
        v.request=AsyncMock(return_value=receipt)
        with self.assertRaises(PaymentMismatch):await v.verify(TX,TRON,token,'1000101')
        receipt['receipt']['result']='REVERT'
        with self.assertRaises(PaymentMismatch):await v.verify(TX,TRON,token,'1000100')

    async def test_tron_batched_and_malformed(self):
        token=NETWORKS['tron']['token'];good=self.tron_receipt()['log'][0]
        logs=[dict(good,data=hex(5)[2:]),dict(good,topics=[TRANSFER[2:],'0'*64,'0'*64]),good]
        v=NetworkVerifier('tron');v.request=AsyncMock(return_value=self.tron_receipt(logs))
        self.assertEqual((await v.verify(TX,TRON,token,'1000100')).transfer_id,TX+':2')
        for bad in [self.tron_receipt([dict(good,data='zz')]),{'id':TX,'receipt':None},['unexpected'],
                    dict(self.tron_receipt(),blockTimeStamp=None),dict(self.tron_receipt(),id='b'*64)]:
            v.request=AsyncMock(return_value=bad)
            with self.subTest(bad=bad),self.assertRaises(PendingPayment):await v.verify(TX,TRON,token,'1000100')
        with self.assertRaises(PaymentMismatch):await v.verify('nonsense',TRON,token,'1000100')

    def aptos_tx(self,events=None):
        token=NETWORKS['aptos']['token']
        store='0x'+hashlib.sha3_256(bytes.fromhex(APT[2:])+bytes.fromhex(aptos_address(token)[2:])+b'\xfc').hexdigest()
        event={'type':'0x1::fungible_asset::Deposit','data':{'store':store,'amount':'1000100'}}
        return {'hash':'0x'+TX,'type':'user_transaction','success':True,'timestamp':'1000000000','version':'7',
                'events':events if events is not None else [event]}

    async def test_aptos_primary_store_binds_asset_and_recipient(self):
        token=NETWORKS['aptos']['token'];tx=self.aptos_tx()
        v=NetworkVerifier('aptos');v.request=AsyncMock(side_effect=[{'chain_id':1},tx])
        proof=await v.verify('0x'+TX,APT,token,'1000100')
        self.assertEqual((proof.network,proof.transfer_id),('aptos','0x'+TX+':0'))
        v.request=AsyncMock(side_effect=[{'chain_id':1},tx])
        with self.assertRaises(PaymentMismatch):await v.verify('0x'+TX,'0x'+'2'*64,token,'1000100')
        v.request=AsyncMock(side_effect=[{'chain_id':2}])
        with self.assertRaises(PaymentMismatch):await v.verify('0x'+TX,APT,token,'1000100')
        v.request=AsyncMock(side_effect=[{'chain_id':1},dict(tx,success=False)])
        with self.assertRaises(PaymentMismatch):await v.verify('0x'+TX,APT,token,'1000100')

    async def test_aptos_batched_and_malformed(self):
        token=NETWORKS['aptos']['token'];event=self.aptos_tx()['events'][0]
        events=[{'type':'0x1::fungible_asset::Withdraw','data':{}},dict(event,data=dict(event['data'],amount='5')),event]
        v=NetworkVerifier('aptos');v.request=AsyncMock(side_effect=[{'chain_id':1},self.aptos_tx(events)])
        self.assertEqual((await v.verify('0x'+TX,APT,token,'1000100')).transfer_id,'0x'+TX+':2')
        for side in [[{}],[{'chain_id':'x'}],[{'chain_id':1},{'type':'user_transaction'}],
                     [{'chain_id':1},self.aptos_tx([{'type':'0x1::fungible_asset::Deposit','data':None}])],
                     [{'chain_id':1},dict(self.aptos_tx(),timestamp=None)]]:
            v.request=AsyncMock(side_effect=side)
            with self.subTest(side=side),self.assertRaises(PendingPayment):await v.verify('0x'+TX,APT,token,'1000100')

    async def test_real_request_path_non_json_and_http_errors_are_pending(self):
        for response in [FakeResponse(b'<html>'),FakeResponse(b'',status=429),FakeResponse(b'{}',status=404)]:
            with patch('aiohttp.ClientSession.request',new=lambda *a,**k:response):
                with self.assertRaises(PendingPayment):
                    await NetworkVerifier('tron').verify(TX,TRON,NETWORKS['tron']['token'],'1000100')


MASTER=NETWORKS['ton']['token'];RECEIVER='0:'+'2'*64;SENDER='0:'+'3'*64


class TonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        crypto_networks._ton_wallet_cache.clear()
        self.receiving={'hash':'b'*64,'lt':'20','account':RECEIVER,'now':1000,
                        'description':{'aborted':False,'compute_ph':{'success':True}},
                        'in_msg':{'opcode':'0x178d4519','source':SENDER}}
        self.trace={'trace_id':'c'*64,'is_incomplete':False,'mc_seqno_end':100,'start_utime':990,'end_utime':1001,
                    'transactions':{'source':{'hash':TX,'account':SENDER,'description':{'aborted':False}},
                                    'destination':self.receiving,
                                    'notification':{'hash':'d'*64,'account':TON,'description':{'aborted':True}}}}
        self.transfers={'jetton_transfers':[{'trace_id':'c'*64,'transaction_aborted':False,'destination':TON,
                                             'jetton_master':MASTER,'amount':'1000100','source_wallet':SENDER}]}
        self.derived=RECEIVER;self.calls=[];self.v3_fails=False

    async def api(self,url,payload=None,params=None,**kwargs):
        self.calls.append(url.rsplit('/',1)[-1])
        if url.endswith('/traces'):return {'traces':[self.trace]}
        if url.endswith('/v3/runGetMethod'):
            if self.v3_fails:raise PendingPayment('down')
            self.payload=payload
            return {'gas_used':1,'exit_code':0,'stack':[{'type':'cell','value':boc(address_bits(self.derived))}]}
        if url.endswith('/v2/runGetMethod'):
            self.payload=payload
            return {'ok':True,'result':{'exit_code':0,'stack':[['cell',{'bytes':boc(address_bits(self.derived),True)}]]}}
        if url.endswith('/jetton/transfers'):return self.transfers
        raise AssertionError(url)

    async def verify(self,raw='1000100'):
        v=NetworkVerifier('ton');v.request=self.api
        return await v.verify(TX,TON,MASTER,raw)

    async def test_completed_trace_receiving_wallet_and_notification_bounce(self):
        proof=await self.verify()
        self.assertEqual((proof.network,proof.txid,proof.transfer_id,proof.block_time),('ton','c'*64,'b'*64,1000))
        self.receiving['description']['aborted']=True
        with self.assertRaises(PaymentMismatch):await self.verify()
        self.trace['is_incomplete']=True
        with self.assertRaises(PendingPayment):await self.verify()

    async def test_wallet_derived_on_chain_and_cached(self):
        await self.verify()
        self.assertEqual(self.payload['method'],'get_wallet_address')
        argument=self.payload['stack'][0]
        self.assertEqual(argument['type'],'slice')
        self.assertEqual(argument['value'],crypto_networks.ton_address_slice_boc(TON))
        self.assertEqual(crypto_networks.bits_to_ton_address(crypto_networks.boc_root_bits(argument['value'])),TON)
        self.assertTrue(argument['value'].startswith('te6cckEBAQEAJAAAQ'))
        self.calls.clear();await self.verify()
        self.assertNotIn('runGetMethod',self.calls)

    async def test_v2_fallback(self):
        self.v3_fails=True
        self.assertEqual((await self.verify()).transfer_id,'b'*64)
        self.assertEqual(self.payload['stack'],[['tvm.Slice',crypto_networks.ton_address_slice_boc(TON)]])

    async def test_fake_jetton_wallet_claiming_shop_owner_is_rejected(self):
        # The index would report RECEIVER as the shop's wallet, but the real derived one differs.
        self.derived='0:'+'4'*64
        with self.assertRaises(PaymentMismatch):await self.verify()
        crypto_networks._ton_wallet_cache.clear()
        self.trace['end_utime']=int(time.time())
        with self.assertRaises(PendingPayment):await self.verify()

    async def test_index_lag_is_pending_while_trace_is_young(self):
        self.transfers={'jetton_transfers':[]}
        with self.assertRaises(PaymentMismatch):await self.verify()
        self.trace['end_utime']=int(time.time())-30
        with self.assertRaises(PendingPayment):await self.verify()

    async def test_batched_transfers_use_body_amount(self):
        other=dict(self.receiving,hash='e'*64,lt='10',in_msg=dict(self.receiving['in_msg'],message_content={'body':transfer_body(500)}))
        self.receiving['in_msg']['message_content']={'body':transfer_body(1000100)}
        self.trace['transactions']['other']=other
        self.transfers={'jetton_transfers':[]}       # body decoding needs no index
        proof=await self.verify()
        self.assertEqual(proof.transfer_id,'b'*64)
        self.assertNotIn('transfers',self.calls)
        self.assertEqual((await self.verify('500')).transfer_id,'e'*64)
        with self.assertRaises(PaymentMismatch):await self.verify('777')

    async def test_malformed_ton_responses_are_pending(self):
        async def broken(url,**kwargs):
            if url.endswith('/traces'):return {'traces':[{'is_incomplete':False,'mc_seqno_end':5,'transactions':None}]}
            raise AssertionError(url)
        v=NetworkVerifier('ton');v.request=broken
        with self.assertRaises(PendingPayment):await v.verify(TX,TON,MASTER,'1000100')
        self.trace['transactions']['destination']['now']=None
        with self.assertRaises(PendingPayment):await self.verify()
        self.trace['transactions']['destination']['now']=1000
        self.trace['trace_id']='%%%'
        with self.assertRaises(PendingPayment):await self.verify()
        self.trace['trace_id']='c'*64
        self.derived=None
        async def bad_get(url,**kwargs):
            if url.endswith('/traces'):return {'traces':[self.trace]}
            return {'exit_code':0,'stack':[{'type':'cell','value':'not-a-boc'}]}
        crypto_networks._ton_wallet_cache.clear()
        v.request=bad_get
        with self.assertRaises(PendingPayment):await v.verify(TX,TON,MASTER,'1000100')

    def test_boc_helpers(self):
        self.assertEqual(crypto_networks._crc32c(b'123456789'),0xE3069283)
        self.assertEqual(crypto_networks.ton_internal_transfer_amount(transfer_body(1000100)),1000100)
        self.assertEqual(crypto_networks.ton_internal_transfer_amount(transfer_body(0)),0)
        self.assertIsNone(crypto_networks.ton_internal_transfer_amount(boc('1'*32)))
        self.assertEqual(crypto_networks.bits_to_ton_address(address_bits('-1:'+'ab'*32)),'-1:'+'ab'*32)


if __name__=='__main__':unittest.main(verbosity=2)
