from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from cryptography.fernet import Fernet
from analytics import Analytics,MSK
from excel_export import analytics_xlsx,NS
from store import Store


class ExcelExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'shop.db',Fernet.generate_key())
        self.store.set_currency('100','test');self.analytics=Analytics(self.store)
        self.pid=self.store.save_product(dict(category='Test',name='=1+1 & <product>',description='',price=1234,
            cost_price=500,mode='auto',instruction='',active=True))
        self.store.adjust_wallet(42,100000,'Test','credit','test')

    def tearDown(self):self.temp.cleanup()

    def sale(self,secret):
        self.store.import_stock(self.pid,secret)
        order=self.store.create_order(self.pid,42);self.store.pay_wallet(order['id'],42)
        return order['id']

    def workbook(self,report=None):return ZipFile(BytesIO(analytics_xlsx(report or self.analytics.report('day',full=True))))

    def cells(self,archive,sheet):
        root=ET.fromstring(archive.read('xl/worksheets/sheet'+str(sheet)+'.xml'))
        return {node.attrib['r']:node for node in root.findall('.//{'+NS+'}c')}

    def value(self,node):return ''.join(node.itertext())

    def test_real_xlsx_typed_money_dates_percentages_and_safe_text(self):
        oid=self.sale('private-secret')
        import promo
        promo.Promos(self.store).create(dict(code='GIFT',amount_cents=250),'owner')
        promo.Promos(self.store).redeem_to_balance('GIFT',77)
        day=datetime.now(MSK).date().isoformat()
        self.analytics.add_entry(str(uuid.uuid4()),'expense','server',100,'=HYPERLINK("bad")\x01',day,None,'test')
        self.analytics.add_entry(str(uuid.uuid4()),'refund','refund',200,'Returned',day,oid,'test')
        with self.workbook() as archive:
            for name in archive.namelist():ET.fromstring(archive.read(name))
            summary=self.cells(archive,1)
            self.assertEqual(Decimal(self.value(summary['B6'])),Decimal('12.34'))
            self.assertEqual(self.value(summary['A7']),'Начислено по промокодам')
            self.assertEqual(Decimal(self.value(summary['B7'])),Decimal('2.50'))  # promo credits, not revenue
            self.assertEqual(Decimal(self.value(summary['B11'])),Decimal('4.34'))
            self.assertEqual(summary['B12'].attrib['s'],'7')
            self.assertEqual(summary['B14'].attrib['s'],'0')
            self.assertIsNone(summary['B6'].get('t'))
            sales=self.cells(archive,3)
            self.assertEqual(self.value(sales['C6']),'=1+1 & <product>')
            self.assertEqual(sales['C6'].attrib['t'],'inlineStr')
            self.assertGreater(Decimal(self.value(sales['A6'])),40000)
            self.assertIn(b'autoFilter',archive.read('xl/worksheets/sheet3.xml'))
            self.assertIn(b'ySplit="5"',archive.read('xl/worksheets/sheet3.xml'))
            for name in archive.namelist():
                self.assertNotIn(b'<f>',archive.read(name))
                self.assertNotIn(b'private-secret',archive.read(name))
            texts=[self.value(node) for node in self.cells(archive,4).values()]
            self.assertIn('=HYPERLINK("bad")',texts)

    def test_unknown_cost_remains_unknown_and_zero_cost_remains_numeric(self):
        oid=self.sale('item')
        with self.store.tx() as db:db.execute('UPDATE orders SET cost_total=NULL WHERE id=?',(oid,))
        with self.workbook() as archive:
            self.assertEqual(self.value(self.cells(archive,1)['B11']),'Нет себестоимости')
            self.assertEqual(self.value(self.cells(archive,3)['F6']),'Нет себестоимости')
        self.analytics.set_order_cost(oid,0,'Free product','test')
        with self.workbook() as archive:
            self.assertEqual(self.value(self.cells(archive,3)['F6']),'0')
            self.assertIsNone(self.cells(archive,3)['F6'].get('t'))

    def test_full_export_includes_every_page_and_cancelled_entries(self):
        for index in range(27):self.sale('secret-'+str(index))
        reference=str(uuid.uuid4())
        self.analytics.add_entry(reference,'expense','server',100,'Cancelled cost',datetime.now(MSK).date().isoformat(),None,'test')
        self.analytics.void_entry(reference,'Wrong entry','test')
        with self.assertRaises(ValueError):analytics_xlsx(self.analytics.report('day'))
        with self.workbook() as archive:
            sales=self.cells(archive,3)
            self.assertIn('A32',sales);self.assertNotIn('A33',sales)
            expenses=self.cells(archive,4)
            self.assertEqual(self.value(expenses['G6']),'Отменена')
            self.assertEqual(self.value(expenses['H6']),'Wrong entry')

    def test_empty_period_exports_valid_four_sheets(self):
        with self.workbook() as archive:
            workbook=ET.fromstring(archive.read('xl/workbook.xml'))
            self.assertEqual([sheet.attrib['name'] for sheet in workbook.findall('.//{'+NS+'}sheet')],['Итоги','По дням','Продажи','Расходы'])
            self.assertEqual(self.value(self.cells(archive,1)['B6']),'0')
            self.assertNotIn('A6',self.cells(archive,3))


if __name__=='__main__':unittest.main()
