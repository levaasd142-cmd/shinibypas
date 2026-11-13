import hashlib
import tempfile
import time
import unittest
from unittest.mock import patch

import staff
from staff import IDLE_SECONDS, Staff, hash_password, permissions, verify_password
from test_promo import open_store

OWNER_PASSWORD = 'env-owner-password-123'


class StaffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = open_store(self.temp.name)
        self.staff = Staff(self.store)
        self.owner = self.staff.ensure_owner('owner',OWNER_PASSWORD)

    def tearDown(self):
        staff._ENV_OWNERS.clear()
        self.temp.cleanup()

    def test_initialize_is_idempotent(self):
        with self.store.tx() as db:
            staff.initialize(db);staff.initialize(db)

    def test_password_hash_format_and_salt(self):
        first,second = hash_password('correct horse'),hash_password('correct horse')
        self.assertNotEqual(first,second)
        self.assertTrue(first.startswith('scrypt$16384$8$1$'))
        self.assertTrue(verify_password('correct horse',first))
        self.assertFalse(verify_password('correct horsE',first))
        self.assertFalse(verify_password('x','garbage'))
        with self.store.read() as db:
            stored = db.execute('SELECT password_hash FROM staff_users').fetchone()[0]
        self.assertNotIn(OWNER_PASSWORD,stored)

    def test_owner_bootstrap_idempotent_and_env_password_always_valid(self):
        self.assertEqual((self.owner['role'],self.owner['env_owner'],self.owner['active']),('owner',True,True))
        self.assertEqual(self.staff.ensure_owner('owner',OWNER_PASSWORD)['id'],self.owner['id'])
        self.assertEqual(len(self.staff.list()),1)
        self.assertEqual(self.staff.authenticate('OWNER',OWNER_PASSWORD)['username'],'owner')
        # The owner changes the password in the panel: the .env password still works.
        self.staff.update(self.owner['id'],password='panel-chosen-password',actor='owner')
        self.assertTrue(self.staff.authenticate('owner','panel-chosen-password'))
        self.assertTrue(self.staff.authenticate('owner',OWNER_PASSWORD))
        # Rotated .env password: the new one signs in at the next start.
        self.staff.ensure_owner('owner','rotated-env-password-1')
        self.assertTrue(self.staff.authenticate('owner','rotated-env-password-1'))
        self.assertIsNone(self.staff.authenticate('owner',OWNER_PASSWORD))
        with self.assertRaisesRegex(ValueError,'ADMIN_USER'):
            self.staff.update(self.owner['id'],role='manager',actor='owner')
        with self.assertRaisesRegex(ValueError,'ADMIN_USER'):
            self.staff.update(self.owner['id'],active=False,actor='owner')

    def test_create_validation_and_roles(self):
        manager = self.staff.create('Maria.M','manager-password',"manager",'owner')
        self.assertEqual(manager['permissions'],sorted(permissions('manager')))
        with self.assertRaisesRegex(ValueError,'уже существует'):self.staff.create('maria.m','another-password','support','owner')
        with self.assertRaisesRegex(ValueError,'Логин'):self.staff.create('ма','another-password','support','owner')
        with self.assertRaisesRegex(ValueError,'Пароль'):self.staff.create('short','123','support','owner')
        with self.assertRaisesRegex(ValueError,'роль'):self.staff.create('admin2','another-password','root','owner')
        with self.assertRaisesRegex(ValueError,'не найден'):self.staff.update(999,role='support',actor='owner')
        self.assertEqual([item['username'] for item in self.staff.list()],['owner','Maria.M'])
        self.assertNotIn('password_hash',self.staff.list()[0])

    def test_permissions_map(self):
        self.assertEqual(permissions('owner'),set(staff.PERMISSIONS))
        self.assertIn('promo.view',permissions('manager'));self.assertNotIn('promo.manage',permissions('manager'))
        self.assertNotIn('customers.balance',permissions('manager'));self.assertNotIn('staff',permissions('manager'))
        self.assertEqual(permissions('support'),{'support','orders.view','customers.view'})
        self.assertEqual(permissions('nobody'),set())

    def test_wrong_password_and_unknown_user_cost_one_scrypt(self):
        self.staff.create('helper','helper-password','support','owner')
        staff._dummy_hash()
        calls = []
        original = hashlib.scrypt
        def counting(*args,**kwargs):
            calls.append(1);return original(*args,**kwargs)
        with patch('staff.hashlib.scrypt',side_effect=counting):
            self.assertIsNone(self.staff.authenticate('helper','wrong-password'))
            self.assertIsNone(self.staff.authenticate('nobody-here','whatever-pass'))
            self.assertIsNone(self.staff.authenticate('bad name!','whatever-pass'))
            self.assertIsNone(self.staff.authenticate('helper',None))
            self.assertIsNone(self.staff.authenticate('helper','x'*1000))
        self.assertEqual(len(calls),5)
        with patch('staff.hmac.compare_digest',wraps=staff.hmac.compare_digest) as compare:
            self.staff.authenticate('helper','helper-password')
        self.assertTrue(compare.called)

    def test_disabled_staff_cannot_sign_in_and_loses_sessions(self):
        helper = self.staff.create('helper','helper-password','support','owner')
        token = self.staff.create_session(helper['id'],'127.0.0.1','UA')
        self.assertEqual(self.staff.session(token)['role'],'support')
        self.staff.update(helper['id'],active=False,actor='owner')
        self.assertIsNone(self.staff.session(token))
        self.assertIsNone(self.staff.authenticate('helper','helper-password'))
        with self.assertRaises(ValueError):self.staff.create_session(helper['id'])
        self.staff.update(helper['id'],active=True,actor='owner')
        token = self.staff.create_session(helper['id'])
        self.staff.update(helper['id'],role='manager',actor='owner')   # role change: sign in again
        self.assertIsNone(self.staff.session(token))

    def test_last_owner_cannot_be_removed(self):
        second = self.staff.create('second','second-owner-pw','owner','owner')
        staff._ENV_OWNERS.clear()
        with self.store.tx() as db:db.execute('UPDATE staff_users SET env_owner=0')
        self.staff.update(second['id'],role='manager',actor='owner')
        with self.assertRaisesRegex(ValueError,'последнего владельца'):
            self.staff.update(self.owner['id'],active=False,actor='owner')
        with self.assertRaisesRegex(ValueError,'последнего владельца'):
            self.staff.update(self.owner['id'],role='support',actor='owner')

    def test_sessions_hashed_touch_idle_absolute_and_revoke(self):
        token = self.staff.create_session(self.owner['id'],'10.0.0.1','Browser/1.0')
        self.assertGreaterEqual(len(token),40)
        with self.store.read() as db:
            row = db.execute('SELECT * FROM staff_sessions').fetchone()
        self.assertEqual(row['token_hash'],hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual((row['ip'],row['user_agent']),('10.0.0.1','Browser/1.0'))
        info = self.staff.session(token)
        self.assertEqual(info['username'],'owner');self.assertIn('staff',info['permissions'])
        self.assertIsNone(self.staff.session('x'*43));self.assertIsNone(self.staff.session(None));self.assertIsNone(self.staff.session(''))
        # last_seen is written at most once a minute.
        with self.store.tx() as db:db.execute('UPDATE staff_sessions SET last_seen=last_seen-30')
        before = self.last_seen()
        self.staff.session(token)
        self.assertEqual(self.last_seen(),before)
        with self.store.tx() as db:db.execute('UPDATE staff_sessions SET last_seen=last_seen-60')
        self.staff.session(token)
        self.assertGreater(self.last_seen(),before)
        # Idle expiry.
        with self.store.tx() as db:db.execute('UPDATE staff_sessions SET last_seen=?',(time.time()-IDLE_SECONDS-1,))
        self.assertIsNone(self.staff.session(token))
        self.assertIsNone(self.last_seen())   # expired rows are removed
        # Absolute expiry even when active.
        token = self.staff.create_session(self.owner['id'])
        with self.store.tx() as db:db.execute('UPDATE staff_sessions SET expires=?',(time.time()-1,))
        self.assertIsNone(self.staff.session(token))
        # Revoke one / all.
        first,second = self.staff.create_session(self.owner['id']),self.staff.create_session(self.owner['id'])
        self.assertTrue(self.staff.revoke_session(first));self.assertFalse(self.staff.revoke_session(first))
        self.assertIsNone(self.staff.session(first));self.assertTrue(self.staff.session(second))
        self.assertEqual(self.staff.list()[0]['sessions'],1)
        self.assertEqual(self.staff.revoke_all(self.owner['id']),1)
        self.assertIsNone(self.staff.session(second))

    def test_password_change_revokes_sessions_and_session_cap(self):
        tokens = [self.staff.create_session(self.owner['id']) for _ in range(staff.MAX_SESSIONS_PER_USER+3)]
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM staff_sessions').fetchone()[0],staff.MAX_SESSIONS_PER_USER)
        self.assertTrue(self.staff.session(tokens[-1]))
        self.staff.update(self.owner['id'],password='new-owner-password',actor='owner')
        self.assertIsNone(self.staff.session(tokens[-1]))

    def last_seen(self):
        with self.store.read() as db:
            row = db.execute('SELECT last_seen FROM staff_sessions').fetchone()
        return row[0] if row else None


if __name__=='__main__':unittest.main()
