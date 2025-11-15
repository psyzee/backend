import os, time, json, requests
from flask import Flask, redirect, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, Integer, String, Text, Float
from sqlalchemy.orm import sessionmaker, declarative_base

# Configuration (from environment)
CLIENT_ID = os.environ.get('QBO_CLIENT_ID')
CLIENT_SECRET = os.environ.get('QBO_CLIENT_SECRET')
REALM_ID = os.environ.get('QBO_REALM_ID')
REDIRECT_URI = os.environ.get('QBO_REDIRECT_URI')  # e.g. https://your-backend.onrender.com/callback
FRONTEND_URL = os.environ.get('FRONTEND_URL')      # e.g. https://your-frontend.vercel.app
RECEIPTS_API_KEY = os.environ.get('RECEIPTS_API_KEY')  # optional
DATABASE_URL = os.environ.get('DATABASE_URL')  # Postgres

app = Flask(__name__, static_folder='../qbo-frontend/dist', static_url_path='/')
if FRONTEND_URL:
    CORS(app, resources={r"/receipts": {"origins": FRONTEND_URL}, r"/connect": {"origins": FRONTEND_URL}, r"/callback": {"origins": FRONTEND_URL}})
else:
    CORS(app)

# SQLAlchemy setup
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

def init_db():
    global engine, SessionLocal
    if not DATABASE_URL:
        print("DATABASE_URL not set - tokens will be stored in filesystem (not persistent).")
        return
    engine = create_engine(DATABASE_URL, echo=False, future=True)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

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
        print("save_tokens_db:", e)
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
    except Exception as e:
        print("load_tokens_db:", e)
        return None
    finally:
        s.close()

# File fallback
TOKEN_FILE = os.environ.get('TOKEN_FILE', 'tokens.json')
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

# initialize DB if configured
init_db()

# Token refresh helper
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
        print('refresh failed', r.status_code, r.text)
        return None
    new_tokens = r.json()
    new_tokens['expires_at'] = time.time() + new_tokens.get('expires_in', 3600)
    save_tokens(new_tokens)
    return new_tokens.get('access_token')

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

def require_api_key(req):
    if not RECEIPTS_API_KEY:
        return True
    header = req.headers.get('x-app-key') or req.headers.get('X-APP-KEY')
    q = req.args.get('api_key')
    if header == RECEIPTS_API_KEY or q == RECEIPTS_API_KEY:
        return True
    return False

@app.route('/receipts')
def receipts():
    if not require_api_key(request):
        return jsonify({'error': 'Forbidden'}), 403
    access_token = refresh_access_token()
    if not access_token:
        return jsonify({'error': 'Not connected'}), 401
    url = f'https://sandbox-quickbooks.api.intuit.com/v3/company/{REALM_ID}/query'
    query = 'SELECT * FROM SalesReceipt ORDER BY MetaData.CreateTime DESC MAXRESULTS 50'
    headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json', 'Content-Type': 'application/text'}
    r = requests.post(url, data=query, headers=headers)
    if r.status_code != 200:
        return jsonify({'error': 'QuickBooks API error', 'status': r.status_code, 'text': r.text}), 502
    data = r.json()
    receipts = []
    qr = data.get('QueryResponse', {})
    sales = qr.get('SalesReceipt', [])
    for s in sales:
        rid = s.get('DocNumber') or s.get('Id') or f'Q-{int(time.time()*1000)}'
        md = s.get('MetaData') or {}
        created = md.get('CreateTime') or s.get('TxnDate') or time.strftime('%Y-%m-%dT%H:%M:%S')
        customer = (s.get('CustomerRef') or {}).get('name') or ''
        contact = ''
        if s.get('BillAddr') and s['BillAddr'].get('Line1'):
            contact = s['BillAddr']['Line1']
        elif s.get('ShipAddr') and s['ShipAddr'].get('Line1'):
            contact = s['ShipAddr']['Line1']
        elif s.get('BillEmail') and s['BillEmail'].get('Address'):
            contact = s['BillEmail']['Address']
        served_by = ''
        loc = s.get('LocationRef') or s.get('Location') or {}
        if isinstance(loc, dict):
            served_by = loc.get('name') or loc.get('value') or served_by
        if not served_by and s.get('ClassRef'):
            served_by = (s.get('ClassRef') or {}).get('name') or (s.get('ClassRef') or {}).get('value') or served_by
        if not served_by and s.get('DepartmentRef'):
            served_by = (s.get('DepartmentRef') or {}).get('name') or (s.get('DepartmentRef') or {}).get('value') or served_by
        if not served_by and s.get('CustomerMemo') and s['CustomerMemo'].get('value'):
            memo = s['CustomerMemo'].get('value','').strip()
            if 'served by' in memo.lower():
                try:
                    parts = memo.splitlines()
                    for p in parts:
                        if 'served by' in p.lower():
                            if ':' in p:
                                served_by = p.split(':',1)[1].strip()
                            else:
                                served_by = p.lower().replace('served by','').strip().title()
                            break
                except Exception:
                    served_by = memo
            else:
                served_by = memo
        if not served_by and s.get('CustomField'):
            for cf in s.get('CustomField', []):
                name = (cf.get('Name') or '').lower()
                if 'served' in name or 'location' in name or 'branch' in name:
                    served_by = cf.get('StringValue') or cf.get('Value') or cf.get('Name') or served_by
                    break
                if not served_by:
                    served_by = cf.get('StringValue') or cf.get('Value') or served_by
        items = []
        for line in s.get('Line', []):
            desc = line.get('Description') or ''
            amt = line.get('Amount') or 0
            item_name = (line.get('SalesItemLineDetail') or {}).get('ItemRef', {}).get('name') or ''
            items.append({'name': item_name, 'description': desc, 'amount': amt})
        total = s.get('TotalAmt') or s.get('Total') or 0
        receipts.append({
            'id': rid + '-' + str(int(time.time()*1000)),
            'receiptNumber': rid,
            'createdAt': created,
            'customerName': customer,
            'customerContact': contact,
            'servedBy': served_by,
            'items': items,
            'total': total
        })
    return jsonify({'receipts': receipts})

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
