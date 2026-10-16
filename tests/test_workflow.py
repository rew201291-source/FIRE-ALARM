import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get('TRADE_DATA_DIR')
        os.environ['TRADE_DATA_DIR'] = self.tmp.name
        spec = importlib.util.spec_from_file_location('trade', Path(__file__).parents[1] / 'app.py')
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        self.app.init()
        self.c = self.app.db()

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()
        if self.previous is None:
            os.environ.pop('TRADE_DATA_DIR', None)
        else:
            os.environ['TRADE_DATA_DIR'] = self.previous

    def act(self, **p):
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            return self.app.action(self.c, p)

    def quote(self, currency='USD', price='100.00'):
        return self.act(op='quote', customer='Test buyer', currency=currency,
                        items=[{'model': 'PANEL', 'qty': '10', 'price': price, 'unit': 'PCS'}])

    def order(self, currency='USD'):
        q = self.quote(currency)
        with self.c:
            self.c.execute('INSERT INTO attachments(document_id,name,storage,category) VALUES(?,?,?,?)',
                           (q, 'signed.pdf', 'test', 'test'))
        return self.act(op='confirm', id=q)

    def test_confirmation_requires_evidence_and_is_not_duplicated(self):
        q = self.quote()
        with self.assertRaisesRegex(ValueError, '上傳'):
            self.act(op='confirm', id=q)
        with self.c:
            self.c.execute('INSERT INTO attachments(document_id,name,storage,category) VALUES(?,?,?,?)',
                           (q, 'signed.pdf', 'test', 'test'))
        so = self.act(op='confirm', id=q)
        with self.assertRaisesRegex(ValueError, '已建立'):
            self.act(op='confirm', id=q)
        self.assertEqual(self.c.execute('SELECT amount FROM installments WHERE order_id=?', (so,)).fetchone()[0], '1000.00')

    def test_split_receipts_and_exclusive_payment_basis(self):
        so = self.order()
        self.act(op='basis', id=so, kind='SC')
        with self.assertRaisesRegex(ValueError, '已開立'):
            self.act(op='basis', id=so, kind='PI')
        self.act(op='schedule', id=so, schedule=[{'label': '訂金', 'due': '2026-11-01', 'amount': '300'},
                                                {'label': '尾款', 'due': '裝船前', 'amount': '700'}])
        ident = self.c.execute('SELECT id FROM installments WHERE order_id=? ORDER BY id', (so,)).fetchone()[0]
        self.act(op='receipt', id=so, installment=ident, paid_on='2026-10-16', amount='100', fee='5')
        self.act(op='receipt', id=so, installment=ident, paid_on='2026-10-17', amount='200', fee='0')
        rs = self.c.execute('SELECT amount,fee FROM receipts').fetchall()
        self.assertEqual(len(rs), 2)
        self.assertEqual(sum(self.app.Decimal(r[0]) for r in rs), self.app.Decimal('300'))
        with self.assertRaisesRegex(ValueError, '超過'):
            self.act(op='receipt', id=so, installment=ident, paid_on='2026-10-18', amount='1', fee='0')
        with self.assertRaisesRegex(ValueError, '已有收款'):
            self.act(op='schedule', id=so, schedule=[])

    def test_invalid_schedule_keeps_existing_schedule(self):
        so = self.order()
        with self.assertRaisesRegex(ValueError, '合計'):
            self.act(op='schedule', id=so, schedule=[{'label': 'test', 'due': '裝船前', 'amount': '1'}])
        self.assertEqual(self.c.execute('SELECT amount FROM installments WHERE order_id=?', (so,)).fetchone()[0], '1000.00')

    def test_currencies_and_precision(self):
        for currency in ['TWD', 'USD', 'VND']:
            so = self.order(currency)
            self.assertEqual(self.app.getdoc(self.c, so)['data']['currency'], currency)
        for x in ['1.1', 'NaN', 'Infinity', '-1']:
            with self.assertRaises(ValueError):
                self.app.money(x, 'VND')
        self.assertEqual(self.app.money('0.10', 'USD'), '0.10')

    def test_purchase_and_partial_shipments(self):
        so = self.order()
        po = self.act(op='PO', id=so, supplier='Vendor', currency='VND',
                      items=[{'model': 'PANEL', 'qty': '10', 'price': '250000'}])
        self.assertEqual(self.app.getdoc(self.c, po)['data']['currency'], 'VND')
        for qty in ['6', '4']:
            ci = self.act(op='CI', id=so, items=[{'model': 'PANEL', 'qty': qty, 'price': '100'}])
        with self.assertRaisesRegex(ValueError, '超過'):
            self.act(op='CI', id=so, items=[{'model': 'PANEL', 'qty': '1', 'price': '100'}])
        printed = self.app.printdoc(self.app.getdoc(self.c, ci))
        self.assertIn('COMMERCIAL INVOICE', printed)
        self.assertIn('400.00', printed)
        self.assertEqual(self.app.getdoc(self.c, so)['data']['items'][0]['qty'], '10')

    def test_print_escapes_customer_content(self):
        q = self.act(op='quote', customer='<script>alert(1)</script>', currency='USD',
                     items=[{'model': 'PANEL', 'qty': '1', 'price': '0.10'}])
        output = self.app.printdoc(self.app.getdoc(self.c, q))
        self.assertNotIn('<script>', output)
        self.assertIn('&lt;script&gt;', output)


if __name__ == '__main__':
    unittest.main()
