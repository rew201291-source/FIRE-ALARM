"""Local export-trade prototype. Money is stored as decimal text, never float."""
import html
import json
import os
import secrets
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('TRADE_DATA_DIR', str(ROOT / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
TOKEN = secrets.token_urlsafe(32)
CURRENCIES = {'TWD': 2, 'USD': 2, 'VND': 0}


def db():
    c = sqlite3.connect(DATA / 'trade.sqlite3')
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c


def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS documents (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, number TEXT UNIQUE NOT NULL,
          parent INTEGER REFERENCES documents(id), payload TEXT NOT NULL,
          created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS installments (
          id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES documents(id),
          label TEXT NOT NULL, due TEXT NOT NULL, amount TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS receipts (
          id INTEGER PRIMARY KEY, installment_id INTEGER NOT NULL REFERENCES installments(id),
          paid_on TEXT NOT NULL, amount TEXT NOT NULL, fee TEXT NOT NULL, note TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attachments (
          id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
          name TEXT NOT NULL, storage TEXT NOT NULL, category TEXT NOT NULL);
        ''')


def money(value, currency, positive=True):
    if currency not in CURRENCIES:
        raise ValueError('不支援的幣別')
    try:
        x = Decimal(str(value))
        quantum = Decimal(1).scaleb(-CURRENCIES[currency])
        if not x.is_finite() or abs(x) > Decimal('1000000000000000'):
            raise ValueError('金額超出範圍')
        if x != x.quantize(quantum) or (x <= 0 if positive else x < 0):
            raise ValueError('金額必須符合幣別精度，且不可為負數；應收與收款須大於零')
        return format(x.quantize(quantum), 'f')
    except (InvalidOperation, TypeError):
        raise ValueError('金額格式不正確')


def getdoc(c, ident):
    row = c.execute('SELECT * FROM documents WHERE id=?', (ident,)).fetchone()
    if not row:
        raise ValueError('找不到單據')
    return dict(row) | {'data': json.loads(row['payload'])}


def adddoc(c, kind, payload, parent=None):
    cur = c.execute('INSERT INTO documents(kind,number,parent,payload) VALUES(?,?,?,?)',
                    (kind, secrets.token_hex(16), parent, json.dumps(payload, ensure_ascii=False)))
    ident = cur.lastrowid
    number = f'{kind}-{date.today():%Y%m%d}-{ident:05d}'
    c.execute('UPDATE documents SET number=? WHERE id=?', (number, ident))
    return ident


def items(raw, currency):
    if not isinstance(raw, list) or not raw or len(raw) > 200:
        raise ValueError('請提供 1 至 200 筆商品')
    result = []
    for item in raw:
        model = str(item.get('model', '')).strip()
        if not model:
            raise ValueError('商品型號不可空白')
        try:
            qty = Decimal(str(item.get('qty')))
        except InvalidOperation:
            raise ValueError('數量格式不正確')
        if not qty.is_finite() or qty <= 0 or qty > 1000000000 or qty.as_tuple().exponent < -3:
            raise ValueError('數量須大於零，最多三位小數')
        price = money(item.get('price'), currency, False)
        total = (qty * Decimal(price)).quantize(Decimal(1).scaleb(-CURRENCIES[currency]))
        result.append({k: str(item.get(k, '')) for k in ['brand', 'description', 'unit']} |
                      {'model': model, 'qty': str(qty), 'price': price, 'total': str(total)})
    return result


def total(d):
    return sum((Decimal(i['total']) for i in d['items']), Decimal(0))


def action(c, p):
    op = p.get('op')
    if op == 'quote':
        currency = p.get('currency')
        if not p.get('customer', '').strip():
            raise ValueError('客戶名稱不可空白')
        data = {k: str(p.get(k, '')) for k in ['customer', 'address', 'seller', 'seller_address',
                'terms', 'payment_terms', 'delivery', 'bank', 'contract_terms']}
        data.update(currency=currency, date=date.today().isoformat(), items=items(p.get('items'), currency))
        return adddoc(c, 'QT', data)
    doc = getdoc(c, p.get('id'))
    data = doc['data']
    if op == 'confirm':
        if doc['kind'] != 'QT':
            raise ValueError('只能從報價建立訂單')
        if c.execute("SELECT id FROM documents WHERE parent=? AND kind='SO'", (doc['id'],)).fetchone():
            raise ValueError('此報價已建立訂單')
        if not c.execute('SELECT id FROM attachments WHERE document_id=?', (doc['id'],)).fetchone():
            raise ValueError('請先上傳客戶回簽報價、訂單或合約作為確認依據')
        order = adddoc(c, 'SO', data, doc['id'])
        c.execute('INSERT INTO installments(order_id,label,due,amount) VALUES(?,?,?,?)',
                  (order, '待安排付款', '待確認', str(total(data))))
        return order
    if doc['kind'] != 'SO':
        raise ValueError('請選擇正式訂單')
    if op == 'basis':
        if p.get('kind') not in ['PI', 'SC']:
            raise ValueError('付款依據須為 PI 或合約')
        if c.execute("SELECT id FROM documents WHERE parent=? AND kind IN ('PI','SC')", (doc['id'],)).fetchone():
            raise ValueError('此訂單已開立付款依據')
        return adddoc(c, p['kind'], data, doc['id'])
    if op == 'schedule':
        if c.execute('SELECT r.id FROM receipts r JOIN installments i ON r.installment_id=i.id WHERE i.order_id=?', (doc['id'],)).fetchone():
            raise ValueError('已有收款，不可覆寫付款排程')
        rows = p.get('schedule')
        if not isinstance(rows, list) or not rows or len(rows) > 100:
            raise ValueError('請提供付款排程')
        parsed = [(str(r['label']).strip(), str(r['due']).strip(), money(r['amount'], data['currency'])) for r in rows]
        if any(not label or not due for label, due, amount in parsed):
            raise ValueError('期別與付款期限不可空白，可填裝船前等事件')
        if sum((Decimal(r[2]) for r in parsed), Decimal(0)) != total(data):
            raise ValueError('各期應收合計必須等於訂單總額')
        c.execute('DELETE FROM installments WHERE order_id=?', (doc['id'],))
        c.executemany('INSERT INTO installments(order_id,label,due,amount) VALUES(?,?,?,?)',
                      [(doc['id'], *r) for r in parsed])
        return doc['id']
    if op == 'receipt':
        inst = c.execute('SELECT * FROM installments WHERE id=? AND order_id=?', (p.get('installment'), doc['id'])).fetchone()
        if not inst:
            raise ValueError('找不到付款期別')
        amount = money(p.get('amount'), data['currency'])
        fee = money(p.get('fee', '0'), data['currency'], False)
        paid_on = date.fromisoformat(p.get('paid_on', '')).isoformat()
        already = sum((Decimal(r[0]) for r in c.execute('SELECT amount FROM receipts WHERE installment_id=?', (inst['id'],))), Decimal(0))
        if already + Decimal(amount) > Decimal(inst['amount']):
            raise ValueError('收款超過此期未收金額')
        if Decimal(fee) > Decimal(amount):
            raise ValueError('手續費不可超過付款金額')
        c.execute('INSERT INTO receipts(installment_id,paid_on,amount,fee,note) VALUES(?,?,?,?,?)',
                  (inst['id'], paid_on, amount, fee, str(p.get('note', ''))))
        return doc['id']
    if op in ['PO', 'CI']:
        currency = p.get('currency', data['currency']) if op == 'PO' else data['currency']
        rows = items(p.get('items'), currency)
        models = {i['model'] for i in data['items']}
        if any(i['model'] not in models for i in rows):
            raise ValueError('商品型號必須對應銷售訂單')
        if op == 'CI':
            limits = {}
            used = {}
            for i in data['items']:
                limits[i['model']] = limits.get(i['model'], Decimal(0)) + Decimal(i['qty'])
            for r in c.execute("SELECT payload FROM documents WHERE parent=? AND kind='CI'", (doc['id'],)):
                for i in json.loads(r[0])['items']:
                    used[i['model']] = used.get(i['model'], Decimal(0)) + Decimal(i['qty'])
            for i in rows:
                used[i['model']] = used.get(i['model'], Decimal(0)) + Decimal(i['qty'])
            if any(v > limits[k] for k, v in used.items()):
                raise ValueError('累計出貨數量超過訂單數量')
        extra = {k: str(p.get(k, '')) for k in ['supplier', 'vessel', 'sailing', 'from_port', 'to_port', 'marks', 'amount_words']}
        if op == 'PO' and not extra['supplier'].strip():
            raise ValueError('請填供應商')
        return adddoc(c, op, data | extra | {'items': rows, 'currency': currency, 'date': date.today().isoformat()}, doc['id'])
    raise ValueError('不支援的操作')


def snapshot(c):
    docs = [dict(r) for r in c.execute('SELECT * FROM documents ORDER BY id DESC')]
    for d in docs:
        d['data'] = json.loads(d.pop('payload'))
        d['total'] = str(total(d['data']))
    return {'documents': docs, 'installments': [dict(r) for r in c.execute('SELECT * FROM installments')],
            'receipts': [dict(r) for r in c.execute('SELECT * FROM receipts')],
            'attachments': [dict(r) for r in c.execute('SELECT id,document_id,name,category FROM attachments')]}


def printdoc(doc):
    e = lambda s: html.escape(str(s))
    d = doc['data']
    names = {'QT': 'QUOTATION', 'SO': 'SALES ORDER', 'PI': 'PROFORMA INVOICE',
             'SC': 'SALES CONTRACT', 'PO': 'PURCHASE ORDER', 'CI': 'COMMERCIAL INVOICE'}
    rows = ''.join('<tr>' + ''.join(f'<td>{e(i[k])}</td>' for k in ['brand', 'model', 'description', 'qty', 'unit', 'price', 'total']) + '</tr>' for i in d['items'])
    fields = [('Customer', 'customer'), ('Address', 'address'), ('Supplier', 'supplier'),
              ('Trade terms', 'terms'), ('Payment', 'payment_terms'), ('Delivery', 'delivery'),
              ('Vessel / voyage', 'vessel'), ('Sailing date', 'sailing'), ('From', 'from_port'),
              ('To', 'to_port'), ('Marks', 'marks')]
    details = ''.join(f'<p><b>{label}:</b> {e(d.get(key, ""))}</p>' for label, key in fields if d.get(key))
    return f'''<!doctype html><meta charset="utf-8"><title>{e(doc['number'])}</title>
    <style>body{{font:14px Georgia;max-width:1000px;margin:40px auto;color:#182b3c}}h1,h2,header{{text-align:center}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;border-bottom:1px solid #aaa;text-align:left}}pre{{white-space:pre-wrap;font:inherit}}footer{{display:flex;justify-content:space-between;margin-top:70px}}@media print{{button{{display:none}}body{{margin:0}}}}</style>
    <button onclick="window.print()">列印／另存 PDF</button><header><h2>{e(d.get('seller'))}</h2>{e(d.get('seller_address'))}</header>
    <h1>{names[doc['kind']]}</h1><p>No. {e(doc['number'])}　Date: {e(d['date'])}</p>{details}
    <table><thead><tr><th>Brand</th><th>Model</th><th>Description</th><th>Qty</th><th>Unit</th><th>Unit price</th><th>Amount</th></tr></thead><tbody>{rows}</tbody></table>
    <h3 style="text-align:right">TOTAL {e(d['currency'])} {e(total(d))}</h3>
    <p>{e(d.get('amount_words',''))}</p><pre>{e(d.get('bank',''))}</pre>
    {'<h3>Terms and conditions</h3><pre>' + e(d.get('contract_terms','')) + '</pre>' if doc['kind']=='SC' else ''}
    <footer><span>BUYER: __________________</span><span>SELLER: __________________</span></footer>'''


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, content, content_type='application/json; charset=utf-8'):
        body = content.encode() if isinstance(content, str) else content
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == '/':
                body = (ROOT / 'static/index.html').read_text().replace('__TOKEN__', TOKEN)
                return self.reply(200, body, 'text/html; charset=utf-8')
            if path == '/api/state':
                with db() as c:
                    return self.reply(200, json.dumps(snapshot(c), ensure_ascii=False))
            if path.startswith('/print/'):
                with db() as c:
                    return self.reply(200, printdoc(getdoc(c, int(path.split('/')[-1]))), 'text/html; charset=utf-8')
            if path.startswith('/attachment/'):
                with db() as c:
                    row = c.execute('SELECT * FROM attachments WHERE id=?', (int(path.split('/')[-1]),)).fetchone()
                    if row:
                        return self.reply(200, (DATA / row['storage']).read_bytes(), 'application/octet-stream')
            self.reply(404, '{"error":"Not found"}')
        except (ValueError, OSError) as exc:
            self.reply(400, json.dumps({'error': str(exc)}, ensure_ascii=False))

    def do_POST(self):
        if self.headers.get('X-Trade-Token') != TOKEN:
            return self.reply(403, '{"error":"請重新載入頁面"}')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 15 * 1024 * 1024:
            return self.reply(413, '{"error":"檔案上限 15MB"}')
        try:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                path = urlparse(self.path).path
                if path.startswith('/api/upload/'):
                    ident = int(path.split('/')[-1])
                    getdoc(c, ident)
                    name = self.headers.get('X-File-Name', 'document')
                    from urllib.parse import unquote
                    name = Path(unquote(name)).name
                    if Path(name).suffix.lower() not in ['.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg']:
                        raise ValueError('僅接受 PDF、Word、PNG、JPG')
                    storage = secrets.token_hex(24)
                    (DATA / storage).write_bytes(self.rfile.read(length))
                    c.execute('INSERT INTO attachments(document_id,name,storage,category) VALUES(?,?,?,?)',
                              (ident, name, storage, '客戶確認／往來文件'))
                elif path == '/api/action':
                    p = json.loads(self.rfile.read(length))
                    ident = action(c, p)
                else:
                    raise ValueError('不支援的路徑')
            self.reply(200, json.dumps({'id': ident}))
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
            self.reply(400, json.dumps({'error': str(exc)}, ensure_ascii=False))


if __name__ == '__main__':
    init()
    port = int(os.environ.get('PORT', '8000'))
    print(f'Trade prototype listening on 127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
