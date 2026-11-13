import json
import tempfile
import time
import unittest

import support
from notify import Notify
from support import Support
from test_promo import open_store


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = open_store(self.temp.name)
        self.support = Support(self.store)
        self.store.remember_customer(42,'buyer','Ivan','Petrov')
        self.store.remember_customer(43,'other','Anna')

    def tearDown(self):self.temp.cleanup()

    def test_initialize_is_idempotent(self):
        with self.store.tx() as db:
            support.initialize(db);support.initialize(db)

    def test_one_open_ticket_per_user_and_encrypted_bodies(self):
        first = self.support.open_or_append(42,'  Не пришёл ключ  ')
        second = self.support.open_or_append(42,'Заказ 1234')
        self.assertTrue(first['new_ticket']);self.assertFalse(second['new_ticket'])
        self.assertEqual(first['ticket_id'],second['ticket_id'])
        photo = self.support.open_or_append(42,'',kind='photo',file_id='AgACAg-file')
        self.assertEqual(photo['ticket_id'],first['ticket_id'])
        with self.store.read() as db:
            raw = b''.join(row[0] for row in db.execute('SELECT body FROM support_messages'))
        self.assertNotIn('ключ'.encode(),raw);self.assertNotIn(b'1234',raw)
        thread = self.support.thread(first['ticket_id'])
        self.assertEqual([m['text'] for m in thread['messages']],['Не пришёл ключ','Заказ 1234',''])
        self.assertEqual(thread['messages'][2]['file_id'],'AgACAg-file')
        self.assertEqual(self.support.open_ticket(42),first['ticket_id'])
        self.support.close(first['ticket_id'],'owner')
        self.assertIsNone(self.support.open_ticket(42))
        again = self.support.open_or_append(42,'Снова я')
        self.assertTrue(again['new_ticket']);self.assertNotEqual(again['ticket_id'],first['ticket_id'])
        with self.assertRaisesRegex(ValueError,'уже есть открытое обращение'):self.support.reopen(first['ticket_id'],'owner')
        with self.assertRaisesRegex(ValueError,'уже закрыто'):self.support.close(first['ticket_id'],'owner')
        self.support.close(again['ticket_id'],'owner')
        self.assertEqual(self.support.reopen(first['ticket_id'],'owner')['status'],'open')
        self.assertEqual(self.support.thread(first['ticket_id'])['ticket']['tickets_total'],2)

    def test_validation(self):
        for text in ('',None,'   '):
            with self.assertRaisesRegex(ValueError,'Напишите текст'):self.support.open_or_append(42,text)
        with self.assertRaisesRegex(ValueError,'3500'):self.support.open_or_append(42,'я'*3501)
        self.support.open_or_append(42,'я'*3500)
        with self.assertRaisesRegex(ValueError,'вложение'):self.support.open_or_append(42,'фото',kind='photo')
        with self.assertRaisesRegex(ValueError,'вложение'):self.support.open_or_append(42,'x',kind='video',file_id='a')
        with self.assertRaises(ValueError):self.support.open_or_append(0,'x')
        with self.assertRaisesRegex(ValueError,'не найдено'):self.support.reply(999,'ответ','owner')
        ticket = self.support.open_ticket(42)
        with self.assertRaisesRegex(ValueError,'Напишите текст'):self.support.reply(ticket,' ','owner')

    def test_rate_limit_per_hour(self):
        for index in range(20):self.support.open_or_append(42,'msg '+str(index))
        with self.assertRaisesRegex(ValueError,'не более 20 в час'):self.support.open_or_append(42,'one more')
        self.support.open_or_append(43,'another customer is not limited')
        with self.store.tx() as db:
            db.execute('UPDATE support_messages SET created=created-3601 WHERE id IN (SELECT id FROM support_messages WHERE user_id=42 ORDER BY id LIMIT 5)')
        for index in range(5):self.support.open_or_append(42,'later '+str(index))
        with self.assertRaises(ValueError):self.support.open_or_append(42,'blocked again')
        # Staff replies do not count against the customer.
        self.support.reply(self.support.open_ticket(42),'ответ','owner')
        with self.assertRaises(ValueError):self.support.open_or_append(42,'still blocked')

    def test_reply_outbox_and_backoff(self):
        ticket = self.support.open_or_append(42,'Помогите')['ticket_id']
        self.store.set_language(42,'en')
        message = self.support.reply(ticket,'Ключ отправлен повторно','manager1')
        self.assertEqual((message['author'],message['actor'],message['text'],message['delivery']),
                         ('staff','manager1','Ключ отправлен повторно','pending'))
        due = self.support.outbox_due()
        self.assertEqual(len(due),1)
        self.assertEqual({k:due[0][k] for k in ('user_id','ticket_id','text','language')},
                         {'user_id':42,'ticket_id':ticket,'text':'Ключ отправлен повторно','language':'en'})
        self.assertEqual(self.support.outbox_result(due[0]['id'],False),'retry')
        self.assertEqual(self.support.outbox_due(),[])
        with self.store.tx() as db:
            row = db.execute('SELECT attempts,next_try FROM support_outbox').fetchone()
            self.assertEqual(row['attempts'],1);self.assertGreater(row['next_try'],time.time()+50)
            db.execute('UPDATE support_outbox SET next_try=0')
        self.assertEqual(self.support.outbox_result(due[0]['id'],True),'sent')
        self.assertIsNone(self.support.outbox_result(due[0]['id'],False))  # stale result ignored
        self.assertEqual(self.support.thread(ticket)['messages'][-1]['delivery'],'sent')
        second = self.support.reply(ticket,'Ещё ответ','manager1')
        item = self.support.outbox_due()[0]
        self.assertEqual(self.support.outbox_result(item['id'],False,permanent=True),'failed')
        self.assertEqual(self.support.thread(ticket)['messages'][-1]['delivery'],'failed')
        self.assertEqual(second['ticket_id'],ticket)
        third = self.support.reply(ticket,'Третий','manager1')
        with self.store.tx() as db:db.execute('UPDATE support_outbox SET attempts=28 WHERE message_id=?',(third['id'],))
        item = self.support.outbox_due()[0]
        self.assertEqual(self.support.outbox_result(item['id'],False),'failed')
        self.assertTrue(any(e['action']=='support.delivery_failed' for e in self.store.events(query='support')['items']))

    def test_inbox_listing_unread_and_search(self):
        first = self.support.open_or_append(42,'Первое длинное сообщение '+'x'*300)['ticket_id']
        self.support.open_or_append(42,'Второе')
        second = self.support.open_or_append(43,'Вопрос по оплате')['ticket_id']
        self.assertEqual(self.support.unread_count(),2)
        page = self.support.tickets()
        self.assertEqual(page['total'],2)
        self.assertEqual(page['items'][0]['id'],second)   # newest activity first
        mine = next(item for item in page['items'] if item['id']==first)
        self.assertEqual((mine['username'],mine['first_name'],mine['unread_for_staff'],mine['last_text']),('buyer','Ivan',2,'Второе'))
        self.support.open_or_append(42,'y'*400)
        self.assertLessEqual(len(self.support.ticket(first)['last_text']),121)
        self.support.mark_read(first,'owner')
        self.assertEqual(self.support.unread_count(),1)
        self.support.reply(second,'Ответ','owner')
        self.assertEqual(self.support.unread_count(),0)
        self.assertEqual(self.support.ticket(second)['assignee'],'owner')
        self.assertEqual(self.support.tickets(query='@buyer')['items'][0]['id'],first)
        self.assertEqual(self.support.tickets(query='Anna')['items'][0]['id'],second)
        self.assertEqual(self.support.tickets(query='#'+str(second))['total'],1)
        self.assertEqual(self.support.tickets(query='43')['total'],1)
        self.assertEqual(self.support.tickets(query='%')['total'],0)
        self.support.close(first,'owner')
        self.assertEqual(self.support.tickets('closed')['total'],1)
        self.assertEqual(self.support.tickets('all')['total'],2)
        self.assertEqual(self.support.tickets(page_size=1,page=5)['page'],1)
        with self.assertRaises(ValueError):self.support.tickets('weird')
        with self.assertRaises(ValueError):self.support.tickets(page_size=1000)
        self.assertEqual(self.support.assign(second,'owner','manager1')['assignee'],'manager1')
        self.assertIsNone(self.support.assign(second,'owner','')['assignee'])
        self.assertEqual(self.support.open_count(),1)

    def test_thread_pagination(self):
        ticket = self.support.open_or_append(42,'m0')['ticket_id']
        for index in range(1,15):self.support.open_or_append(42,'m'+str(index))
        page = self.support.thread(ticket,limit=10)
        self.assertTrue(page['has_more'])
        self.assertEqual([m['text'] for m in page['messages']],['m'+str(i) for i in range(5,15)])
        older = self.support.thread(ticket,before=page['messages'][0]['id'],limit=10)
        self.assertFalse(older['has_more'])
        self.assertEqual([m['text'] for m in older['messages']],['m'+str(i) for i in range(5)])
        with self.assertRaises(ValueError):self.support.thread(ticket,limit=1000)

    def test_new_messages_notify_staff_once_per_window(self):
        Notify(self.store).save({'admin_chat_ids':[777]},'owner')
        ticket = self.support.open_or_append(42,'Срочно')['ticket_id']
        self.support.open_or_append(42,'Ещё')
        self.support.open_or_append(43,'Другой')
        due = Notify(self.store).due()
        self.assertEqual(len(due),2)
        self.assertIn('Новое обращение #'+str(ticket),due[0]['text'])
        self.assertIn('@buyer',due[0]['text']);self.assertIn('Срочно',due[0]['text'])
        with self.store.read() as db:
            raw = b''.join(row[0] for row in db.execute('SELECT body FROM notify_queue'))
        self.assertNotIn('Срочно'.encode(),raw)

    def test_audit_trail(self):
        ticket = self.support.open_or_append(42,'Привет')['ticket_id']
        self.support.reply(ticket,'Здравствуйте','owner');self.support.close(ticket,'owner')
        actions = [e['action'] for e in self.store.events(query='support')['items']]
        self.assertEqual(sorted(actions),['support.close','support.open','support.reply'])
        self.assertNotIn('Здравствуйте',json.dumps(self.store.events()['items'],ensure_ascii=False))


if __name__=='__main__':unittest.main()
