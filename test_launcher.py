import os
import base64
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch

from cryptography.fernet import Fernet
from manager import InstanceLock, ROOT, Services, read_config, save_config


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def test_settings_preserve_keys_and_database(self):
        (self.root/'.env').write_text('STOCK_KEY=keep-this-key\nADMIN_PASSWORD=keep-password\nDATABASE_PATH=data/existing.db\nBOT_TOKEN=old\n',encoding='utf-8')
        save_config({'BOT_TOKEN':' new-token ','SHOP_TERMS':'line1\nline2'},self.root)
        config=read_config(self.root)
        self.assertEqual(config['STOCK_KEY'],'keep-this-key')
        self.assertEqual(config['ADMIN_PASSWORD'],'keep-password')
        self.assertEqual(config['DATABASE_PATH'],'data/existing.db')
        self.assertEqual(config['SHOP_TERMS'],'line1 line2')

    def test_duplicate_instance_rejected_and_lock_released(self):
        a=InstanceLock(self.root/'test.lock').acquire()
        try:
            with self.assertRaises(RuntimeError):InstanceLock(self.root/'test.lock').acquire()
        finally:a.release()
        b=InstanceLock(self.root/'test.lock').acquire();b.release()

    def test_logs_redact_credentials(self):
        services=Services(sys.executable,lambda *_:None)
        services.config={'BOT_TOKEN':'private-token','ADMIN_PASSWORD':'private-password','STOCK_KEY':'private-key'}
        self.assertEqual(services.clean('private-token private-password private-key'),'[скрыто] [скрыто] [скрыто]')

    def test_missing_bot_settings_fail_before_launching(self):
        services=Services(sys.executable,lambda *_:None,self.root)
        with self.assertRaises(ValueError):services.start()
        self.assertEqual(services.children,{})

    @unittest.skipIf(os.getenv('TELEMARKET_HTTP_TEST')=='0','TELEMARKET_HTTP_TEST=0 skips the local server integration test')
    def test_start_panel_http_and_stop(self):
        for path in [*ROOT.glob('*.py'),ROOT/'translations_en.json']:
            if not path.name.startswith(('test_','loadtest_')):shutil.copy(path,self.root/path.name)
        shutil.copytree(ROOT/'web',self.root/'web')
        config={'ADMIN_USER':'tester','ADMIN_PASSWORD':'temporary-long-test-password',
                'STOCK_KEY':Fernet.generate_key().decode(),'DATABASE_PATH':str(self.root/'shop.db'),'PORT':'18080'}
        (self.root/'.env').write_text(''.join(k+'='+v+'\n' for k,v in config.items()),encoding='utf-8')
        events=[]
        services=Services(sys.executable,lambda *x:events.append(x),self.root)
        try:
            with patch.dict(os.environ,config):services.start(with_bot=False)
            child=services.children['panel']
            self.assertIsNone(child.poll())
            self.assertTrue(any(k=='url' for k,v in events))
            import http.cookiejar
            jar=http.cookiejar.CookieJar()
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(jar))
            anonymous=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            # The launcher reports ready without credentials; staff then sign in on the login page.
            login=urllib.request.Request(services.url+'/api/login',method='POST',
                data=json.dumps({'username':config['ADMIN_USER'],'password':config['ADMIN_PASSWORD']}).encode(),
                headers={'Content-Type':'application/json','Origin':services.url})
            with opener.open(login,timeout=5) as response:self.assertTrue(json.load(response)['ok'])
            with opener.open(services.url+'/login',timeout=5) as response:self.assertEqual(response.status,200)
            def request(path,body=None,method=None):
                headers={'Content-Type':'application/json','Origin':services.url}
                req=urllib.request.Request(services.url+'/api/'+path,data=json.dumps(body).encode() if body else None,headers=headers,method=method)
                with opener.open(req,timeout=5) as response:return json.load(response)
            snapshot=request('state')
            self.assertIn('metrics',snapshot)
            self.assertIn('me',snapshot)
            gate=request('channel-gate');self.assertFalse(gate['enabled'])
            request('channel-gate',{'enabled':False,'channel':'@TheMarketFresh'},method='PUT')
            self.assertEqual(request('channel-gate')['channel_ref'],'@TheMarketFresh')
            self.assertEqual(request('channel-gate')['join_url'],'https://t.me/TheMarketFresh')
            with self.assertRaises(urllib.error.HTTPError) as error:
                request('channel-gate',{'enabled':False,'channel':'https://evil.example/x'},method='PUT')
            self.assertEqual(error.exception.code,400);error.exception.close()
            self.assertEqual(request('channel-gate')['channel_ref'],'@TheMarketFresh')
            # A bot-side DB write must become visible to the already running panel.
            from store import Store
            bot_store=Store(config['DATABASE_PATH'],config['STOCK_KEY'])
            bot_store.remember_customer(1234567,'new_visitor','New','Visitor')
            snapshot=request('state')
            visitor=next(c for c in request('customers?query=new_visitor')['items'] if c['user_id']==1234567)
            self.assertEqual(visitor['username'],'new_visitor')
            self.assertEqual(visitor['balance'],0)
            news=request('broadcasts')
            self.assertGreaterEqual(news['audience'],1)
            self.assertTrue(news['settings']['new_product'])
            request('broadcasts/settings',{'new_product':False,'restock':True},method='PUT')
            self.assertFalse(request('broadcasts')['settings']['new_product'])
            data={'reference':'99999999-9999-4999-8999-999999999999','title':'HTTP campaign','text':'Test only','target':'catalog'}
            draft=request('broadcasts',data)
            self.assertEqual(draft,request('broadcasts',data))
            detail=request('broadcasts/'+draft['id'])
            self.assertEqual(detail['status'],'draft');self.assertEqual(detail['previews']['ru']['text'],'Test only')
            data['text']='Edited test draft'
            request('broadcasts/'+draft['id'],data,method='PUT')
            self.assertEqual(request('broadcasts/'+draft['id'])['previews']['ru']['text'],'Edited test draft')
            request('broadcasts/'+draft['id']+'/start',method='POST')
            self.assertGreaterEqual(request('broadcasts/'+draft['id'])['counts']['total'],1)
            request('broadcasts/'+draft['id']+'/pause',method='POST')
            self.assertEqual(request('broadcasts/'+draft['id'])['status'],'paused')
            request('broadcasts/'+draft['id']+'/cancel',method='POST')
            self.assertEqual(request('broadcasts/'+draft['id'])['status'],'cancelled')
            self.assertEqual(visitor['orders'],0)
            bot_store.save_emoji_pack('Brands','Brand icons',[{'id':'12345','emoji':'🎬','file_id':'secret-file-id'}],'test')
            library=request('emojis')
            self.assertEqual(library['packs'][0]['items'][0]['id'],'12345')
            self.assertNotIn('file_id',library['packs'][0]['items'][0])
            request('emojis/assign',{'target':'menu:catalog','emoji_id':'12345'})
            self.assertEqual(request('emojis')['assignments']['menu:catalog'],'12345')
            self.assertIn('welcome:greeting',library['defaults'])
            request('emojis/assign',{'target':'welcome:greeting','emoji_id':'12345'})
            self.assertEqual(request('emojis')['assignments']['welcome:greeting'],'12345')
            data={'delta':25,'reason':'Integration test','reference':'11111111-1111-1111-1111-111111111111'}
            self.assertEqual(request('customers/42/wallet',data)['wallet']['balance'],25)
            self.assertEqual(request('customers/42/wallet',data)['wallet']['balance'],25)
            self.assertEqual(request('customers/42')['wallet']['balance'],25)
            with self.assertRaises(urllib.error.HTTPError) as error:
                request('customers/42/wallet',{'delta':-26,'reason':'Overdraft','reference':'22222222-2222-2222-2222-222222222222'})
            self.assertEqual(error.exception.code,400)
            error.exception.close()
            request('currency',{'stars_per_usd':'100'})
            created=request('products',{'category':'Test','name':'USD item','price':'2.50','mode':'manual'})
            snapshot=request('state')
            self.assertTrue(snapshot['currency']['active'])
            self.assertEqual(next(p for p in request('products') if p['id']==created['id'])['price'],250)
            self.assertTrue(request('products/'+str(created['id']),method='DELETE')['ok'])
            self.assertFalse(any(p['id']==created['id'] for p in request('products')))
            event_result=request('events?category=products')
            self.assertTrue(any(e['action']=='product.delete' for e in event_result['items']))
            req=urllib.request.Request(services.url+'/api/events/export?category=products')
            with opener.open(req,timeout=5) as response:
                exported=response.read().decode('utf-8-sig')
            self.assertIn('Товар удалён',exported)
            request('payment-methods/ton',{'enabled':True,'wallet':'0:'+'1'*64,'api_key':'private-test-api-key'},method='PUT')
            methods=request('payment-methods')
            ton=next(item for item in methods['networks'] if item['network']=='ton')
            self.assertTrue(ton['enabled']);self.assertTrue(ton['has_api_key'])
            self.assertNotIn('private-test-api-key',json.dumps(methods))
            deposit=bot_store.create_crypto_deposit(42,500,network='ton')
            self.assertEqual(deposit['network'],'ton')
            for index in range(5):bot_store.crypto_input_failure(deposit['id'],42,'http-attempt-'+str(index),'Invalid TXID')
            cancelled=next(item for item in request('payments')['crypto'] if item['id']==deposit['id'])
            self.assertTrue(cancelled['cancelled'])
            self.assertEqual(cancelled['failed_attempts'],5)
            result=request('customers/42/wallet',{'delta':'0.25','reason':'USD cents','reference':'33333333-3333-3333-3333-333333333333'})
            self.assertEqual(result['wallet']['balance'],50)
            result=request('customers/42/balance',{'amount':'123456.78','reason':'Exact target','reference':'44444444-4444-4444-4444-444444444444'})
            self.assertEqual(result['wallet']['balance'],12345678)
            result=request('customers/42/balance',{'amount':'0','reason':'Reset','reference':'55555555-5555-5555-5555-555555555555'})
            self.assertEqual(result['wallet']['balance'],0)
            from datetime import datetime
            from analytics import MSK
            product_data={'category':'Test','name':'=Formula item','price':'2.50','mode':'auto','cost_price':'1.00'}
            finance_product=request('products',product_data)['id']
            request('products/'+str(finance_product)+'/stock',{'lines':'first-finance-secret','unit_cost':'0.80'})
            request('products/'+str(finance_product)+'/stock',{'lines':'second-finance-secret'})
            bot_store.adjust_wallet(42,1000,'Analytics test','analytics-credit','test')
            sale=bot_store.create_order(finance_product,42,2);bot_store.pay_wallet(sale['id'],42)
            summary=request('analytics?period=day')['summary']
            self.assertEqual((summary['revenue'],summary['cost'],summary['profit']),(500,180,320))
            base_entry={'reference':'66666666-6666-4666-8666-666666666666','kind':'expense','category':'server','amount':'0.50',
                'note':'=Unsafe spreadsheet formula','occurred_date':datetime.now(MSK).date().isoformat()}
            request('analytics/entries',base_entry)
            request('analytics/entries',base_entry)
            self.assertEqual(request('analytics?period=day')['summary']['profit'],270)
            request('orders/'+sale['id']+'/cost',{'amount':'2.00','reason':'Invoice correction'},method='PUT')
            self.assertEqual(request('analytics?period=day')['summary']['profit'],250)
            req=urllib.request.Request(services.url+'/api/analytics/export?period=day')
            with opener.open(req,timeout=5) as response:csv_text=response.read().decode('utf-8-sig')
            self.assertIn("'=Formula item",csv_text)
            self.assertIn("'=Unsafe spreadsheet formula",csv_text)
            from io import BytesIO
            from zipfile import ZipFile
            import xml.etree.ElementTree as ET
            req=urllib.request.Request(services.url+'/api/analytics/export.xlsx?period=day')
            with opener.open(req,timeout=5) as response:
                self.assertEqual(response.headers['Content-Type'],'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                self.assertIn('.xlsx',response.headers['Content-Disposition'])
                self.assertEqual(response.headers['Cache-Control'],'no-store')
                excel_bytes=response.read()
            with ZipFile(BytesIO(excel_bytes)) as workbook:
                sales_xml=ET.fromstring(workbook.read('xl/worksheets/sheet3.xml'))
                self.assertIn('=Formula item',''.join(sales_xml.itertext()))
                self.assertNotIn(b'<f>',workbook.read('xl/worksheets/sheet3.xml'))
            with self.assertRaises(urllib.error.HTTPError) as error:
                anonymous.open(services.url+'/api/analytics/export.xlsx?period=day',timeout=5)
            self.assertEqual(error.exception.code,401);error.exception.close()
            request('analytics/entries/'+base_entry['reference']+'/void',{'reason':'Wrong expense'})
            self.assertEqual(request('analytics?period=day')['summary']['profit'],300)
            created_backup=request('backups',method='POST')
            self.assertTrue(created_backup['ok'])
            name=created_backup['name']
            self.assertIn(name,[item['name'] for item in request('backups')['items']])
            self.assertEqual(request('backups/'+name+'/verify',method='POST')['digest'],created_backup['digest'])
            def download(path):
                req=urllib.request.Request(services.url+'/api/'+path,data=b'',method='POST',headers={'Origin':services.url})
                with opener.open(req,timeout=10) as response:
                    self.assertEqual(response.headers['Cache-Control'],'no-store')
                    return response.read()
            recovery_key=download('backups/recovery-key')
            self.assertNotEqual(recovery_key.strip().decode(),config['STOCK_KEY'])
            saved_key=self.root/'downloaded-recovery.key';saved_key.write_bytes(recovery_key)
            saved_archive=self.root/'downloaded.tmbak';saved_archive.write_bytes(download('backups/'+name+'/download'))
            from backups import restore_archive
            restored=restore_archive(saved_archive,saved_key,self.root/'downloaded-restore')
            self.assertEqual(restored['digest'],created_backup['digest'])
            with self.assertRaises(urllib.error.HTTPError) as error:
                anonymous.open(urllib.request.Request(services.url+'/api/backups/recovery-key',data=b'',method='POST',headers={'Origin':services.url}),timeout=5)
            self.assertEqual(error.exception.code,401);error.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as error:
                request('backups/invalid/verify',method='POST')
            self.assertEqual(error.exception.code,400);error.exception.close()
        finally:services.stop()
        self.assertIsNotNone(child.poll())


    @unittest.skipIf(os.name=='nt','POSIX permissions')
    def test_env_written_private_and_atomically(self):
        (self.root/'.env').write_text('STOCK_KEY=k\nADMIN_PASSWORD=p\n',encoding='utf-8');os.chmod(self.root/'.env',0o644)
        save_config({'BOT_TOKEN':'123'},self.root)
        self.assertEqual((self.root/'.env').stat().st_mode&0o777,0o600)
        self.assertFalse((self.root/'.env.pending').exists())

    def test_old_env_without_admin_password_is_repaired_without_touching_stock_key(self):
        from manager import ensure_config
        (self.root/'.env').write_text('STOCK_KEY=keep-this\nBOT_TOKEN=x\n',encoding='utf-8')
        config=ensure_config(self.root)
        self.assertGreaterEqual(len(config['ADMIN_PASSWORD']),16);self.assertEqual(config['ADMIN_USER'],'owner')
        self.assertEqual(read_config(self.root),config);self.assertEqual(config['STOCK_KEY'],'keep-this')
        self.assertEqual(ensure_config(self.root),config)

    def test_start_without_admin_password_is_a_clear_error(self):
        (self.root/'.env').write_text('STOCK_KEY=k\n',encoding='utf-8')
        services=Services(sys.executable,lambda *_:None,self.root)
        with self.assertRaisesRegex(ValueError,'пароля'):services.start(with_bot=False)
        self.assertEqual(services.children,{})

    def test_instance_lock_error_type(self):
        from manager import InstanceBusy
        with InstanceLock(self.root/'x.lock'):
            with self.assertRaises(InstanceBusy):InstanceLock(self.root/'x.lock').acquire()

    def child_script(self,body):
        script=self.root/'child.py'
        script.write_text('import sys,time\nsys.path.insert(0,'+repr(str(ROOT))+')\nfrom manager import watch_parent\n'+body,encoding='utf-8')
        return script

    def test_stop_is_graceful_through_control_pipe(self):
        self.child_script('watch_parent(timeout=5)\nprint("started",flush=True)\n'
            'try:\n    time.sleep(60)\nexcept KeyboardInterrupt:\n    print("graceful-exit",flush=True)\n')
        events=[];services=Services(sys.executable,lambda *x:events.append(x),self.root)
        child=services.launch('panel',['child.py'],os.environ.copy())
        for _ in range(100):
            if any('started' in str(v) for _,v in events):break
            time.sleep(.05)
        started=time.monotonic();services.stop(timeout=10)
        self.assertLess(time.monotonic()-started,8)
        self.assertEqual(child.returncode,0,events)
        for _ in range(40):
            if any('graceful-exit' in str(v) for _,v in events):break
            time.sleep(.05)
        self.assertTrue(any('graceful-exit' in str(v) for _,v in events),events)

    def test_child_exits_when_parent_pipe_breaks_even_if_it_ignores_sigint(self):
        import signal,subprocess
        script=self.child_script('import signal\nsignal.signal(signal.SIGINT,signal.SIG_IGN)\nwatch_parent(timeout=0.5)\ntime.sleep(60)\n')
        child=subprocess.Popen([sys.executable,str(script)],stdin=subprocess.PIPE)
        time.sleep(.3);child.stdin.close()
        self.assertEqual(child.wait(timeout=10),1)

    def test_run_redacts_secrets_and_maps_errors(self):
        import logging,run
        with patch.dict(os.environ,{'BOT_TOKEN':'123456:'+'A'*35,'ADMIN_PASSWORD':'very-secret-password'}):
            record=logging.LogRecord('x',logging.ERROR,'',0,'token %s pw %s other 999999:%s',(os.environ['BOT_TOKEN'],'very-secret-password','B'*40),None)
            run.Redact().filter(record)
            self.assertNotIn('AAAA',record.getMessage());self.assertNotIn('very-secret',record.getMessage());self.assertNotIn('BBBB',record.getMessage())
            with patch.dict(os.environ,{'SUPPORT_CONTACT':'','SHOP_TERMS':' '}):
                self.assertEqual(run.missing_settings('bot'),['STOCK_KEY','SUPPORT_CONTACT','SHOP_TERMS'] if not os.getenv('STOCK_KEY') else ['SUPPORT_CONTACT','SHOP_TERMS'])
        from manager import InstanceBusy
        self.assertIn('уже запущен',run.bot_error_message(InstanceBusy('x')))
        self.assertIn('SHOP_TERMS',run.bot_error_message(RuntimeError('Set SHOP_TERMS before starting the bot')))
        self.assertIn('Telegram',run.bot_error_message(type('TelegramNetworkError',(Exception,),{})()))

    def test_rotating_log_file_with_timestamps(self):
        import logging,run
        root=logging.getLogger();saved=list(root.handlers),root.level
        try:
            with patch.dict(os.environ,{'LOG_DIRECTORY':str(self.root/'logs'),'BOT_TOKEN':'654321:'+'C'*35}):
                run.setup_logging('bot')
                logging.getLogger('test').warning('hello %s',os.environ['BOT_TOKEN'])
                for handler in logging.getLogger().handlers:handler.flush()
                text=(self.root/'logs'/'bot.log').read_text(encoding='utf-8')
                file_handler=[h for h in root.handlers if hasattr(h,'maxBytes')][0]
                self.assertEqual((file_handler.maxBytes,file_handler.backupCount),(5*1024*1024,5))
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
                if handler not in saved[0]:handler.close()
            for handler in saved[0]:root.addHandler(handler)
            root.setLevel(saved[1])
        self.assertRegex(text,r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d');self.assertIn('hello [скрыто]',text);self.assertNotIn('CCCC',text)

    def test_ignore_files_cover_secrets_and_exports(self):
        gitignore=(ROOT/'.gitignore').read_text().split();dockerignore=(ROOT/'.dockerignore').read_text().split()
        for entry in ('.env','.env.pending','*.xlsx','*.csv','data/'):self.assertIn(entry,gitignore)
        for entry in ('.env','.env.pending','.git','test_*.py','*.xlsx','*.csv','data'):self.assertIn(entry,dockerignore)

    def test_launcher_open_folder_is_portable(self):
        try:import launcher
        except ImportError:self.skipTest('tkinter is not installed')
        with patch('launcher.webbrowser.open') as browser,patch.object(launcher.os,'name','posix'):
            launcher.open_folder(self.root)
        browser.assert_called_once_with(self.root.resolve().as_uri())

if __name__=='__main__':unittest.main(verbosity=2)
