from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from cryptography.fernet import Fernet
from backups import BackupService, inspect_database, restore_archive
from store import Store


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.key=Fernet.generate_key()
        self.store=Store(self.root/'shop.sqlite3',self.key)
        self.service=BackupService(self.store.path,self.key,interval_hours=24)
        self.store.set_currency('100','test')
        self.pid=self.store.save_product(dict(category='Test',name='License',description='',price=125,
            mode='auto',instruction='Activate',active=True))
        self.store.import_stock(self.pid,'sold-one\nsold-two\nreserved-secret\nfree-secret')
        self.store.adjust_wallet(42,1000,'Test','credit','tester')
        self.order=self.store.create_order(self.pid,42,2)
        self.store.pay_wallet(self.order['id'],42)
        self.reserved=self.store.create_order(self.pid,99)
        self.store.save_payment_method('ton',True,'0:'+'1'*64,'private-api-key','test')

    def tearDown(self):self.temp.cleanup()

    def create(self):
        result=self.service.create()
        return self.service.directory/result['name']

    def test_encrypted_bundle_restores_balances_purchases_stock_and_keys(self):
        archive=self.create()
        raw=archive.read_bytes()
        for secret in (self.key,b'private-api-key',b'free-secret',b'SQLite format',b'License'):
            self.assertNotIn(secret,raw)
        before=inspect_database(self.store.path,self.key)
        report=self.service.verify(archive.name)
        self.assertTrue(report['ok'])
        self.assertEqual(report['balance_total'],750)
        self.assertEqual(report['tables']['stock'],4)
        self.assertEqual(report['stock_free'],1)
        self.assertEqual(report['purchases'],1)
        output=self.root/'recovered'
        restored_report=restore_archive(archive,self.service.key_file,output)
        self.assertEqual(restored_report,before)
        recovered_key=dict(line.split('=',1) for line in (output/'recovered.env').read_text().splitlines())['STOCK_KEY']
        restored=Store(output/'data'/'shop.sqlite3',recovered_key)
        self.assertEqual(restored.wallet(42)['balance'],750)
        self.assertEqual(restored.purchases(42)['total'],1)
        self.assertIn('sold-one',restored.purchase_delivery(42,self.order['id']))
        self.assertEqual(restored.payment_methods(private=True)['networks'][1]['api_key'],'private-api-key')
        with restored.tx() as db:
            self.assertEqual(restored.decrypt(db.execute('SELECT secret FROM stock WHERE order_id IS NULL').fetchone()[0]),'free-secret')
        self.assertEqual(inspect_database(self.store.path,self.key),before)
        self.assertEqual(list(self.service.directory.glob('.verify-*')),[])
        self.assertEqual(list(self.service.directory.glob('.snapshot-*')),[])

    def test_snapshot_contains_committed_wal_changes(self):
        with closing(sqlite3.connect(self.store.path)) as keeper:
            keeper.execute('PRAGMA journal_mode=WAL')
            self.store.adjust_wallet(42,333,'WAL write','wal-credit','tester')
            self.assertTrue(Path(self.store.path+'-wal').exists())
            archive=self.create()
            self.assertEqual(self.service.verify(archive.name)['balance_total'],1083)

    def test_tampering_and_wrong_key_fail_without_creating_restore(self):
        archive=self.create()
        wrong=self.root/'wrong.key';wrong.write_bytes(Fernet.generate_key())
        with self.assertRaises(ValueError):restore_archive(archive,wrong,self.root/'wrong-output')
        self.assertFalse((self.root/'wrong-output').exists())
        raw=bytearray(archive.read_bytes());raw[len(raw)//2]^=1;archive.write_bytes(raw)
        with self.assertRaises(ValueError):self.service.verify(archive.name)
        with self.assertRaises(ValueError):restore_archive(archive,self.service.key_file,self.root/'bad-output')
        self.assertFalse((self.root/'bad-output').exists())

    def test_existing_directory_and_live_database_never_overwritten(self):
        archive=self.create()
        before=Path(self.store.path).read_bytes()
        with self.assertRaises(ValueError):restore_archive(archive,self.service.key_file,self.root)
        self.assertEqual(Path(self.store.path).read_bytes(),before)

    def test_missing_recovery_key_does_not_silently_regenerate(self):
        archive=self.create()
        saved=self.service.key_file.read_bytes()
        self.service.key_file.unlink()
        with self.assertRaises(ValueError):self.service.create()
        self.assertFalse(self.service.key_file.exists())
        self.service.key_file.write_bytes(saved)
        self.assertTrue(self.service.verify(archive.name)['ok'])

    def test_bad_stock_key_or_corrupt_secret_prevents_publishing(self):
        wrong=BackupService(self.store.path,Fernet.generate_key(),interval_hours=0)
        with self.assertRaises(ValueError):wrong.create()
        self.assertEqual(self.service.catalog()['items'],[])
        with self.store.tx() as db:db.execute("UPDATE stock SET secret=X'0001' WHERE order_id IS NULL")
        with self.assertRaises(ValueError):self.service.create()
        self.assertEqual(self.service.catalog()['items'],[])

    def test_schedule_and_locking(self):
        self.assertTrue(self.service.create(only_if_due=True))
        self.assertIsNone(self.service.create(only_if_due=True))
        self.assertEqual(len(self.service.catalog()['items']),1)
        disabled=BackupService(self.store.path,self.key,interval_hours=0)
        self.assertIsNone(disabled.create(only_if_due=True))
        with self.service.locked():
            with self.assertRaises(ValueError):self.service.create()
        self.assertTrue(self.service.create())

    def test_rebrand_keeps_legacy_backups_and_original_recovery_key(self):
        archive=self.create()
        self.assertTrue(archive.name.startswith('themarket-'))
        recovery_key=self.service.key_file.read_bytes()
        legacy=archive.with_name(archive.name.replace('themarket-','telemarket-',1))
        archive.rename(legacy)
        self.assertTrue(self.service.verify(legacy.name)['ok'])
        current=self.create()
        self.assertEqual(self.service.key_file.read_bytes(),recovery_key)
        self.assertTrue(self.service.verify(current.name)['ok'])
        os.utime(legacy,(current.stat().st_mtime+10,current.stat().st_mtime+10))
        self.assertEqual(self.service.catalog()['items'][0]['name'],legacy.name)
        self.assertIsNone(self.service.create(only_if_due=True))

    def test_names_and_key_location_are_restricted(self):
        self.create()
        for name in ('../shop.sqlite3','shop.sqlite3','/etc/passwd'):
            with self.assertRaises(ValueError):self.service.verify(name)
        with self.assertRaises(ValueError):
            BackupService(self.store.path,self.key,key_file=self.service.directory/'key')

    def test_restore_rejects_extra_members_even_with_valid_encryption(self):
        archive=self.create()
        key=self.service.key_file.read_bytes().strip()
        payload=io.BytesIO(Fernet(key).decrypt(archive.read_bytes()))
        with ZipFile(payload,'a') as bundle:bundle.writestr('../unexpected','content')
        archive.write_bytes(Fernet(key).encrypt(payload.getvalue()))
        with self.assertRaises(ValueError):self.service.verify(archive.name)

    def test_manifest_mismatch_detects_incomplete_restore(self):
        archive=self.create()
        key=self.service.key_file.read_bytes().strip()
        original=ZipFile(io.BytesIO(Fernet(key).decrypt(archive.read_bytes())))
        payload=io.BytesIO()
        with original,ZipFile(payload,'w') as bundle:
            for name in original.namelist():
                value=original.read(name)
                if name=='manifest.json':
                    manifest=json.loads(value);manifest['report']['balance_total']+=1
                    value=json.dumps(manifest).encode()
                bundle.writestr(name,value)
        archive.write_bytes(Fernet(key).encrypt(payload.getvalue()))
        with self.assertRaises(ValueError):self.service.verify(archive.name)


    def photo(self,name,data=b'\xff\xd8jpeg-bytes'):
        folder=self.root/'product-images';folder.mkdir(exist_ok=True)
        (folder/name).write_bytes(data)
        return folder/name

    def test_product_photos_are_backed_up_verified_and_restored(self):
        first=self.photo('%d-%s.jpg'%(self.pid,'a'*8),b'\xff\xd8first')
        self.photo('%d-%s.jpg'%(self.pid,'b'*8),b'\xff\xd8second')
        self.photo('notes.txt',b'not a photo')
        (self.root/'product-images'/('%d-%s.jpg'%(self.pid,'c'*8))).symlink_to(self.root/'shop.sqlite3')
        with patch('backups.IMAGE_MAX_BYTES',100):
            self.photo('%d-%s.jpg'%(self.pid,'d'*8),b'x'*101)  # oversize: skipped
            archive=self.create()
        self.assertEqual(self.service.verify(archive.name,full=True)['images'],2)
        key=self.service.key_file.read_bytes().strip()
        with ZipFile(io.BytesIO(Fernet(key).decrypt(archive.read_bytes()))) as bundle:
            self.assertEqual(sorted(n for n in bundle.namelist() if n.startswith('product-images/')),
                             ['product-images/%d-%s.jpg'%(self.pid,'a'*8),'product-images/%d-%s.jpg'%(self.pid,'b'*8)])
            self.assertEqual(json.loads(bundle.read('manifest.json'))['images_skipped'],1)
        output=self.root/'restored'
        restore_archive(archive,self.service.key_file,output)
        restored=output/'data'/'product-images'
        self.assertEqual(sorted(p.name for p in restored.iterdir()),[first.name,'%d-%s.jpg'%(self.pid,'b'*8)])
        self.assertEqual((restored/first.name).read_bytes(),b'\xff\xd8first')

    def rewrite(self,archive,change):
        key=self.service.key_file.read_bytes().strip()
        original=ZipFile(io.BytesIO(Fernet(key).decrypt(archive.read_bytes())))
        payload=io.BytesIO()
        with original,ZipFile(payload,'w') as bundle:
            for name in original.namelist():
                result=change(name,original.read(name))
                if result is not None:bundle.writestr(*result)
        archive.write_bytes(Fernet(key).encrypt(payload.getvalue()))

    def test_tampered_or_missing_photo_fails_verification(self):
        name='%d-%s.jpg'%(self.pid,'a'*8);self.photo(name)
        cases=(lambda n,v:(n,b'tampered') if n.startswith('product-images/') else (n,v),
               lambda n,v:None if n.startswith('product-images/') else (n,v),
               lambda n,v:('product-images/../evil.jpg',v) if n.startswith('product-images/') else (n,v))
        for change in cases:
            archive=self.create()
            self.rewrite(archive,change)
            with self.subTest(change=change),self.assertRaises(ValueError):self.service.verify(archive.name,full=True)
            self.assertFalse((self.root/('restore-'+archive.name)).exists())

    def test_old_archives_without_photos_still_restore(self):
        archive=self.create()
        def legacy(name,value):
            if name.startswith('product-images/'):return None
            if name=='manifest.json':
                manifest=json.loads(value);manifest['format']=1;manifest.pop('images');manifest.pop('images_skipped')
                value=json.dumps(manifest).encode()
            return name,value
        self.photo('%d-%s.jpg'%(self.pid,'a'*8))
        self.rewrite(archive,legacy)
        self.assertEqual(self.service.verify(archive.name,full=True)['images'],0)
        restore_archive(archive,self.service.key_file,self.root/'legacy')
        self.assertTrue((self.root/'legacy'/'data'/'shop.sqlite3').is_file())
        self.assertFalse((self.root/'legacy'/'data'/'product-images').exists())

    def age(self,path,seconds):
        stamp=path.stat().st_mtime-seconds;os.utime(path,(stamp,stamp))

    def test_retention_keeps_newest_verified_archives(self):
        service=BackupService(self.store.path,self.key,interval_hours=24,keep=2)
        names=[]
        for index in range(4):
            names.append(service.create()['name'])
            for path in service.directory.glob("*.tmbak"):
                if path.name!=names[-1]:self.age(path,10)
        remaining=[item['name'] for item in service.catalog()['items']]
        self.assertEqual(remaining,names[::-1][:2])
        self.assertTrue(all(item['verified'] for item in service.catalog()['items']))
        self.assertEqual(sorted(p.name for p in service.directory.glob('*.json')),sorted(n+'.json' for n in remaining))

    def test_retention_never_deletes_newest_verified_for_unverified_newer_files(self):
        service=BackupService(self.store.path,self.key,interval_hours=24,keep=1)
        first=service.create()['name']
        self.age(service.directory/first,100)
        second=service.directory/service.create()['name']
        self.assertEqual([i['name'] for i in service.catalog()['items']],[second.name])
        # A newer archive without a verification record (e.g. copied in) does not count as verified.
        (service.directory/(second.name+'.json')).unlink()
        self.age(second,50)
        third=service.create()['name']
        self.assertEqual({i['name'] for i in service.catalog()['items']},{third})
        self.assertNotIn(first,[i['name'] for i in service.catalog()['items']])

    def test_download_verification_reuses_record_and_detects_changes(self):
        archive=self.create()
        with patch('backups.unpack',side_effect=AssertionError('decrypted again')):
            self.assertTrue(self.service.verify(archive.name)['ok'])
            self.service.recovery_key();self.service.recovery_key()
        with patch('backups.unpack',wraps=__import__('backups').unpack) as unpack:
            self.assertTrue(self.service.verify(archive.name,full=True)['ok'])
            self.assertEqual(unpack.call_count,1)
        raw=bytearray(archive.read_bytes());raw[-5]^=1;archive.write_bytes(raw)
        with self.assertRaises(ValueError):self.service.verify(archive.name)
        self.assertFalse(self.service.catalog()['items'][0]['verified'])

    def test_stale_temp_folders_removed_at_startup(self):
        self.create()
        for name in ('.snapshot-crashed','.verify-crashed'):
            (self.service.directory/name).mkdir();(self.service.directory/name/'shop.sqlite3').write_bytes(b'x')
        (self.service.directory/'themarket-20200101T000000Z-deadbeef.tmbak.json').write_text('{}')
        BackupService(self.store.path,self.key)
        self.assertEqual(list(self.service.directory.glob('.snapshot-*'))+list(self.service.directory.glob('.verify-*')),[])
        self.assertFalse((self.service.directory/'themarket-20200101T000000Z-deadbeef.tmbak.json').exists())
        with self.service.locked():self.assertEqual(self.service.cleanup(),0)

    def test_low_disk_space_refuses_backup(self):
        from collections import namedtuple
        usage=namedtuple('usage','total used free')
        with patch('backups.shutil.disk_usage',return_value=usage(10,10,1024)):
            with self.assertRaisesRegex(ValueError,'места'):self.service.create()
        self.assertEqual(self.service.catalog()['items'],[])

    def test_archives_and_records_are_private(self):
        if os.name=='nt':self.skipTest('POSIX permissions')
        archive=self.create()
        self.assertEqual(archive.stat().st_mode&0o777,0o600)
        self.assertEqual((archive.parent/(archive.name+'.json')).stat().st_mode&0o777,0o600)

    def test_keep_setting_validated(self):
        with patch.dict(os.environ,{'BACKUP_KEEP':'0'}),self.assertRaises(ValueError):BackupService(self.store.path,self.key)
        with patch.dict(os.environ,{'BACKUP_KEEP':'5'}):self.assertEqual(BackupService(self.store.path,self.key).keep,5)

    def test_feature_tables_backed_up_and_encrypted_columns_checked(self):
        import notify, promo, staff, support
        with self.store.tx() as db:
            for module in (promo,support,staff,notify):module.initialize(db)
        notify.Notify(self.store).save({'admin_chat_ids':[1]},'owner')
        ticket=support.Support(self.store).open_or_append(42,'secret support text')['ticket_id']
        support.Support(self.store).reply(ticket,'staff answer','owner')
        promo.Promos(self.store).create(dict(code='SALE',kind='percent',value=10),'owner')
        staff.Staff(self.store).create('helper','helper-password','support','owner')
        report=inspect_database(self.store.path,self.key)
        for table in ('support_tickets','support_messages','support_outbox','notify_queue','promo_codes','staff_users'):
            self.assertGreaterEqual(report['tables'][table],1)
        archive=self.create()
        self.assertNotIn(b'secret support text',archive.read_bytes())
        self.assertEqual(self.service.verify(archive.name,full=True)['tables']['support_messages'],2)
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE support_messages SET body=? WHERE id=1",(Fernet(Fernet.generate_key()).encrypt(b'x'),));db.commit()
        with self.assertRaisesRegex(ValueError,'расшифровать'):inspect_database(self.store.path,self.key)
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE support_messages SET body=? WHERE id=1",(Fernet(self.key).encrypt(b'ok'),))
            db.execute("UPDATE notify_queue SET body=X'00'");db.commit()
        with self.assertRaisesRegex(ValueError,'расшифровать'):inspect_database(self.store.path,self.key)

if __name__=='__main__':unittest.main()
