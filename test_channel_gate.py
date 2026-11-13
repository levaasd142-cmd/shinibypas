from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from aiogram.exceptions import TelegramBadRequest,TelegramForbiddenError,TelegramRetryAfter
from aiogram.methods import GetChatMember
from cryptography.fernet import Fernet

from backups import inspect_database
from channel_gate import ChannelGate,inspect_channel,normalize
from store import Store


class ChannelGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.key=Fernet.generate_key()
        self.store=Store(Path(self.temp.name)/'shop.db',self.key);self.gate=ChannelGate(self.store)
        self.config=dict(channel_ref='@TheMarketFresh',join_url='https://t.me/TheMarketFresh',chat_id=-1001234567890,title='TheMarket',checked_at=time.time())
        self.bot=SimpleNamespace(id=9876,get_chat=AsyncMock(return_value=SimpleNamespace(id=-1001234567890,type='channel',title='TheMarket',username='TheMarketFresh')),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator')))

    def tearDown(self):self.temp.cleanup()

    def activate(self):self.gate.save(True,self.config,'owner')
    def new_user(self,uid=42):self.store.remember_customer(uid);return self.gate.required(uid)
    def statuses(self,status,**extra):
        async def membership(chat_id,user_id,**kwargs):
            return SimpleNamespace(status='administrator') if user_id==self.bot.id else SimpleNamespace(status=status,**extra)
        return membership
    def unthrottle(self):
        with self.store.tx() as db:db.execute('UPDATE channel_access SET next_check=0')

    def test_normalize_public_private_and_reject_other_destinations(self):
        for value in ('@TheMarketFresh','TheMarketFresh','https://t.me/TheMarketFresh','t.me/TheMarketFresh/'):
            self.assertEqual(normalize(value),dict(channel_ref='@TheMarketFresh',join_url='https://t.me/TheMarketFresh'))
        self.assertEqual(normalize('-1001234567890','https://t.me/+invite_hash')['channel_ref'],'-1001234567890')
        for value,url in [('https://evil.example/TheMarketFresh',''),('https://t.me@evil.example/x',''),('https://t.me/TheMarketFresh/12',''),
            ('@TheMarketFresh','https://t.me/OtherChannel'),('-1001234567890','https://evil.example/invite'),
            ('-1001234567890','https://t.me/TheMarketFresh/15'),('@foo/bar','')]:
            with self.subTest(value=value,url=url),self.assertRaises(ValueError):normalize(value,url)

    async def test_configuration_resolves_channel_and_admin(self):
        result=await inspect_channel(self.bot,'@TheMarketFresh')
        self.assertEqual(result['chat_id'],self.config['chat_id'])
        self.assertEqual(result['join_url'],'https://t.me/TheMarketFresh')
        self.bot.get_chat_member.assert_awaited_once_with(self.config['chat_id'],self.bot.id,request_timeout=10)
        self.assertTrue(result['checked_at'])

    async def test_configuration_rejects_groups_missing_admin_and_unavailable_channel(self):
        self.bot.get_chat.return_value.type='supergroup'
        with self.assertRaisesRegex(ValueError,'канал'):await inspect_channel(self.bot,'@TheMarketFresh')
        self.bot.get_chat.return_value.type='channel';self.bot.get_chat_member.return_value.status='member'
        with self.assertRaisesRegex(ValueError,'администратором'):await inspect_channel(self.bot,'@TheMarketFresh')
        self.bot.get_chat.side_effect=TelegramBadRequest(method=GetChatMember(chat_id=-1001234567890,user_id=9876),message='Private internal detail')
        with self.assertRaises(ValueError) as error:await inspect_channel(self.bot,'@TheMarketFresh')
        self.assertNotIn('Private internal',str(error.exception))

    async def test_private_channel_requires_invite_and_preserves_it(self):
        self.bot.get_chat.return_value.username=None
        with self.assertRaisesRegex(ValueError,'пригласительную'):await inspect_channel(self.bot,'-1001234567890')
        result=await inspect_channel(self.bot,'-1001234567890','https://t.me/+invite_hash')
        self.assertEqual(result['join_url'],'https://t.me/+invite_hash')

    def test_disabled_by_default_and_existing_visitors_keep_access(self):
        self.assertFalse(self.gate.settings()['enabled']);self.new_user(41)
        self.activate();self.assertIsNone(self.gate.required(41));self.assertIsNotNone(self.new_user(42))
        self.store.remember_customer(42);self.assertIsNotNone(self.gate.required(42))

    def test_migration_exempts_legacy_buyers_and_does_not_complete_pending_users(self):
        self.store.adjust_wallet(99,100,'fixture','legacy-credit','owner')
        with self.store.tx() as db:db.execute('DROP TABLE channel_access')
        self.store=Store(self.store.path,self.key);self.gate=ChannelGate(self.store);self.activate()
        self.new_user(99);self.assertIsNone(self.gate.required(99));self.new_user(42)
        reopened=Store(self.store.path,self.key)
        self.assertIsNotNone(ChannelGate(reopened).required(42))

    async def test_real_membership_is_verified_once_and_survives_restart(self):
        self.activate();config=self.new_user()
        self.bot.get_chat_member.side_effect=[SimpleNamespace(status='administrator'),SimpleNamespace(status='member')]
        self.assertEqual(await self.gate.verify(self.bot,42,config),'passed')
        self.assertIsNone(self.gate.required(42))
        reopened=Store(self.store.path,self.key)
        self.assertIsNone(ChannelGate(reopened).required(42))
        with self.store.tx() as db:
            row=db.execute('SELECT * FROM channel_access WHERE user_id=42').fetchone()
            self.assertEqual(row['verified_channel'],self.config['chat_id'])
            self.assertIsNotNone(row['completed_at'])

    async def test_nonmembers_requests_restricted_and_kicked_do_not_pass(self):
        self.activate()
        for index,status in enumerate(('left','kicked','restricted')):
            config=self.new_user(42+index)
            self.bot.get_chat_member.side_effect=self.statuses(status)
            self.assertEqual(await self.gate.verify(self.bot,42+index,config),'missing')
            self.assertIsNotNone(self.gate.required(42+index))

    async def test_admin_owner_members_pass(self):
        self.activate()
        for index,status in enumerate(('administrator','creator')):
            config=self.new_user(42+index)
            self.bot.get_chat_member.side_effect=self.statuses(status)
            self.assertEqual(await self.gate.verify(self.bot,42+index,config),'passed')

    async def test_telegram_errors_do_not_unlock_and_are_logged_without_payloads(self):
        self.activate();config=self.new_user();method=GetChatMember(chat_id=self.config['chat_id'],user_id=42)
        for error in (TelegramForbiddenError(method=method,message='secret invite'),TimeoutError()):
            self.unthrottle();self.bot.get_chat_member.side_effect=error
            self.assertEqual(await self.gate.verify(self.bot,42,config),'unavailable')
            self.assertIsNotNone(self.gate.required(42))
        events=[r for r in self.store.audits() if r['action']=='channel.check.failed']
        self.assertEqual(len(events),1);self.assertNotIn('secret invite',str(events))

    async def test_revoked_bot_admin_does_not_unlock_even_if_member_call_would_succeed(self):
        self.activate();config=self.new_user();self.bot.get_chat_member.return_value.status='member'
        self.assertEqual(await self.gate.verify(self.bot,42,config),'unavailable')
        self.assertEqual(self.bot.get_chat_member.await_count,1)

    async def test_repeated_clicks_and_retry_after_limit_network_calls(self):
        self.activate();config=self.new_user()
        method=GetChatMember(chat_id=self.config['chat_id'],user_id=42)
        self.bot.get_chat_member.side_effect=TelegramRetryAfter(method=method,message='retry',retry_after=60)
        self.assertEqual(await self.gate.verify(self.bot,42,config),'wait')
        self.assertEqual(await self.gate.verify(self.bot,42,config),'wait')
        self.assertEqual(self.bot.get_chat_member.await_count,1)

    async def test_channel_changed_during_network_check_does_not_unlock(self):
        self.activate();config=self.new_user()
        async def membership(chat_id,user_id,**kwargs):
            if user_id==42:self.gate.save(True,{**self.config,'chat_id':-1009999999999},'owner')
            return SimpleNamespace(status='administrator' if user_id==self.bot.id else 'member')
        self.bot.get_chat_member.side_effect=membership
        self.assertEqual(await self.gate.verify(self.bot,42,config),'changed')
        self.assertIsNotNone(self.gate.required(42))

    def test_disabling_allows_pending_visitors_and_never_relocks_completed(self):
        self.activate();self.new_user()
        self.gate.save(False,self.config,'owner');self.store.remember_customer(42)
        self.assertIsNone(self.gate.required(42));self.activate();self.assertIsNone(self.gate.required(42))

    def test_unverified_config_cannot_be_enabled(self):
        with self.assertRaises(ValueError):self.gate.save(True,normalize('@TheMarketFresh'),'owner')
        self.assertFalse(self.gate.settings()['enabled'])

    def test_invite_encrypted_and_included_in_restore_verification(self):
        data={**self.config,'channel_ref':'-1001234567890','join_url':'https://t.me/+private_invite'}
        self.gate.save(True,data,'owner')
        self.assertEqual(self.gate.settings()['join_url'],data['join_url'])
        with self.store.tx() as db:
            self.assertNotIn(b'private_invite',db.execute('SELECT join_url FROM channel_settings').fetchone()[0])
        report=inspect_database(self.store.path,self.key)
        self.assertEqual(report['encrypted_values'],1)
        with self.store.tx() as db:db.execute("UPDATE channel_settings SET join_url=X'0000'")
        with self.assertRaises(ValueError):inspect_database(self.store.path,self.key)

    async def test_restricted_member_with_membership_passes(self):
        self.activate();config=self.new_user()
        self.bot.get_chat_member.side_effect=self.statuses('restricted',is_member=True)
        self.assertEqual(await self.gate.verify(self.bot,42,config),'passed')
        self.assertIsNone(self.gate.required(42))

    async def test_admin_rights_checked_once_per_minute(self):
        self.activate()
        self.bot.get_chat_member.side_effect=self.statuses('member')
        for uid in (42,43,44):
            self.assertEqual(await ChannelGate(self.store).verify(self.bot,uid,self.new_user(uid)),'passed')
        admin_calls=[c for c in self.bot.get_chat_member.await_args_list if c.args[1]==self.bot.id]
        self.assertEqual(len(admin_calls),1);self.assertEqual(self.bot.get_chat_member.await_count,4)

    def test_settings_decrypt_join_url_once_per_value(self):
        self.activate()
        with patch.object(self.store,'decrypt',wraps=self.store.decrypt) as decrypt:
            for uid in range(42,52):ChannelGate(self.store).required(uid)
            self.assertLessEqual(decrypt.call_count,1)
        data={**self.config,'channel_ref':'-1001234567890','join_url':'https://t.me/+fresh_invite'}
        self.gate.save(True,data,'owner')
        self.assertEqual(ChannelGate(self.store).settings()['join_url'],'https://t.me/+fresh_invite')

    async def test_repeated_unavailable_checks_fail_open_with_error_audit(self):
        self.activate();self.bot.get_chat_member.side_effect=TimeoutError()
        results=[]
        for uid in (42,43,44):
            config=self.new_user(uid);results.append(await ChannelGate(self.store).verify(self.bot,uid,config))
        self.assertEqual(results,['unavailable','unavailable','passed'])
        self.assertIsNone(self.new_user(45))
        self.assertIsNone(self.gate.required(42))
        with self.store.tx() as db:
            self.assertIsNone(db.execute('SELECT completed_at FROM channel_access WHERE user_id=42').fetchone()[0])
            errors=db.execute("SELECT count(*) FROM audit WHERE action='channel.check.failed' AND level='error'").fetchone()[0]
        self.assertEqual(errors,1)
        import channel_gate
        with patch.object(channel_gate.time,'time',return_value=time.time()+channel_gate.FAIL_OPEN_SECONDS+1):
            self.assertIsNotNone(self.gate.required(42))

    async def test_successful_check_resets_failure_streak(self):
        self.activate()
        for uid,effect in ((42,TimeoutError()),(43,TimeoutError()),(44,None),(45,TimeoutError())):
            config=self.new_user(uid)
            self.bot.get_chat_member.side_effect=effect or self.statuses('member')
            await self.gate.verify(self.bot,uid,config)
        self.assertFalse(self.gate.fail_open_active())
        self.assertIsNotNone(self.gate.required(45))
