"""
Enhanced Flask app with:
- Render Postgres SSL fixes
- Clean SalesReceipt parsing
- Auto-generate thermal receipt text
- Pagination + DB caching for receipts
- /receipt/<id> lookup API
- PDF generation (ReportLab)
- Webhook endpoint to cache new receipts
- Background worker to refresh QBO tokens automatically

Dependencies (add to requirements.txt):
Flask
Flask-Cors
SQLAlchemy
psycopg2-binary
requests
cachetools
reportlab

Place this file as app.py; keep your frontend in ../qbo-frontend/dist as before.
"""

import os
import time
import json
import threading
import queue
from io import BytesIO
from datetime import datetime, timedelta

import requests
from flask import Flask, redirect, request, jsonify, send_from_directory, make_response, abort
from flask_cors import CORS
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, JSON, func
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import SQLAlchemyError
from cachetools import TTLCache
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# --------------------------- Configuration ---------------------------
CLIENT_ID = os.environ.get('QBO_CLIENT_ID')
CLIENT_SECRET = os.environ.get('QBO_CLIENT_SECRET')
REALM_ID = os.environ.get('QBO_REALM_ID')
REDIRECT_URI = os.environ.get('QBO_REDIRECT_URI')
FRONTEND_URL = os.environ.get('FRONTEND_URL')
RECEIPTS_API_KEY = os.environ.get('RECEIPTS_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
TOKEN_FILE = os.environ.get('TOKEN_FILE', 'tokens.json')

# App
app = Flask(__name__, static_folder='../qbo-frontend/dist', static_url_path='/')
if FRONTEND_URL:
    CORS(app, resources={r"/receipts": {"origins": FRONTEND_URL}, r"/connect": {"origins": FRONTEND_URL}, r"/callback": {"origins": FRONTEND_URL}})
else:
    CORS(app)

# --------------------------- Database / Models ---------------------------
Base = declarative_base()
engine = None
SessionLocal = None

class Token(Base):
    __tablename__ = 'tokens'
    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String(64), nullable=True)
    expires_at = Column(Float, nullable=True)
    created_at = Column(Float, default=time.time)

class CachedReceipt(Base):
    __tablename__ = 'cached_receipts'
    id = Column(Integer, primary_key=True, index=True)
    receipt_id = Column(String(200), unique=True, index=True)
    receipt_number = Column(String(200), index=True)
    created_at = Column(DateTime, default=func.now())
    payload = Column(JSON)  # stores parsed receipt
    raw = Column(JSON, nullable=True)  # raw QBO object
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

# In-memory TTL cache for very fast reads (optional)
INMEM_CACHE = TTLCache(maxsize=1024, ttl=300)  # 5 minutes

# --------------------------- DB Init (with Render fixes) ---------------------------

def init_db():
    global engine, SessionLocal
    if not DATABASE_URL:
        print("DATABASE_URL not set - tokens & cache will be stored in filesystem / memory (not persistent).")
        return

    db_url = DATABASE_URL.replace("postgres://", "postgresql://")
    engine = create_engine(
        db_url,
        echo=False,
        future=True,
        pool_size=5,
        max_overflow=0,
        pool_pre_ping=True,
        pool_recycle=180,
        connect_args={
            "sslmode": "require",
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        }
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

init_db()

# --------------------------- Token Storage Helpers ---------------------------

def save_tokens_db(data):
    if not SessionLocal:
        return
    s = SessionLocal()
    try:
        s.query(Token).delete()
        t = Token(
            access_token=data.get('access_token'),
            refresh_token=data.get('refresh_token'),
            token_type=data.get('token_type'),
            expires_at=float(time.time()) + float(data.get('expires_in', 3600))
        )
        s.add(t)
        s.commit()
    except Exception as e:
        s.rollback()
        app.logger.exception('save_tokens_db failed')
    finally:
        s.close()


def load_tokens_db():
    if not SessionLocal:
        return None
    s = SessionLocal()
    try:
        t = s.query(Token).order_by(Token.id.desc()).first()
        if not t:
            return None
        return {
            'access_token': t.access_token,
            'refresh_token': t.refresh_token,
            'token_type': t.token_type,
            'expires_at': t.expires_at
        }
    except Exception:
        app.logger.exception('load_tokens_db failed')
        return None
    finally:
        s.close()


def save_tokens_file(data):
    with open(TOKEN_FILE, 'w') as f:
        json.dump(data, f)


def load_tokens_file():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, 'r') as f:
        return json.load(f)


def save_tokens(data):
    if SessionLocal:
        save_tokens_db(data)
    else:
        save_tokens_file(data)


def load_tokens():
    if SessionLocal:
        return load_tokens_db()
    return load_tokens_file()

# --------------------------- QBO auth + refresh ---------------------------

def refresh_access_token():
    tokens = load_tokens()
    if not tokens:
        return None
    if tokens.get('access_token') and time.time() < tokens.get('expires_at', 0) - 60:
        return tokens.get('access_token')
    refresh_token = tokens.get('refresh_token')
    if not refresh_token:
        return None
    token_url = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
    auth = requests.auth.HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET)
    data = {'grant_type': 'refresh_token', 'refresh_token': refresh_token}
    r = requests.post(token_url, auth=auth, data=data)
    if r.status_code != 200:
        app.logger.error('refresh failed %s %s', r.status_code, r.text)
        return None
    new_tokens = r.json()
    new_tokens['expires_at'] = time.time() + new_tokens.get('expires_in', 3600)
    save_tokens(new_tokens)
    return new_tokens.get('access_token')

# Background worker queue for tasks (webhook processing etc.)
WORKER_Q = queue.Queue()


def worker_loop():
    """Simple background worker that processes queued tasks."""
    app.logger.info('Background worker started')
    while True:
        try:
            task = WORKER_Q.get()
            if not task:
                time.sleep(1)
                continue
            ttype = task.get('type')
            if ttype == 'refresh_token':
                # attempt refresh
                refresh_access_token()
            elif ttype == 'fetch_and_cache':
                receipt_id = task.get('receipt_id')
                if receipt_id:
                    try:
                        fetch_and_cache_receipt(receipt_id)
                    except Exception:
                        app.logger.exception('fetch_and_cache failed for %s', receipt_id)
            WORKER_Q.task_done()
        except Exception:
            app.logger.exception('worker_loop uncaught')
            time.sleep(1)

# Start worker thread
t = threading.Thread(target=worker_loop, daemon=True)
t.start()

# Periodic token refresher thread (ensures token stays fresh)

def periodic_token_refresher():
    while True:
        try:
            WORKER_Q.put({'type': 'refresh_token'})
        except Exception:
            app.logger.exception('enqueue refresh failed')
        time.sleep(60)  # every 60s

tr = threading.Thread(target=periodic_token_refresher, daemon=True)
tr.start()

# --------------------------- QBO Helpers ---------------------------

def qbo_query(query):
    access_token = refresh_access_token()
    if not access_token:
        return None, (401, 'Not connected')
    url = f'https://sandbox-quickbooks.api.intuit.com/v3/company/{REALM_ID}/query'
    headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json', 'Content-Type': 'application/text'}
    r = requests.post(url, data=query, headers=headers, timeout=20)
    if r.status_code != 200:
        return None, (r.status_code, r.text)
    return r.json(), None


def qbo_get_salesreceipt_by_id(qbo_id):
    access_token = refresh_access_token()
    if not access_token:
        return None
    url = f'https://sandbox-quickbooks.api.intuit.com/v3/company/{REALM_ID}/salesreceipt/{qbo_id}'
    headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code != 200:
        app.logger.error('QBO get failed %s %s', r.status_code, r.text)
        return None
    return r.json()

# --------------------------- Parsing (clean) ---------------------------

def parse_salesreceipt(s):
    # 1. Receipt Number
    receipt_number = s.get('DocNumber') or s.get('Id') or f'R-{int(time.time()*1000)}'

    # 2. Created timestamp
    metadata = s.get('MetaData') or {}
    created_at = metadata.get('CreateTime') or s.get('TxnDate') or datetime.utcnow().isoformat()

    # 3. Customer name
    customer_name = (s.get('CustomerRef') or {}).get('name') or ''

    # 4. Contact
    contact = ''
    if s.get('BillAddr') and s['BillAddr'].get('Line1'):
        contact = s['BillAddr']['Line1']
    elif s.get('ShipAddr') and s['ShipAddr'].get('Line1'):
        contact = s['ShipAddr']['Line1']
    elif s.get('BillEmail') and s['BillEmail'].get('Address'):
        contact = s['BillEmail']['Address']

    # 5. Served By
    served_by = ''
    for field in ['LocationRef', 'Location', 'ClassRef', 'DepartmentRef']:
        ref = s.get(field)
        if isinstance(ref, dict):
            served_by = ref.get('name') or ref.get('value') or ''
        if served_by:
            break

    if not served_by:
        memo = (s.get('CustomerMemo') or {}).get('value', '').strip()
        if memo:
            lower = memo.lower()
            if 'served by' in lower:
                try:
                    for line in memo.splitlines():
                        if 'served by' in line.lower():
                            if ':' in line:
                                served_by = line.split(':', 1)[1].strip()
                            else:
                                served_by = line.lower().replace('served by', '').strip().title()
                            break
                except Exception:
                    served_by = memo
            else:
                served_by = memo

    if not served_by:
        for cf in s.get('CustomField', []) or []:
            name = (cf.get('Name') or '').lower()
            if any(key in name for key in ['served', 'location', 'branch']):
                served_by = cf.get('StringValue') or cf.get('Value') or ''
                break

    # 6. Items (also capture taxes and unit prices)
    items = []
    for line in s.get('Line', []) or []:
        detail = line.get('SalesItemLineDetail') or {}
        item_ref = detail.get('ItemRef') or {}
        item_name = item_ref.get('name') or ''
        qty = detail.get('Qty') or detail.get('Quantity') or 1
        unit_price = detail.get('UnitPrice') or 0
        amount = line.get('Amount') or 0
        tax_code = detail.get('TaxCodeRef', {}).get('value') if isinstance(detail.get('TaxCodeRef'), dict) else None
        items.append({
            'name': item_name,
            'description': line.get('Description') or '',
            'quantity': qty,
            'unit_price': unit_price,
            'amount': amount,
            'tax_code': tax_code
        })

    # 7. Itemized taxes & payment details
    taxes = []
    for tline in s.get('TaxLine', []) or []:
        taxes.append({
            'amount': tline.get('Amount') or 0,
            'detail_type': tline.get('DetailType'),
            'tax_rate_ref': (tline.get('TaxLineDetail') or {}).get('TaxRateRef') if isinstance(tline.get('TaxLineDetail'), dict) else None
        })

    payments = []
    for p in s.get('Payment', []) or []:
        payments.append({
            'amount': p.get('Amount') or 0,
            'method': p.get('PaymentMethodRef', {}).get('name') if isinstance(p.get('PaymentMethodRef'), dict) else None
        })

    total = s.get('TotalAmt') or s.get('Total') or 0

    parsed = {
        'receiptNumber': receipt_number,
        'createdAt': created_at,
        'customerName': customer_name,
        'customerContact': contact,
        'servedBy': served_by,
        'items': items,
        'taxes': taxes,
        'payments': payments,
        'total': total,
    }
    return parsed

# --------------------------- Cache helpers ---------------------------

def cache_receipt_in_db(receipt_id, parsed_payload, raw_obj=None):
    if not SessionLocal:
        return
    s = SessionLocal()
    try:
        existing = s.query(CachedReceipt).filter(CachedReceipt.receipt_id == receipt_id).first()
        if existing:
            existing.payload = parsed_payload
            existing.raw = raw_obj
            existing.updated_at = datetime.utcnow()
        else:
            r = CachedReceipt(
                receipt_id=receipt_id,
                receipt_number=parsed_payload.get('receiptNumber'),
                created_at=datetime.utcnow(),
                payload=parsed_payload,
                raw=raw_obj
            )
            s.add(r)
        s.commit()
    except Exception:
        s.rollback()
        app.logger.exception('cache_receipt_in_db failed')
    finally:
        s.close()


def get_cached_receipt_db(receipt_id):
    # Check in-memory first
    if receipt_id in INMEM_CACHE:
        return INMEM_CACHE[receipt_id]
    if not SessionLocal:
        return None
    s = SessionLocal()
    try:
        r = s.query(CachedReceipt).filter(CachedReceipt.receipt_id == receipt_id).first()
        if not r:
            return None
        INMEM_CACHE[receipt_id] = r.payload
        return r.payload
    except Exception:
        app.logger.exception('get_cached_receipt_db failed')
        return None
    finally:
        s.close()

# --------------------------- Fetch + Cache helpers ---------------------------

def fetch_and_cache_receipt(receipt_id):
    # Try fetching from QBO by Id
    q = qbo_get_salesreceipt_by_id(receipt_id)
    # qbo_get returns full response JSON; format may be {"SalesReceipt": {...}} or wrapped
    if not q:
        return None
    # if sandbox q returns wrapper
    raw = None
    if isinstance(q, dict):
        # try to locate SalesReceipt object
        if 'SalesReceipt' in q:
            raw = q.get('SalesReceipt')
        elif 'QueryResponse' in q:
            qr = q.get('QueryResponse')
            raw = (qr.get('SalesReceipt') or [None])[0]
        else:
            # try top-level
            raw = q
    else:
        raw = q
    if not raw:
        return None
    parsed = parse_salesreceipt(raw)
    # use a canonical id: receiptNumber or Id
    canonical_id = parsed.get('receiptNumber') or raw.get('DocNumber') or raw.get('Id')
    cache_receipt_in_db(canonical_id, parsed, raw)
    INMEM_CACHE[canonical_id] = parsed
    return parsed

# --------------------------- Thermal text generator ---------------------------

def generate_thermal_text(parsed):
    # create a compact thermal friendly text block
    lines = []
    lines.append('*** RECEIPT ***')
    lines.append(f"No: {parsed.get('receiptNumber')}")
    lines.append(f"Date: {parsed.get('createdAt')}")
    lines.append(f"Customer: {parsed.get('customerName')}")
    contact = parsed.get('customerContact')
    if contact:
        lines.append(f"Contact: {contact}")
    served = parsed.get('servedBy')
    if served:
        lines.append(f"Served By: {served}")
    lines.append('-' * 24)
    for it in parsed.get('items', []):
        name = it.get('name') or it.get('description') or 'Item'
        qty = it.get('quantity') or 1
        amt = it.get('amount') or 0
        lines.append(f"{name[:20]:20} x{qty:>3}  {amt:>8.2f}")
    lines.append('-' * 24)
    for tax in parsed.get('taxes', []):
        lines.append(f"Tax {tax.get('detail_type') or ''}: {float(tax.get('amount') or 0):.2f}")
    lines.append(f"TOTAL: {float(parsed.get('total') or 0):.2f}")
    lines.append('* Thank you *')
    # join with newline suitable for thermal printers
    return '\n'.join(lines)

# --------------------------- PDF Generation (ReportLab) ---------------------------

def generate_pdf_bytes(parsed):
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    x = 40
    y = height - 40
    p.setFont('Helvetica-Bold', 12)
    p.drawString(x, y, 'RECEIPT')
    p.setFont('Helvetica', 10)
    y -= 20
    p.drawString(x, y, f"No: {parsed.get('receiptNumber')}")
    y -= 14
    p.drawString(x, y, f"Date: {parsed.get('createdAt')}")
    y -= 14
    p.drawString(x, y, f"Customer: {parsed.get('customerName')}")
    y -= 14
    if parsed.get('customerContact'):
        p.drawString(x, y, f"Contact: {parsed.get('customerContact')}")
        y -= 14
    if parsed.get('servedBy'):
        p.drawString(x, y, f"Served by: {parsed.get('servedBy')}")
        y -= 18
    p.drawString(x, y, '-' * 60)
    y -= 18
    p.setFont('Helvetica', 9)
    p.drawString(x, y, f"{'Item':30}{'Qty':>5}{'Price':>12}{'Amount':>12}")
    y -= 14
    for it in parsed.get('items', []):
        if y < 80:
            p.showPage()
            y = height - 40
        name = (it.get('name') or '')[:30]
        qty = it.get('quantity') or 1
        unit_price = it.get('unit_price') or 0
        amount = it.get('amount') or 0
        p.drawString(x, y, f"{name:30}{qty:>5}{unit_price:12.2f}{amount:12.2f}")
        y -= 14
    y -= 10
    p.drawString(x, y, '-' * 60)
    y -= 18
    p.setFont('Helvetica-Bold', 11)
    p.drawString(x, y, f"TOTAL: {float(parsed.get('total') or 0):.2f}")
    p.showPage()
    p.save()
    pdf = buffer.getvalue()
    buffer.close()
    return pdf

# --------------------------- API Endpoints ---------------------------

def require_api_key(req):
    if not RECEIPTS_API_KEY:
        return True
    header = req.headers.get('x-app-key') or req.headers.get('X-APP-KEY')
    q = req.args.get('api_key')
    if header == RECEIPTS_API_KEY or q == RECEIPTS_API_KEY:
        return True
    return False


@app.route('/connect')
def connect():
    state = 'state123'
    auth_url = (
        f"https://appcenter.intuit.com/connect/oauth2?client_id={CLIENT_ID}"
        f"&response_type=code&scope=com.intuit.quickbooks.accounting&redirect_uri={REDIRECT_URI}&state={state}"
    )
    return redirect(auth_url)


@app.route('/callback')
def callback():
    code = request.args.get('code')
    if not code:
        return 'Missing code', 400
    token_url = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
    auth = requests.auth.HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET)
    data = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': REDIRECT_URI}
    r = requests.post(token_url, auth=auth, data=data)
    if r.status_code != 200:
        return f'Token exchange failed: {r.text}', 500
    tokens = r.json()
    tokens['expires_at'] = time.time() + tokens.get('expires_in', 3600)
    save_tokens(tokens)
    frontend = FRONTEND_URL or 'http://localhost:5173'
    return redirect(frontend + '/?connected=true')


@app.route('/receipts')
def receipts():
    if not require_api_key(request):
        return jsonify({'error': 'Forbidden'}), 403

    # pagination
    try:
        page = max(int(request.args.get('page', 1)), 1)
        per_page = min(int(request.args.get('per_page', 20)), 50)
    except Exception:
        page, per_page = 1, 20

    # check DB cache first: return cached receipts ordered by created_at desc
    if SessionLocal:
        s = SessionLocal()
        try:
            q = s.query(CachedReceipt).order_by(CachedReceipt.updated_at.desc())
            total = q.count()
            items = q.offset((page - 1) * per_page).limit(per_page).all()
            receipts_list = [r.payload for r in items]
            return jsonify({'receipts': receipts_list, 'page': page, 'per_page': per_page, 'total': total})
        except Exception:
            app.logger.exception('DB list failed; falling back to QBO')
        finally:
            s.close()

    # fallback: query QBO directly
    query = f"SELECT * FROM SalesReceipt ORDER BY MetaData.CreateTime DESC MAXRESULTS {per_page} STARTPOSITION {(page-1)*per_page + 1}"
    data, err = qbo_query(query)
    if err:
        code, text = err
        return jsonify({'error': 'QuickBooks API error', 'status': code, 'text': text}), 502
    receipts = []
    qr = data.get('QueryResponse', {})
    sales = qr.get('SalesReceipt', [])
    for s in sales:
        parsed = parse_salesreceipt(s)
        # cache asynchronously
        canonical_id = parsed.get('receiptNumber')
        if canonical_id:
            WORKER_Q.put({'type': 'fetch_and_cache', 'receipt_id': canonical_id})
        receipts.append(parsed)
    return jsonify({'receipts': receipts, 'page': page, 'per_page': per_page, 'total': len(receipts)})


@app.route('/receipt/<receipt_id>')
def receipt_lookup(receipt_id):
    if not require_api_key(request):
        return jsonify({'error': 'Forbidden'}), 403

    # Try in-memory / db
    cached = get_cached_receipt_db(receipt_id)
    if cached:
        return jsonify({'receipt': cached})

    # else fetch from QBO (synchronous)
    parsed = fetch_and_cache_receipt(receipt_id)
    if not parsed:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'receipt': parsed})


@app.route('/receipt/<receipt_id>/thermal')
def receipt_thermal(receipt_id):
    if not require_api_key(request):
        return jsonify({'error': 'Forbidden'}), 403
    cached = get_cached_receipt_db(receipt_id)
    if not cached:
        cached = fetch_and_cache_receipt(receipt_id)
    if not cached:
        return jsonify({'error': 'Not found'}), 404
    text = generate_thermal_text(cached)
    return make_response(text, 200, {'Content-Type': 'text/plain; charset=utf-8'})


@app.route('/receipt/<receipt_id>/pdf')
def receipt_pdf(receipt_id):
    if not require_api_key(request):
        return jsonify({'error': 'Forbidden'}), 403
    cached = get_cached_receipt_db(receipt_id)
    if not cached:
        cached = fetch_and_cache_receipt(receipt_id)
    if not cached:
        return jsonify({'error': 'Not found'}), 404
    pdf = generate_pdf_bytes(cached)
    resp = make_response(pdf)
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'inline; filename=receipt-{receipt_id}.pdf'
    return resp

# --------------------------- Webhook endpoint ---------------------------
# QuickBooks Webhooks will POST JSON describing entity and event. We accept it and enqueue a cache refresh.

@app.route('/webhook/qbo', methods=['POST'])
def qbo_webhook():
    # You should set this webhook URL in your QBO app config
    # NOTE: signature verification omitted (QuickBooks uses HMAC with a signature header). If you want to verify,
    # implement verification using your app's webhook secret.
    payload = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({'ok': False, 'reason': 'invalid json'}), 400
    # sample webhook body contains event notifications with entityId etc.
    # We'll attempt to pull any 'entityId' or 'id' we can find and enqueue a fetch
    ids = set()
    def find_ids(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in ('entityid', 'id', 'entity') and isinstance(v, (str, int)):
                    ids.add(str(v))
                else:
                    find_ids(v)
        elif isinstance(obj, list):
            for it in obj:
                find_ids(it)
    find_ids(payload)
    for rid in ids:
        WORKER_Q.put({'type': 'fetch_and_cache', 'receipt_id': rid})
    return jsonify({'ok': True, 'queued': list(ids)})

# static serve optional
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def static_proxy(path):
    SPA_DIR = os.path.join(os.path.dirname(__file__), '../qbo-frontend/dist')
    if path and os.path.exists(os.path.join(SPA_DIR, path)):
        return send_from_directory(SPA_DIR, path)
    index = os.path.join(SPA_DIR, 'index.html')
    if os.path.exists(index):
        return send_from_directory(SPA_DIR, 'index.html')
    return 'Backend running', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
