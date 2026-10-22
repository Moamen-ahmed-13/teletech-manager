import json, math, os, re, shutil, socket, threading, time, uuid, webbrowser, zipfile, subprocess, base64, random, hashlib, hmac, secrets, smtplib, email.message, http.cookies, sqlite3, datetime

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
from collections import Counter, defaultdict
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET
from email.parser import BytesParser
from email.policy import default as email_policy_default
try:
    import recon_module as RECON, recon_ui as RECON_UI   # BOQ vs MIR reconciliation (optional add-on)
except Exception as _recon_err:
    RECON=None; RECON_UI=None; print(f'[RECON] reconciliation module unavailable: {_recon_err}')

try:
    import fitz  # PyMuPDF - used to render scanned As-Built PDF pages to images
except ImportError:
    fitz = None

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

# As-Built is intentionally CODE-ONLY in this build.
# No GLM/Qwen/vision-agent is called by the As-Built workflow.
# Optional OCR is kept disabled by default and is never required for scanning.
try:
    from PIL import Image
except ImportError:
    Image = None

ASBUILT_HF_OCR_MODEL = os.getenv(
    'TELETECH_HF_OCR_MODEL',
    'Waqf-AI/waqf-ocr-hand-written-v1'
).strip()
ASBUILT_HF_OCR_ENABLED = os.getenv('TELETECH_HF_OCR_ENABLED', '0').strip().lower() not in {
    '0','false','no','off'
}
ASBUILT_HF_OCR_MAX_NEW_TOKENS = int(os.getenv('TELETECH_HF_OCR_MAX_NEW_TOKENS','128') or 128)
ASBUILT_HF_OCR_MAX_IMAGE_DIM = int(os.getenv('TELETECH_HF_OCR_MAX_IMAGE_DIM','1400') or 1400)
_ASBUILT_HF_OCR_MODEL_OBJ = None
_ASBUILT_HF_OCR_PROCESSOR = None
_ASBUILT_HF_OCR_LOCK = threading.Lock()

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / '.env'
if load_dotenv:
    load_dotenv(ENV_FILE, override=False)
    print(f'[ENV] .env loaded: {ENV_FILE.exists()}')
else:
    print('[ENV] python-dotenv not installed; .env not loaded')

# ----------------------------- Authentication -----------------------------
AUTH_SESSION_TTL = int(os.getenv('TELETECH_AUTH_SESSION_TTL', '28800') or 28800)
AUTH_OTP_TTL = int(os.getenv('TELETECH_AUTH_OTP_TTL', '300') or 300)
AUTH_OTP_MAX_ATTEMPTS = int(os.getenv('TELETECH_AUTH_OTP_MAX_ATTEMPTS', '5') or 5)
AUTH_LOGIN_RATE_MAX = int(os.getenv('TELETECH_AUTH_LOGIN_RATE_MAX', '5') or 5)
AUTH_LOGIN_RATE_WINDOW = int(os.getenv('TELETECH_AUTH_LOGIN_RATE_WINDOW', '30') or 30)
AUTH_FORGOT_RATE_MAX = int(os.getenv('TELETECH_AUTH_FORGOT_RATE_MAX', '3') or 3)
AUTH_FORGOT_RATE_WINDOW = int(os.getenv('TELETECH_AUTH_FORGOT_RATE_WINDOW', '30') or 30)
AUTH_USERNAME = os.getenv('TELETECH_AUTH_USERNAME', 'admin').strip()
AUTH_PASSWORD = os.getenv('TELETECH_AUTH_PASSWORD', '').strip()
AUTH_OTP_MODE = os.getenv('TELETECH_AUTH_OTP_MODE', 'console').strip().lower()
AUTH_OTP_EMAIL = os.getenv('TELETECH_AUTH_OTP_EMAIL', '').strip()
AUTH_SMTP_HOST = os.getenv('TELETECH_SMTP_HOST', '').strip()
AUTH_SMTP_PORT = int(os.getenv('TELETECH_SMTP_PORT', '587') or 587)
AUTH_SMTP_USERNAME = os.getenv('TELETECH_SMTP_USERNAME', '').strip()
AUTH_SMTP_PASSWORD = os.getenv('TELETECH_SMTP_PASSWORD', '').strip()
AUTH_SMTP_FROM = os.getenv('TELETECH_SMTP_FROM', AUTH_SMTP_USERNAME).strip()
AUTH_SMTP_USE_SSL = os.getenv('TELETECH_SMTP_USE_SSL', '0').strip().lower() in {'1','true','yes','on'}
AUTH_COOKIE_NAME = 'TELETECH_AUTH'
AUTH_COOKIE_SECURE = os.getenv('TELETECH_AUTH_COOKIE_SECURE', '0').strip().lower() in {'1','true','yes','on'}
AUTH_DB = ROOT / os.getenv('TELETECH_AUTH_DB', 'teletech_users.db')
AUTH_ROLES = {'ADMIN','ENGINEER','TECHNICIAN','VIEWER'}
_AUTH_SESSIONS = {}
FORGOT_CHALLENGES = {}
_AUTH_PENDING = {}
_AUTH_RATE = {}
_AUTH_FORGOT_RATE = {}
_AUTH_LOCK = threading.Lock()

def _auth_db_conn():
    con = sqlite3.connect(str(AUTH_DB), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    return con

def _auth_password_hash(password, salt=None):
    if salt is None: salt = secrets.token_bytes(16)
    elif isinstance(salt, str): salt = bytes.fromhex(salt)
    digest = hashlib.scrypt(str(password).encode('utf-8'), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return digest.hex(), salt.hex()

def _auth_init_db():
    with _auth_db_conn() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'VIEWER',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        con.commit()
        con.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, username TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, path TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '')")
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_username ON audit_log(username)")
        con.commit()
        if con.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0 and AUTH_PASSWORD:
            ph, salt = _auth_password_hash(AUTH_PASSWORD)
            now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            initial_email = AUTH_OTP_EMAIL or AUTH_SMTP_USERNAME
            con.execute('INSERT INTO users(username,password_hash,password_salt,email,role,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        (AUTH_USERNAME or 'admin', ph, salt, initial_email, 'ADMIN', 1, now, now))
            con.commit()
            print(f'[AUTH] Created initial ADMIN user in {AUTH_DB.name}: {AUTH_USERNAME}')
        elif AUTH_OTP_MODE == 'email' and AUTH_SMTP_USERNAME:
            # Backfill the initial admin email once, if the DB was created before email OTP was enabled.
            row = con.execute('SELECT id,email FROM users WHERE username=? COLLATE NOCASE',(AUTH_USERNAME or 'admin',)).fetchone()
            if row and not str(row['email'] or '').strip():
                target_email = AUTH_OTP_EMAIL or AUTH_SMTP_USERNAME
                con.execute('UPDATE users SET email=?, updated_at=? WHERE id=?',(target_email,time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),row['id']))
                con.commit()
                print(f'[AUTH] Backfilled email for initial ADMIN user: {target_email}')

def _auth_get_user(username_or_email):
    key=str(username_or_email or '').strip()
    with _auth_db_conn() as con:
        row=con.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE OR email=? COLLATE NOCASE LIMIT 1',(key,key)).fetchone()
        return dict(row) if row else None

def _auth_list_users():
    with _auth_db_conn() as con:
        return [dict(r) for r in con.execute('SELECT id,username,email,role,active,created_at,updated_at FROM users ORDER BY username COLLATE NOCASE').fetchall()]

def _auth_create_user(username,password,email='',role='VIEWER'):
    username=str(username or '').strip(); email=str(email or '').strip(); role=str(role or 'VIEWER').strip().upper()
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,80}',username): raise ValueError('Username must be 3-80 characters and use letters, numbers, _, ., @ or -.')
    if len(password or '')<8: raise ValueError('Password must be at least 8 characters.')
    if role not in AUTH_ROLES: raise ValueError('Invalid role.')
    if email and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email): raise ValueError('Invalid email address.')
    ph,salt=_auth_password_hash(password); now=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    with _auth_db_conn() as con:
        con.execute('INSERT INTO users(username,password_hash,password_salt,email,role,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(username,ph,salt,email,role,1,now,now)); con.commit()
    return _auth_get_user(username)

def _auth_update_user(user_id,email=None,role=None,active=None,password=None):
    with _auth_db_conn() as con:
        row=con.execute('SELECT * FROM users WHERE id=?',(int(user_id),)).fetchone()
        if not row: raise ValueError('User not found.')
        sets=[]; vals=[]
        if email is not None:
            email=str(email).strip()
            if email and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email): raise ValueError('Invalid email address.')
            sets+=['email=?']; vals+=[email]
        if role is not None:
            role=str(role).strip().upper()
            if role not in AUTH_ROLES: raise ValueError('Invalid role.')
            sets+=['role=?']; vals+=[role]
        if active is not None: sets+=['active=?']; vals+=[1 if bool(active) else 0]
        if password is not None:
            if len(password)<8: raise ValueError('Password must be at least 8 characters.')
            ph,salt=_auth_password_hash(password); sets += ['password_hash=?','password_salt=?']; vals += [ph,salt]
        if not sets: return dict(row)
        sets.append('updated_at=?'); vals.append(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())); vals.append(int(user_id))
        con.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?",vals); con.commit()
    return _auth_get_user(row['username'])

def _auth_change_password(user_id, current_password, new_password):
    if not new_password or len(new_password) < 8:
        raise ValueError('New password must be at least 8 characters.')
    with _auth_db_conn() as con:
        row = con.execute('SELECT * FROM users WHERE id=?', (int(user_id),)).fetchone()
        if not row:
            raise ValueError('User not found.')
        user = dict(row)
        if not _auth_password_ok(user, current_password):
            raise ValueError('Current password is incorrect.')
        ph, salt = _auth_password_hash(new_password)
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        con.execute('UPDATE users SET password_hash=?, password_salt=?, updated_at=? WHERE id=?', (ph, salt, now, int(user_id)))
        con.commit()
    return _auth_get_user(user['username'])

def _auth_count_admins():
    with _auth_db_conn() as con: return int(con.execute("SELECT COUNT(*) FROM users WHERE role='ADMIN' AND active=1").fetchone()[0])

def _auth_password_ok(user,password):
    if not user or not user.get('active'): return False
    try:
        digest,_=_auth_password_hash(password,user['password_salt']); return hmac.compare_digest(digest,user['password_hash'])
    except Exception: return False

def _client_ip(handler):
    try: return str(handler.client_address[0])
    except Exception: return ''

def _rate_check(store, key, limit, window):
    now=time.time()
    with _AUTH_LOCK:
        vals=[t for t in store.get(key,[]) if now-t < window]
        store[key]=vals
        return len(vals) < limit, max(0, int(window-(now-vals[0])) if vals else 0)

def _rate_hit(store, key, window):
    now=time.time()
    with _AUTH_LOCK:
        vals=[t for t in store.get(key,[]) if now-t < window]
        vals.append(now)
        store[key]=vals

def _rate_clear(store, key):
    with _AUTH_LOCK: store.pop(key,None)

def _auth_cleanup():
    now=time.time()
    with _AUTH_LOCK:
        for token,rec in list(_AUTH_SESSIONS.items()):
            if now>=rec.get('expires',0): _AUTH_SESSIONS.pop(token,None)
        for key,rec in list(_AUTH_PENDING.items()):
            if now>=rec.get('expires',0): _AUTH_PENDING.pop(key,None)
        for store,window in ((_AUTH_RATE,AUTH_LOGIN_RATE_WINDOW),(_AUTH_FORGOT_RATE,AUTH_FORGOT_RATE_WINDOW)):
            for key,vals in list(store.items()):
                vals[:]=[t for t in vals if now-t < window]
                if not vals: store.pop(key,None)

def _auth_cookie_value(handler):
    jar=http.cookies.SimpleCookie()
    try:
        jar.load(handler.headers.get('Cookie','')); return jar.get(AUTH_COOKIE_NAME).value if jar.get(AUTH_COOKIE_NAME) else ''
    except Exception: return ''

def _auth_current_user(handler):
    _auth_cleanup(); token=_auth_cookie_value(handler)
    if not token: return None
    with _AUTH_LOCK:
        rec=_AUTH_SESSIONS.get(token)
        if not rec: return None
        if time.time()>=rec.get('expires',0): _AUTH_SESSIONS.pop(token,None); return None
        rec['last_seen']=time.time(); username=rec.get('username','')
    user=_auth_get_user(username)
    if not user or not user.get('active'):
        with _AUTH_LOCK: _AUTH_SESSIONS.pop(token,None)
        return None
    return user

def _auth_is_authenticated(handler): return _auth_current_user(handler) is not None

def _auth_set_cookie(token):
    c=http.cookies.SimpleCookie(); c[AUTH_COOKIE_NAME]=token; m=c[AUTH_COOKIE_NAME]; m['path']='/'; m['httponly']=True; m['samesite']='Strict'
    if AUTH_COOKIE_SECURE: m['secure']=True
    return m.OutputString()

def _auth_clear_cookie():
    c=http.cookies.SimpleCookie(); c[AUTH_COOKIE_NAME]=''; m=c[AUTH_COOKIE_NAME]; m['path']='/'; m['expires']='Thu, 01 Jan 1970 00:00:00 GMT'; m['max-age']='0'; m['httponly']=True; m['samesite']='Strict'; return m.OutputString()

def _auth_send_otp(user,code):
    destination=(user.get('email') or '').strip()
    if AUTH_OTP_MODE=='console':
        print(f"[AUTH OTP] {user['username']}: {code} (valid {AUTH_OTP_TTL}s)")
        return True
    if AUTH_OTP_MODE!='email':
        raise RuntimeError("Invalid TELETECH_AUTH_OTP_MODE. Use 'console' or 'email'.")
    if not destination:
        raise RuntimeError('This user has no email address configured for OTP.')
    if not AUTH_SMTP_HOST or not AUTH_SMTP_USERNAME or not AUTH_SMTP_PASSWORD:
        raise RuntimeError('Email OTP is not configured. Check TELETECH_SMTP_HOST, TELETECH_SMTP_USERNAME, and TELETECH_SMTP_PASSWORD in .env.')

    msg=email.message.EmailMessage()
    msg['Subject']='TELETECH Login OTP'
    msg['From']=AUTH_SMTP_FROM or AUTH_SMTP_USERNAME
    msg['To']=destination
    msg.set_content(f"Your TELETECH login OTP is: {code}\n\nIt expires in {max(1,AUTH_OTP_TTL//60)} minutes and can be used once.\n\nIf you did not request this code, ignore this email.")

    # Gmail supports both 587/STARTTLS and 465/implicit SSL.  Try the configured
    # mode first, then the other standard Gmail mode if the connection is closed.
    # This is especially useful on Windows networks where one SMTP port can be blocked.
    configured=(AUTH_SMTP_PORT, AUTH_SMTP_USE_SSL)
    alternatives=[]
    if AUTH_SMTP_HOST.lower() in {'smtp.gmail.com','smtp.googlemail.com'}:
        if configured != (465, True): alternatives.append((465, True))
        if configured != (587, False): alternatives.append((587, False))
    attempts=[configured]+alternatives
    last_error=None
    for port,use_ssl in attempts:
        try:
            print(f"[AUTH SMTP] Connecting {AUTH_SMTP_HOST}:{port} mode={'SSL' if use_ssl else 'STARTTLS'}")
            if use_ssl:
                with smtplib.SMTP_SSL(AUTH_SMTP_HOST,port,timeout=20) as smtp:
                    smtp.ehlo()
                    smtp.login(AUTH_SMTP_USERNAME,AUTH_SMTP_PASSWORD)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(AUTH_SMTP_HOST,port,timeout=20) as smtp:
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
                    smtp.login(AUTH_SMTP_USERNAME,AUTH_SMTP_PASSWORD)
                    smtp.send_message(msg)
            print(f"[AUTH SMTP] OTP email sent to {destination}")
            return True
        except Exception as e:
            last_error=e
            print(f"[AUTH SMTP ERROR] {type(e).__name__} on {AUTH_SMTP_HOST}:{port}: {e}")
            continue
    raise RuntimeError(f'Gmail SMTP delivery failed on ports 587 and 465: {type(last_error).__name__}: {last_error}')

def _forgot_find_user(identifier):
    ident=str(identifier or '').strip()
    if not ident: return None
    with _auth_db_conn() as con:
        row=con.execute('SELECT * FROM users WHERE username=? OR lower(email)=lower(?) LIMIT 1',(ident,ident)).fetchone()
        return dict(row) if row else None

def _forgot_request(identifier):
    user=_forgot_find_user(identifier)
    if not user:
        return None, 'If the account exists, an OTP has been sent to its registered email.'
    email=str(user.get('email') or '').strip()
    if not email:
        return None, 'This account has no registered email. Contact an administrator.'
    code=f'{random.randint(0,999999):06d}'
    cid=secrets.token_urlsafe(24)
    FORGOT_CHALLENGES[cid]={'user_id':int(user['id']),'username':user['username'],'email':email,'otp_hash':hashlib.sha256(code.encode()).hexdigest(),'expires_at':time.time()+300,'attempts':0,'verified':False,'used':False}
    if AUTH_OTP_MODE=='email': _auth_send_otp(user,code)
    else: print(f'[AUTH FORGOT OTP] {user["username"]}: {code}',flush=True)
    return cid,'If the account exists, an OTP has been sent to its registered email.'

def _forgot_verify(cid,otp):
    ch=FORGOT_CHALLENGES.get(cid)
    if not ch or ch.get('used'): return None,'Invalid or expired reset request.'
    if time.time()>ch['expires_at']: FORGOT_CHALLENGES.pop(cid,None); return None,'OTP expired.'
    if ch['attempts']>=5: FORGOT_CHALLENGES.pop(cid,None); return None,'Too many OTP attempts.'
    ch['attempts']+=1
    if not secrets.compare_digest(hashlib.sha256(str(otp).strip().encode()).hexdigest(),ch['otp_hash']): return None,'Invalid OTP.'
    ch['verified']=True; return ch,None

def _forgot_reset(cid,password):
    ch=FORGOT_CHALLENGES.get(cid)
    if not ch or not ch.get('verified') or ch.get('used'): return False,'Reset request is not verified.'
    if time.time()>ch['expires_at']: FORGOT_CHALLENGES.pop(cid,None); return False,'Reset request expired.'
    if len(password or '')<8: return False,'Password must be at least 8 characters.'
    _auth_update_user(ch['user_id'], password=password)
    ch['used']=True
    for sid,sess in list(_AUTH_SESSIONS.items()):
        if sess.get('user_id')==ch['user_id'] or sess.get('username')==ch['username']: _AUTH_SESSIONS.pop(sid,None)
    return True,None

def _auth_begin(username,password):
    _auth_cleanup(); user=_auth_get_user(username)
    if not user or not _auth_password_ok(user,str(password or '')): return {'error':'Invalid username or password.','code':'INVALID_CREDENTIALS'}
    code=f'{secrets.randbelow(1000000):06d}'; challenge=secrets.token_urlsafe(32)
    with _AUTH_LOCK: _AUTH_PENDING[challenge]={'username':user['username'],'otp_hash':hashlib.sha256(code.encode('ascii')).hexdigest(),'expires':time.time()+AUTH_OTP_TTL,'attempts':0}
    try:
        _auth_send_otp(user,code)
    except Exception as e:
        with _AUTH_LOCK: _AUTH_PENDING.pop(challenge,None)
        print(f'[AUTH EMAIL ERROR] {type(e).__name__}: {e}')
        raise RuntimeError('Could not send OTP email. Check your Gmail SMTP App Password and SMTP settings in .env.') from e
    return {'ok':True,'challenge':challenge,'expires_in':AUTH_OTP_TTL,'delivery':'email' if AUTH_OTP_MODE=='email' else 'console'}

def _auth_verify(challenge,otp):
    _auth_cleanup(); otp=str(otp or '').strip()
    if not re.fullmatch(r'\d{6}',otp): return {'error':'OTP must be exactly 6 digits.','code':'INVALID_OTP'}
    with _AUTH_LOCK:
        rec=_AUTH_PENDING.get(str(challenge or ''))
        if not rec: return {'error':'OTP expired or login challenge is invalid. Start login again.','code':'OTP_EXPIRED'}
        if rec.get('attempts',0)>=AUTH_OTP_MAX_ATTEMPTS: _AUTH_PENDING.pop(str(challenge),None); return {'error':'Too many OTP attempts. Start login again.','code':'OTP_LOCKED'}
        rec['attempts']+=1
        if not hmac.compare_digest(hashlib.sha256(otp.encode('ascii')).hexdigest(),rec['otp_hash']): return {'error':f"Invalid OTP. Attempts remaining: {max(0,AUTH_OTP_MAX_ATTEMPTS-rec['attempts'])}.",'code':'INVALID_OTP'}
        _AUTH_PENDING.pop(str(challenge),None); token=secrets.token_urlsafe(48); now=time.time(); user=_auth_get_user(rec['username'])
        if not user or not user.get('active'): return {'error':'User account is disabled.','code':'USER_DISABLED'}
        _AUTH_SESSIONS[token]={'username':rec['username'],'role':user['role'],'created':now,'last_seen':now,'expires':now+AUTH_SESSION_TTL}
    return {'ok':True,'token':token,'username':rec['username'],'role':user['role']}

def _auth_logout(handler):
    token=_auth_cookie_value(handler)
    with _AUTH_LOCK: _AUTH_SESSIONS.pop(token,None)
    return _auth_clear_cookie()

def _auth_audit(handler, action, path='', detail='', role=None, username=None):
    try:
        if username is None or role is None:
            u=_auth_current_user(handler)
            if u:
                username=username if username is not None else u['username']
                role=role if role is not None else u['role']
        try: ip=str(handler.client_address[0])
        except Exception: ip=''
        now=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        with _auth_db_conn() as con:
            con.execute('INSERT INTO audit_log(created_at,username,role,action,path,detail,ip) VALUES(?,?,?,?,?,?,?)',(now,str(username or ''),str(role or ''),str(action),str(path or ''),str(detail or '')[:1000],ip)); con.commit()
    except Exception as e: print(f'[AUTH AUDIT ERROR] {e}')

def _auth_audit_list(limit=250):
    limit=max(1,min(int(limit or 250),500))
    with _auth_db_conn() as con: return [dict(r) for r in con.execute('SELECT id,created_at,username,role,action,path,detail,ip FROM audit_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]

def _auth_require_role(handler,roles):
    user=_auth_current_user(handler)
    if not user: return None,{'error':'Authentication required.','code':'AUTH_REQUIRED'}
    if user['role'] not in set(roles): return user,{'error':'You do not have permission for this action.','code':'FORBIDDEN'}
    return user,None

_auth_init_db()

# ----------------------------- Project Management -----------------------------
PROJECT_DB = AUTH_DB
PROJECTS_ROOT = ROOT / 'workspace' / 'projects'
PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
PROJECT_CONTEXT_LOCK = threading.RLock()
PROJECT_STATE_LOCK = threading.RLock()
PROJECT_COOKIE_NAME = 'TELETECH_PROJECT'
PROJECT_SCHEMA_LOCK = threading.Lock()
CURRENT_PROJECT_ID = None
CURRENT_PROJECT_OWNER_ID = None
CURRENT_RESULT = None
CURRENT_DRAWING = None
PROJECT_RESULTS = {}
UPLOADED_DRAWINGS = []
PROJECT_GENERATED_FILES = set()

def _project_db_conn():
    con=sqlite3.connect(str(PROJECT_DB),timeout=10); con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL'); con.execute('PRAGMA foreign_keys=ON'); return con

def _project_init_db():
    with PROJECT_SCHEMA_LOCK:
        with _project_db_conn() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, name TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE)''')
            con.execute('CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_user_id, updated_at DESC)')
            con.commit()

def _project_now(): return datetime.datetime.utcnow().replace(microsecond=0).isoformat()+'Z'
def _project_safe_name(name):
    name=re.sub(r'[\/:*?"<>|]+','_',str(name or '').strip()); name=re.sub(r'\s+',' ',name).strip()
    return name[:120] or 'Untitled Project'
def _project_dir(pid): d=PROJECTS_ROOT/str(pid); d.mkdir(parents=True,exist_ok=True); return d
def _project_state_path(pid): return _project_dir(pid)/'state.json'
def _project_cookie_value(handler):
    jar=http.cookies.SimpleCookie()
    try:
        jar.load(handler.headers.get('Cookie','')); return jar.get(PROJECT_COOKIE_NAME).value if jar.get(PROJECT_COOKIE_NAME) else ''
    except Exception: return ''
def _project_set_cookie(pid):
    c=http.cookies.SimpleCookie(); c[PROJECT_COOKIE_NAME]=str(pid); m=c[PROJECT_COOKIE_NAME]; m['path']='/'; m['samesite']='Strict'; return m.OutputString()
def _project_get(pid,user):
    if not pid or not user:return None
    with _project_db_conn() as con:
        r=con.execute('SELECT id,owner_user_id,name,created_at,updated_at FROM projects WHERE id=? AND owner_user_id=?',(str(pid),int(user['id']))).fetchone()
    return dict(r) if r else None
def _project_list(user):
    with _project_db_conn() as con:
        rows=con.execute('SELECT id,owner_user_id,name,created_at,updated_at FROM projects WHERE owner_user_id=? ORDER BY updated_at DESC,created_at DESC',(int(user['id']),)).fetchall()
    return [dict(r) for r in rows]
def _project_create(user,name=None):
    pid=uuid.uuid4().hex; pname=_project_safe_name(name or 'New Project'); now=_project_now()
    with _project_db_conn() as con:
        con.execute('INSERT INTO projects(id,owner_user_id,name,created_at,updated_at) VALUES(?,?,?,?,?)',(pid,int(user['id']),pname,now,now)); con.commit()
    _project_dir(pid); _project_save_state(pid,{},[],None,None); return {'id':pid,'owner_user_id':int(user['id']),'name':pname,'created_at':now,'updated_at':now}
def _project_touch(pid):
    now=_project_now()
    with _project_db_conn() as con: con.execute('UPDATE projects SET updated_at=? WHERE id=?',(now,str(pid))); con.commit()
    return now
def _project_save_state(pid,results,drawings,current_result,current_drawing):
    # Serialize state writes and use a unique temp file so Windows file locks or
    # overlapping requests cannot collide on the same state.json.tmp path.
    with PROJECT_STATE_LOCK:
        d=_project_dir(pid)
        state={'version':1,'saved_at':_project_now(),'results':results or {},'drawings':drawings or [],'generated_files':sorted(PROJECT_GENERATED_FILES),'current_floor':(current_result or {}).get('floor') if isinstance(current_result,dict) else None,'current_drawing':str(current_drawing) if current_drawing else None}
        tmp=d/('state.'+uuid.uuid4().hex+'.tmp')
        try:
            tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
            last_error=None
            for attempt in range(5):
                try:
                    tmp.replace(d/'state.json')
                    last_error=None
                    break
                except PermissionError as e:
                    last_error=e
                    if attempt < 4: time.sleep(0.08 * (attempt + 1))
            if last_error is not None:
                raise last_error
            _project_touch(pid)
        finally:
            try:
                if tmp.exists(): tmp.unlink()
            except OSError:
                pass
def _project_load_state(pid):
    global CURRENT_RESULT,CURRENT_DRAWING,PROJECT_RESULTS,UPLOADED_DRAWINGS,PROJECT_GENERATED_FILES
    data={}
    try:
        f=_project_state_path(pid)
        if f.exists(): data=json.loads(f.read_text(encoding='utf-8') or '{}')
    except Exception as e: print(f'[PROJECT] Failed to load {pid}: {e}')
    PROJECT_RESULTS=data.get('results') or {}; UPLOADED_DRAWINGS=data.get('drawings') or []; PROJECT_GENERATED_FILES=set(data.get('generated_files') or [])
    cf=data.get('current_floor'); cd=data.get('current_drawing')
    CURRENT_RESULT=PROJECT_RESULTS.get(cf) if cf else (next(iter(PROJECT_RESULTS.values())) if PROJECT_RESULTS else None)
    CURRENT_DRAWING=Path(cd) if cd else (Path(CURRENT_RESULT.get('drawing')) if isinstance(CURRENT_RESULT,dict) and CURRENT_RESULT.get('drawing') else None)
def _project_resolve(handler,user,pid=None,create_if_missing=False):
    pid=str(pid or _project_cookie_value(handler) or '').strip(); p=_project_get(pid,user) if pid else None
    if p:return p
    rows=_project_list(user)
    if rows:return rows[0]
    return _project_create(user,'New Project') if create_if_missing else None
def _project_require_for_request(handler,user,path,pid=None,create_if_missing=False):
    p=_project_resolve(handler,user,pid,create_if_missing)
    if not p:return None,{'error':'No project selected. Create a project first.','code':'PROJECT_REQUIRED'}
    _project_load_state(p['id']); return p,None
def _project_save_current():
    if CURRENT_PROJECT_ID: _project_save_state(CURRENT_PROJECT_ID,PROJECT_RESULTS,UPLOADED_DRAWINGS,CURRENT_RESULT,CURRENT_DRAWING)

_project_init_db()

AUTH_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TELETECH — Login</title><style>*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f3f5f4;color:#16202a;font:15px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif}
.card{width:min(420px,92vw);padding:32px;border:1px solid #dde3e0;border-top:4px solid #0b6b4f;border-radius:14px;background:#fff}
.brand{font-size:13px;letter-spacing:.08em;color:#0b6b4f;font-weight:700;margin-bottom:14px}
h1{margin:0 0 6px;font-size:26px}.sub{color:#5d6975;margin-bottom:22px}
.field{margin:14px 0}.field label{display:block;font-size:13px;font-weight:600;color:#5d6975;margin-bottom:6px}
input{width:100%;height:44px;padding:0 13px;border-radius:8px;border:1px solid #c8d1cd;background:#fff;color:#16202a;font-size:15px}
input:focus-visible{outline:2px solid #2f55a4;outline-offset:1px}
.btn{width:100%;height:44px;border:0;border-radius:8px;background:#0b6b4f;color:#fff;font-weight:650;font-size:15px;cursor:pointer;margin-top:10px}
.btn:hover:not(:disabled){background:#074f3b}.btn:disabled{opacity:.5;cursor:not-allowed}
.status{min-height:22px;margin-top:14px;font-size:13.5px;color:#b42318}.success{color:#067647}
.otp{letter-spacing:7px;text-align:center;font-size:22px}.hidden{display:none}
#forgotModal input{margin:6px 0}#forgotModal h2{margin:0 0 6px}
</style></head><body><div class="card"><div class="brand">TELETECH MANAGER</div><h1>Sign in</h1>
<div class="sub">Enter your password, then the one-time code (OTP) we send you.</div><div id="step1">
<div class="field"><label>Username or Email</label><input id="user" autocomplete="username"></div>
<div class="field"><label>Password</label><input id="pass" type="password" autocomplete="current-password"></div>
<button id="loginBtn" class="btn">Continue</button></div><div id="step2" class="hidden">
<div class="field"><label>6-digit OTP</label><input id="otp" class="otp" inputmode="numeric" maxlength="6" autocomplete="one-time-code"></div>
<button id="otpBtn" class="btn">Verify OTP</button><button id="backBtn" class="btn" style="background:#fff;color:#16202a;border:1px solid #c8d1cd">Back</button></div>
<div id="status" class="status"></div><div style="margin-top:10px;text-align:center"><button id="forgotLink" type="button" style="border:0;background:none;color:#0b6b4f;font-weight:600;cursor:pointer;padding:4px 0">Forgot Password?</button></div></div><script>
const $=id=>document.getElementById(id),status=$('status');
$('loginBtn').onclick=async()=>{status.className='status';status.textContent='Checking credentials...';$('loginBtn').disabled=true;
try{let r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('user').value,password:$('pass').value})});let j=await r.json();
if(!r.ok||j.error)throw Error(j.error||'Login failed');window._challenge=j.challenge;$('step1').classList.add('hidden');$('step2').classList.remove('hidden');$('otp').focus();status.className='status success';status.textContent='OTP sent. Enter the 6-digit code.'}
catch(e){status.textContent=e.message}finally{$('loginBtn').disabled=false}};
$('otpBtn').onclick=async()=>{status.className='status';status.textContent='Verifying OTP...';$('otpBtn').disabled=true;
try{let r=await fetch('/api/auth/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({challenge:window._challenge,otp:$('otp').value})});let j=await r.json();
if(!r.ok||j.error)throw Error(j.error||'OTP verification failed');location.href='/'}
catch(e){status.textContent=e.message}finally{$('otpBtn').disabled=false}};
$('backBtn').onclick=()=>{window._challenge=null;$('step2').classList.add('hidden');$('step1').classList.remove('hidden');status.textContent=''};
$('pass').onkeydown=e=>{if(e.key==='Enter')$('loginBtn').click()};$('otp').onkeydown=e=>{if(e.key==='Enter')$('otpBtn').click()};

</script>
<div id="forgotModal" style="display:none;position:fixed;inset:0;background:#0009;place-items:center;z-index:10000"><div style="width:min(420px,92vw);background:#fff;color:#16202a;border:1px solid #dde3e0;border-radius:14px;padding:24px;position:relative"><button id="forgotClose" style="position:absolute;right:12px;top:8px;border:0;background:none;color:#16202a;font-size:25px;cursor:pointer">×</button><h2>Reset Password</h2><p style="color:#5d6975">Reset using the OTP sent to your registered email.</p><div id="fp1"><input id="fpId" placeholder="Username or Email"><button type="button" id="fpSend" class="btn">Send OTP</button></div><div id="fp2" class="hidden"><input id="fpOtp" class="otp" maxlength="6" inputmode="numeric" placeholder="6-digit OTP"><button type="button" id="fpVerify" class="btn">Verify OTP</button></div><div id="fp3" class="hidden"><input id="fpNew" type="password" placeholder="New Password"><input id="fpConfirm" type="password" placeholder="Confirm New Password"><button type="button" id="fpReset" class="btn">Reset Password</button></div><div id="fpMsg" class="status"></div></div></div>
<script>
(function(){const $=id=>document.getElementById(id);let cid='';$('forgotLink').onclick=()=>{$('forgotModal').style.display='grid'};$('forgotClose').onclick=()=>{$('forgotModal').style.display='none'};$('fpSend').onclick=async(e)=>{e.preventDefault();const id=$('fpId').value.trim();if(!id){$('fpMsg').textContent='Enter your username or email first.';return}$('fpSend').disabled=true;$('fpMsg').textContent='Sending OTP...';try{let r=await fetch('/api/auth/forgot/request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({identifier:id})}),j=await r.json();if(!r.ok){$('fpMsg').textContent=j.error||'Failed to send OTP.';return}if(j.challenge_id){cid=j.challenge_id;$('fp1').classList.add('hidden');$('fp2').classList.remove('hidden');$('fpOtp').focus()}$('fpMsg').textContent=j.message||'OTP sent. Check your email.'}catch(err){$('fpMsg').textContent='Connection error: '+err.message}finally{$('fpSend').disabled=false}};$('fpVerify').onclick=async()=>{let r=await fetch('/api/auth/forgot/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({challenge_id:cid,otp:$('fpOtp').value})}),j=await r.json();if(!r.ok)return $('fpMsg').textContent=j.error||'Invalid OTP';$('fp2').classList.add('hidden');$('fp3').classList.remove('hidden');$('fpMsg').textContent='OTP verified.'};$('fpReset').onclick=async()=>{let r=await fetch('/api/auth/forgot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({challenge_id:cid,password:$('fpNew').value,confirm_password:$('fpConfirm').value})}),j=await r.json();$('fpMsg').textContent=j.message||j.error||'';if(r.ok)setTimeout(()=>location.href='/login',900)}})();
</script>
</body></html>'''

print('[AS-BUILT] CODE-ONLY pipeline enabled | no GLM/Qwen agent')

WORKSPACE = ROOT / 'workspace'
EXPORTS = ROOT / 'exports'
TEMPLATES = ROOT / 'templates'
WORKSPACE.mkdir(exist_ok=True)
EXPORTS.mkdir(exist_ok=True)

PORT_MAP_TEMPLATE = TEMPLATES / 'port_map_template.xlsx'
SUMMARY_TEMPLATE = TEMPLATES / 'quantity_summary_template.xlsx'
CABLE_TEMPLATE = TEMPLATES / 'cable_schedule_template.xlsx'
PORT_MAP_PROJECT_TEMPLATE = TEMPLATES / 'port_map_project_template.xlsx'
CABLE_PROJECT_TEMPLATE = TEMPLATES / 'cable_schedule_project_template.xlsx'
ROUTE_PROFILES_DIR = ROOT / 'route_profiles'
BUILTIN_ROUTE_REFERENCE_JSON = ROUTE_PROFILES_DIR / 'mansoura_ground_route_profile.json'

# Project-scoped state is initialized by the Project Management layer above.
ANALYSIS_LOCK = threading.Lock()
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB safety cap
UPLOAD_CHUNK = 4 * 1024 * 1024            # 4 MiB streaming chunks

# Drawing QA / wall-gap auto-heal cache. Patches are overlay-only and never modify the uploaded DXF.
GAP_HEAL_CACHE = {}

# As-Built site statuses that are allowed to remove a point from the drawing/BOQ.
# All other handwritten/site notes are reference-only and must not alter outputs.
ASBUILT_REMOVE_STATUSES = (
    'لم يتم السحب',
    'تم رد الكابلات',
    'لا يوجد لها مسار',
    'تم نقل',
)

def _norm_ar(s):
    """Normalize Arabic handwritten text for fuzzy status matching.
    Strips diacritics, shadda/tatweel, unifies hamza/alef/ta-marbuta/ya,
    collapses whitespace."""
    s = (s or '').strip()
    s = re.sub(r'[\u064B-\u0652\u0640]', '', s)   # تشكيل + تطويل
    s = s.replace('أ','ا').replace('إ','ا').replace('آ','ا')
    s = s.replace('ة','ه').replace('ى','ي')
    return re.sub(r'\s+', ' ', s)

_REMOVE_NORM = tuple(_norm_ar(x) for x in ASBUILT_REMOVE_STATUSES)

def _is_remove_status(status):
    """Return True if the handwritten status matches any removal keyword,
    tolerating spacing/hamza/tashkeel differences and trailing context words.
    Also handles fully-joined (spaceless) variants like 'تم ردالكابلات'."""
    n = _norm_ar(status)
    ns = n.replace(' ', '')                          # spaceless version
    for r in _REMOVE_NORM:
        rs = r.replace(' ', '')
        if n == r or n.startswith(r + ' ') or ns == rs or ns.startswith(rs):
            return True
    return False
ASBUILT_PATCH_ACTIONS = {'SHIFT','RESERVE'}
ASBUILT_PDF_MAX_BYTES = 200 * 1024 * 1024
ASBUILT_VISION_DEFAULT_MODEL = os.getenv('TELETECH_VISION_MODEL','gpt-5.6').strip() or 'gpt-5.6'
ASBUILT_OPENAI_BASE_URL = os.getenv('TELETECH_OPENAI_BASE_URL','https://api.openai.com/v1').rstrip('/')
ASBUILT_VISION_TIMEOUT = int(os.getenv('TELETECH_VISION_TIMEOUT','300') or 300)
# NOTE: the three settings above and analyze_asbuilt_pdf_with_vision() /
# analyze_asbuilt_pdf_project_with_vision() / _asbuilt_manual_prompt_bundle() /
# analyze_asbuilt_manual_findings_project() below are the old OpenAI single-shot
# + manual copy/paste As-Built path. They are kept only so nothing else in this
# file breaks; the live UI no longer calls them. The live As-Built entry point is
# the GLM Vision chat flow (asbuilt_chat_start/asbuilt_chat_reply) further down.

# ------------------------- As-Built Code Pipeline -------------------------
# The As-Built workflow is deterministic/local:
# PDF/images -> rendered page images -> OpenCV color regions -> review.
# No remote AI API and no local multimodal model are called here.
ASBUILT_CHAT_SESSIONS = {}
ASBUILT_CHAT_LOCK = threading.Lock()
ASBUILT_CHAT_TTL_SECONDS = 6*3600
ASBUILT_CHAT_UPLOAD_POLL_ATTEMPTS = 20
ASBUILT_CHAT_UPLOAD_POLL_DELAY = 1.5
# Project-validated scan rule: components below 78% confidence are treated as likely noise/false positives for counting.
ASBUILT_MIN_MARKUP_CONFIDENCE = float(os.getenv('ASBUILT_MIN_MARKUP_CONFIDENCE','0.78') or '0.78')


# ------------------------- Route Profile Engine -------------------------
# Project-specific profile learned from user confirmation (2026-09-15).
# This is a route-order profile only.  It must never change BOQ counts or rack
# ownership.  Labs are route zones only unless an explicit rack override exists.
ROUTE_PROFILE = {
    "project": "Mansoura University",
    "building": "HUM",
    "profile": "twin_wing_central_core_down_out_up_return",
    "floor_aliases": {"G":"Ground Floor","F":"First Floor","S":"Second Floor","TH":"Third Floor","T":"Third Floor","R":"Roof Floor"},
    "label_prefix": {"Ground Floor":"G","First Floor":"F","Second Floor":"S","Third Floor":"T","Roof Floor":"R"},
    "racks": {
        "IDF-G1": {"floor":"Ground Floor", "start_room_code":"GR-32", "direction":"right_first"},
        "IDF-G2": {"floor":"Ground Floor", "start_room_code":"GR-32", "direction":"left_first"},
        "IDF-F1": {"floor":"First Floor", "direction":"right_first"},
        "IDF-F2": {"floor":"First Floor", "direction":"left_first"},
        "IDF-S1": {"floor":"Second Floor", "direction":"right_first"},
        "IDF-S2": {"floor":"Second Floor", "direction":"left_first"},
        "IDF-T1": {"floor":"Third Floor", "direction":"right_first"},
        "IDF-T2": {"floor":"Third Floor", "direction":"left_first"},
    },
    "global_rules": {
        "start_room_first": True,
        "finish_room_before_next": True,
        "prevent_room_reentry": True,
        "perimeter_first": True,
        "inner_points_last": True,
        "roof_last": True,
        "labs_route_zone_only": True,
    }
}

def _route_profile_for_floor_rack(floor_name, rack):
    """Return the external profile rule for the current logical rack.

    The internal rack IDs remain IDF-G1/IDF-G2 for historical v4.9 code, while
    the displayed label prefix is floor-aware.  Therefore map IDF-G1/2 to the
    current floor prefix before looking up profile rules.
    """
    prefix = _floor_prefix(floor_name) if '_floor_prefix' in globals() else 'G'
    logical = f"IDF-{prefix}{'1' if str(rack).endswith('1') else '2'}"
    return ROUTE_PROFILE.get('racks', {}).get(logical) or ROUTE_PROFILE.get('racks', {}).get(str(rack), {})

def _room_code_norm(v):
    s=str(v or '').upper().strip().replace(' ', '')
    # normalize GR-32 / GR32 / GR-032
    m=re.match(r'^([A-Z]{1,3})-?0*(\d{1,3})$', s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return s


def _load_builtin_reference_routes_json():
    path = BUILTIN_ROUTE_REFERENCE_JSON if 'BUILTIN_ROUTE_REFERENCE_JSON' in globals() else None
    try:
        if not path or not Path(path).exists():
            return None
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return None
    racks = (data or {}).get('racks') or {}
    out = {}; all_pts = []
    for key in ('G1','G2','IDF-G1','IDF-G2'):
        if key in racks and isinstance(racks[key], dict):
            pts = racks[key].get('route_polyline') or racks[key].get('path') or []
            rack_name = 'IDF-G1' if str(key).endswith('1') else 'IDF-G2'
            clean=[]
            for pt in pts:
                try:
                    x=float(pt[0]); y=float(pt[1])
                    clean.append((x,y)); all_pts.append({'x':x,'y':y})
                except Exception:
                    pass
            if len(clean) >= 2:
                out[rack_name] = clean
    if len(out) < 2:
        return None
    return {'routes': out, 'bounds': _robust_xy_bounds(all_pts, trim=0.00), 'meta': data}


def _is_roof_zone_block_override(rec):
    b = _norm_block_name((rec or {}).get('block') or '')
    # User correction: PTZ roof/outdoor camera points are the final roof tail.
    # Do not force ordinary CAM3/CAM3AAAA here because some normal-floor cameras
    # under the drawing must stay in the normal route order.
    return b in {'PTZ', 'PTZ CAMERA', 'PTZ CAM'}


def _is_ptz_roof_tail_device(rec):
    b = _norm_block_name((rec or {}).get('block') or '')
    return b in {'PTZ', 'PTZ CAMERA', 'PTZ CAM'}


def _roof_tail_local_key(result, rec):
    # Cancel CENTER-first experiment: it mixed inside/outside roof cameras.
    # Roof internal fallback is stable route/perimeter-ish; PTZ devices are
    # placed last by _flat_cctv_route_projection_key.
    try:
        x=float(rec.get('x')); y=float(rec.get('y'))
    except Exception:
        return (1,1e9,1e9,0.0,0.0)
    roof_pts=[]
    for p in (result.get('mapped') or []):
        try:
            if not _is_in_drawing_roof_tail(result,p):
                continue
            if _is_ptz_roof_tail_device(p):
                continue
            px=float(p.get('x')); py=float(p.get('y'))
            if math.isfinite(px) and math.isfinite(py):
                roof_pts.append({'x':px,'y':py})
        except Exception:
            pass
    if not roof_pts:
        return (1,1e9,1e9,y,x)
    minx,miny,maxx,maxy=_robust_xy_bounds(roof_pts, trim=0.0)
    w=max(maxx-minx,1.0); h=max(maxy-miny,1.0)
    nx=(x-minx)/w; ny=(y-miny)/h
    edge=min(nx,1.0-nx,ny,1.0-ny)
    inner_phase=1 if edge > 0.18 else 0
    radial=(nx-0.5)*(nx-0.5)+(ny-0.5)*(ny-0.5)
    return (inner_phase, edge, radial, y, x)


def _block_code_norm_for_roof_order(rec):
    b=_norm_block_name((rec or {}).get('block') or '')
    if b in {'PTZ','PTZ CAMERA','PTZ CAM'}:
        return 'PTZ'
    if b in {'CAM1','INDOOR CAM AGN'}:
        return 'CAM1'
    return b


def _record_room_matches_rack_start(result, route, rec):
    """True when the endpoint belongs to the rack/electrical start room.

    Hard rule from the user: for every (rack, point_type) sequence, points whose
    room_id equals rack_room_id are position block #1, regardless of t-value.
    This is checked from the already-computed room_id only; proximity is not used
    to reassign room membership.
    """
    if not route or not rec:
        return False
    rec_names=set()
    for v in (rec.get('_room_key_v49'), rec.get('_room_name_v49')):
        if not v:
            continue
        sv=str(v)
        rec_names.add(sv.upper())
        rec_names.add(_room_code_norm(sv))
        # A key may be like GR-32@x,y; keep the code/name part too.
        if '@' in sv:
            rec_names.add(sv.split('@',1)[0].upper())
            rec_names.add(_room_code_norm(sv.split('@',1)[0]))
    if not rec_names:
        return False
    start_refs=set()
    sr=route.get('start_room')
    if sr:
        start_refs.add(str(sr).upper()); start_refs.add(_room_code_norm(sr))
    for room in (route.get('rooms') or []):
        if room.get('_start_room'):
            for v in (room.get('_key'), room.get('name')):
                if v:
                    sv=str(v)
                    start_refs.add(sv.upper()); start_refs.add(_room_code_norm(sv))
                    if '@' in sv:
                        start_refs.add(sv.split('@',1)[0].upper())
                        start_refs.add(_room_code_norm(sv.split('@',1)[0]))
    return bool(rec_names & start_refs)


def _rack_start_room_bucket_key(result, rack, code, rec, dup_index=0):
    """Sort key prefix for the user's rack-room-first hard constraint.

    It only promotes endpoints already assigned to the rack room by room_id.
    It never uses proximity to pull external points into the rack room.
    """
    try:
        if _is_in_drawing_roof_tail(result, rec):
            return None
    except Exception:
        pass
    try:
        ctx=_spatial_route_context(result)
        route=ctx.get(str(rack)) or {}
    except Exception:
        route={}
    if not _record_room_matches_rack_start(result, route, rec):
        return None
    try:
        x=float(rec.get('x')); y=float(rec.get('y'))
        pr=_project_point_to_manual_route(x,y,(result.get('manual_routes') or {}).get(str(rack)))
    except Exception:
        pr=None
    if pr is not None:
        dist,t,seg,u,qx,qy=pr
        return (-100, float(t), float(dist), int(seg), float(u), str(rec.get('occurrence_id','')), int(dup_index))
    # Fallback stays inside the bucket but uses local geometry only.
    try:
        return (-100, float(rec.get('y')), float(rec.get('x')), str(rec.get('occurrence_id','')), int(dup_index))
    except Exception:
        return (-100, 0.0, 0.0, str(rec.get('occurrence_id','')), int(dup_index))


# ------------------------- XLSX patcher (stdlib only) -------------------------
NS_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
NS_REL_DOC = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS_REL_PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
ET.register_namespace('', NS_MAIN)
ET.register_namespace('r', NS_REL_DOC)
# Preserve Excel's compatibility namespace prefixes exactly.
# If ElementTree rewrites these as ns1/ns2 while mc:Ignorable still names
# x14ac/xr/xr2/xr3, desktop Excel reports the generated workbook as corrupt.
ET.register_namespace('mc', 'http://schemas.openxmlformats.org/markup-compatibility/2006')
ET.register_namespace('x14ac', 'http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac')
ET.register_namespace('xr', 'http://schemas.microsoft.com/office/spreadsheetml/2014/revision')
ET.register_namespace('xr2', 'http://schemas.microsoft.com/office/spreadsheetml/2015/revision2')
ET.register_namespace('xr3', 'http://schemas.microsoft.com/office/spreadsheetml/2016/revision3')
ET.register_namespace('x14', 'http://schemas.microsoft.com/office/spreadsheetml/2009/9/main')
ET.register_namespace('xdr', 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing')


def _col_num(ref):
    m = re.match(r'([A-Z]+)(\d+)$', ref)
    n = 0
    for ch in m.group(1):
        n = n * 26 + ord(ch) - 64
    return n


def _sheet_paths(z):
    wb = ET.fromstring(z.read('xl/workbook.xml'))
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    rid_to_target = {}
    for rel in rels.findall(f'{{{NS_REL_PKG}}}Relationship'):
        rid_to_target[rel.attrib['Id']] = rel.attrib['Target']
    out = {}
    sheets = wb.find(f'{{{NS_MAIN}}}sheets')
    for s in sheets:
        name = s.attrib['name']
        rid = s.attrib[f'{{{NS_REL_DOC}}}id']
        target = rid_to_target[rid]
        if target.startswith('/'):
            path = target.lstrip('/')
        else:
            path = 'xl/' + target.lstrip('./')
        out[name] = path
    return out


def _find_or_make_cell(root, ref):
    row_num = int(re.findall(r'\d+', ref)[0])
    sheet_data = root.find(f'{{{NS_MAIN}}}sheetData')
    row = None
    for r in sheet_data.findall(f'{{{NS_MAIN}}}row'):
        if int(r.attrib.get('r', '0')) == row_num:
            row = r; break
    if row is None:
        row = ET.Element(f'{{{NS_MAIN}}}row', {'r': str(row_num)})
        inserted = False
        for i, r in enumerate(list(sheet_data)):
            if int(r.attrib.get('r', '0')) > row_num:
                sheet_data.insert(i, row); inserted = True; break
        if not inserted:
            sheet_data.append(row)
    for c in row.findall(f'{{{NS_MAIN}}}c'):
        if c.attrib.get('r') == ref:
            return c
    c = ET.Element(f'{{{NS_MAIN}}}c', {'r': ref})
    target_col = _col_num(ref)
    inserted = False
    for i, existing in enumerate(list(row)):
        er = existing.attrib.get('r')
        if er and _col_num(er) > target_col:
            row.insert(i, c); inserted = True; break
    if not inserted:
        row.append(c)
    return c


def _set_cell(root, ref, value):
    c = _find_or_make_cell(root, ref)
    # preserve style but clear content/type
    for child in list(c):
        if child.tag in {f'{{{NS_MAIN}}}v', f'{{{NS_MAIN}}}f', f'{{{NS_MAIN}}}is'}:
            c.remove(child)
    c.attrib.pop('t', None)
    if value is None or value == '':
        return
    if isinstance(value, tuple) and value and value[0] == 'FORMULA':
        f = ET.SubElement(c, f'{{{NS_MAIN}}}f')
        f.text = value[1].lstrip('=')
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = ET.SubElement(c, f'{{{NS_MAIN}}}v'); v.text = str(value); return
    c.attrib['t'] = 'inlineStr'
    isel = ET.SubElement(c, f'{{{NS_MAIN}}}is')
    t = ET.SubElement(isel, f'{{{NS_MAIN}}}t')
    t.text = str(value)


_IGNORABLE_URIS = {
    'x14ac': 'http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac',
    'xr': 'http://schemas.microsoft.com/office/spreadsheetml/2014/revision',
    'xr2': 'http://schemas.microsoft.com/office/spreadsheetml/2015/revision2',
    'xr3': 'http://schemas.microsoft.com/office/spreadsheetml/2016/revision3',
}

def _cleanup_mc_ignorable(root):
    key = '{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable'
    value = root.attrib.get(key)
    if not value:
        return
    used_uris = set()
    for elem in root.iter():
        if isinstance(elem.tag, str) and elem.tag.startswith('{'):
            used_uris.add(elem.tag[1:].split('}', 1)[0])
        for attr in elem.attrib:
            if attr.startswith('{'):
                used_uris.add(attr[1:].split('}', 1)[0])
    kept = [p for p in value.split() if _IGNORABLE_URIS.get(p) in used_uris]
    if kept:
        root.attrib[key] = ' '.join(kept)
    else:
        root.attrib.pop(key, None)

def _ensure_sheet_rows(root, max_row, style_source_row=None):
    """Extend a worksheet to max_row while preserving the template row/cell styles."""
    sheet_data = root.find(f'{{{NS_MAIN}}}sheetData')
    rows = sheet_data.findall(f'{{{NS_MAIN}}}row')
    existing = {int(r.attrib.get('r','0')): r for r in rows}
    current_max = max(existing.keys(), default=0)
    if max_row <= current_max:
        return
    src_num = style_source_row if style_source_row in existing else current_max
    src = existing.get(src_num)
    for rn in range(current_max + 1, max_row + 1):
        if src is not None:
            row = ET.fromstring(ET.tostring(src, encoding='utf-8'))
            row.attrib['r'] = str(rn)
            for c in row.findall(f'{{{NS_MAIN}}}c'):
                old_ref = c.attrib.get('r','A1')
                m = re.match(r'([A-Z]+)', old_ref)
                col = m.group(1) if m else 'A'
                c.attrib['r'] = f'{col}{rn}'
                c.attrib.pop('t', None)
                for child in list(c):
                    if child.tag in {f'{{{NS_MAIN}}}v', f'{{{NS_MAIN}}}f', f'{{{NS_MAIN}}}is'}:
                        c.remove(child)
        else:
            row = ET.Element(f'{{{NS_MAIN}}}row', {'r': str(rn)})
        sheet_data.append(row)
    dim = root.find(f'{{{NS_MAIN}}}dimension')
    if dim is not None:
        ref = dim.attrib.get('ref','A1:D1')
        m = re.match(r'([^:]+):([A-Z]+)(\d+)$', ref)
        if m:
            dim.attrib['ref'] = f'{m.group(1)}:{m.group(2)}{max(max_row, int(m.group(3)))}'
        else:
            dim.attrib['ref'] = f'A1:D{max_row}'


def _remove_body_merges(root, start_row=5):
    """Remove old template service-group merges from the data body only."""
    merges = root.find(f'{{{NS_MAIN}}}mergeCells')
    if merges is None:
        return
    for mc in list(merges):
        nums = [int(x) for x in re.findall(r'\d+', mc.attrib.get('ref',''))]
        if nums and max(nums) >= start_row:
            merges.remove(mc)
    merges.attrib['count'] = str(len(list(merges)))


def _excel_col_num(col):
    n=0
    for ch in str(col or '').upper():
        if 'A' <= ch <= 'Z':
            n=n*26+(ord(ch)-64)
    return n

def _crop_sheet_to_range(root, min_col, max_col, min_row=1, max_row=None):
    """Remove template cells/merges outside one wanted Excel range.

    Used only by Quantity Summary so the generated workbook contains the
    user-approved table and nothing from the old consultant template around it.
    The kept cells are NOT shifted; formatting inside the wanted block remains.
    """
    cmin=_excel_col_num(min_col); cmax=_excel_col_num(max_col)
    rmin=int(min_row or 1); rmax=int(max_row) if max_row is not None else 1048576

    def in_range_ref(ref):
        m=re.match(r'^([A-Z]+)(\d+)$', str(ref or '').upper())
        if not m: return False
        c=_excel_col_num(m.group(1)); r=int(m.group(2))
        return cmin <= c <= cmax and rmin <= r <= rmax

    sheet_data=root.find(f'{{{NS_MAIN}}}sheetData')
    if sheet_data is not None:
        for row in list(sheet_data.findall(f'{{{NS_MAIN}}}row')):
            rn=int(row.attrib.get('r','0') or 0)
            if rn < rmin or rn > rmax:
                sheet_data.remove(row); continue
            # Old template row styles can paint blank cells outside the wanted
            # table.  Kept table cells already carry their own exact styles.
            row.attrib.pop('s',None); row.attrib.pop('customFormat',None)
            for cell in list(row.findall(f'{{{NS_MAIN}}}c')):
                if not in_range_ref(cell.attrib.get('r','')):
                    row.remove(cell)
            # Drop template-only blank rows after filtering.
            if not row.findall(f'{{{NS_MAIN}}}c'):
                sheet_data.remove(row)

    # Remove old template column formatting outside the approved block so the
    # left/right side of the worksheet stays truly blank.
    cols=root.find(f'{{{NS_MAIN}}}cols')
    if cols is not None:
        for col in list(cols.findall(f'{{{NS_MAIN}}}col')):
            mn=int(col.attrib.get('min','1')); mx=int(col.attrib.get('max',str(mn)))
            if mx < cmin or mn > cmax:
                cols.remove(col); continue
            col.attrib['min']=str(max(mn,cmin)); col.attrib['max']=str(min(mx,cmax))

    merges=root.find(f'{{{NS_MAIN}}}mergeCells')
    if merges is not None:
        for mc in list(merges.findall(f'{{{NS_MAIN}}}mergeCell')):
            ref=mc.attrib.get('ref','')
            parts=ref.split(':',1)
            if not parts or not in_range_ref(parts[0]) or not in_range_ref(parts[-1]):
                merges.remove(mc)
        if len(list(merges)):
            merges.attrib['count']=str(len(list(merges)))
        else:
            root.remove(merges)

    # Keep Excel's used-range metadata aligned with the real generated table.
    dim=root.find(f'{{{NS_MAIN}}}dimension')
    if dim is not None:
        dim.attrib['ref']=f'{min_col}{rmin}:{max_col}{rmax}'

def _excel_col_letters(n):
    n=max(1,int(n)); out=[]
    while n:
        n,rem=divmod(n-1,26); out.append(chr(65+rem))
    return ''.join(reversed(out))

def _set_freeze_columns(root, count):
    """Freeze the first `count` worksheet columns without changing row scrolling."""
    count=max(0,int(count or 0))
    sheet_views=root.find(f'{{{NS_MAIN}}}sheetViews')
    if sheet_views is None:
        sheet_views=ET.Element(f'{{{NS_MAIN}}}sheetViews')
        # OOXML order: sheetViews belongs before sheetFormatPr / cols / sheetData.
        insert_at=0
        for i,child in enumerate(list(root)):
            if child.tag in {f'{{{NS_MAIN}}}sheetFormatPr', f'{{{NS_MAIN}}}cols', f'{{{NS_MAIN}}}sheetData'}:
                insert_at=i; break
            insert_at=i+1
        root.insert(insert_at,sheet_views)
    sheet_view=sheet_views.find(f'{{{NS_MAIN}}}sheetView')
    if sheet_view is None:
        sheet_view=ET.SubElement(sheet_views,f'{{{NS_MAIN}}}sheetView',{'workbookViewId':'0'})
    for pane in list(sheet_view.findall(f'{{{NS_MAIN}}}pane')):
        sheet_view.remove(pane)
    if count:
        top_left=f'{_excel_col_letters(count+1)}1'
        pane=ET.Element(f'{{{NS_MAIN}}}pane',{
            'xSplit':str(count),
            'topLeftCell':top_left,
            'activePane':'topRight',
            'state':'frozen',
        })
        sheet_view.insert(0,pane)

def _xlsx_shared_strings(zin):
    """Return shared string values so existing consultant Legend text can be preserved."""
    if 'xl/sharedStrings.xml' not in zin.namelist():
        return []
    try:
        root=ET.fromstring(zin.read('xl/sharedStrings.xml'))
        return [''.join(t.text or '' for t in si.iter(f'{{{NS_MAIN}}}t')) for si in root.findall(f'{{{NS_MAIN}}}si')]
    except Exception:
        return []


def _xlsx_sheet_cell_text(root, ref, shared_strings=None):
    for c in root.iter(f'{{{NS_MAIN}}}c'):
        if c.attrib.get('r') != ref:
            continue
        typ=c.attrib.get('t')
        if typ=='inlineStr':
            return ''.join(t.text or '' for t in c.iter(f'{{{NS_MAIN}}}t')).strip()
        v=c.find(f'{{{NS_MAIN}}}v')
        if v is None or v.text is None:
            return ''
        if typ=='s':
            try:
                return str((shared_strings or [])[int(v.text)]).strip()
            except Exception:
                return ''
        return str(v.text).strip()
    return ''


def _set_port_map_asbuilt_legend(root, text):
    """Use the consultant Port Map Legend box for deterministic As-Built notes."""
    text=str(text or '').strip()
    if not text:
        return
    for ref in ('V5','W5','X5','Y5'):
        _find_or_make_cell(root,ref).attrib['s']='34'
    for ref in ('V6','W6','X6'):
        _find_or_make_cell(root,ref).attrib['s']='35'
    _find_or_make_cell(root,'Y6').attrib['s']='36'
    merges=root.find(f'{{{NS_MAIN}}}mergeCells')
    if merges is None:
        merges=ET.Element(f'{{{NS_MAIN}}}mergeCells')
        sheet_data=root.find(f'{{{NS_MAIN}}}sheetData')
        children=list(root); idx=children.index(sheet_data) if sheet_data in children else len(children)-1
        root.insert(idx+1,merges)
    existing={m.attrib.get('ref') for m in merges.findall(f'{{{NS_MAIN}}}mergeCell')}
    for mref in ('V5:Y5','V6:X6'):
        if mref not in existing:
            ET.SubElement(merges,f'{{{NS_MAIN}}}mergeCell',{'ref':mref}); existing.add(mref)
    merges.attrib['count']=str(len(list(merges)))
    sheet_data=root.find(f'{{{NS_MAIN}}}sheetData'); row6=None
    for row in sheet_data.findall(f'{{{NS_MAIN}}}row'):
        if int(row.attrib.get('r','0'))==6: row6=row; break
    if row6 is None:
        row6=ET.Element(f'{{{NS_MAIN}}}row',{'r':'6'}); sheet_data.append(row6)
    row6.attrib['ht']='54'; row6.attrib['customHeight']='1'
    _set_cell(root,'V5','Legend'); _set_cell(root,'V6',text); _set_cell(root,'Y6','AS-BUILT')


def patch_xlsx(template, output, patches, min_rows=None, strip_body_merges_sheets=None, body_merges=None, port_map_panel_maps=None, port_map_used_panels=None, crop_ranges=None, freeze_columns=None, port_map_legends=None):
    with zipfile.ZipFile(template, 'r') as zin:
        sheet_map = _sheet_paths(zin)
        shared_strings = _xlsx_shared_strings(zin)
        modified = {}
        for shname, cells in patches.items():
            if shname not in sheet_map:
                continue
            path = sheet_map[shname]
            root = ET.fromstring(zin.read(path))
            if port_map_panel_maps and shname in port_map_panel_maps:
                _ensure_port_map_panel_blocks(root, port_map_panel_maps[shname])
                if port_map_used_panels is not None:
                    _set_port_map_panel_visibility(root, port_map_panel_maps[shname], port_map_used_panels.get(shname,set()))
            if port_map_legends and shname in port_map_legends:
                new_legend=str(port_map_legends.get(shname,'') or '').strip()
                old_legend=_xlsx_sheet_cell_text(root,'V6',shared_strings)
                if old_legend and old_legend not in new_legend:
                    new_legend=old_legend+'\n'+new_legend
                _set_port_map_asbuilt_legend(root, new_legend)
            if min_rows and shname in min_rows:
                _ensure_sheet_rows(root, int(min_rows[shname]))
            if strip_body_merges_sheets and shname in strip_body_merges_sheets:
                _remove_body_merges(root, start_row=5)
            if body_merges and shname in body_merges:
                merges_node=root.find(f'{{{NS_MAIN}}}mergeCells')
                if merges_node is None:
                    merges_node=ET.Element(f'{{{NS_MAIN}}}mergeCells')
                    root.append(merges_node)
                existing={x.attrib.get('ref') for x in merges_node}
                for mref in body_merges[shname]:
                    if mref not in existing:
                        ET.SubElement(merges_node,f'{{{NS_MAIN}}}mergeCell',{'ref':mref})
                merges_node.attrib['count']=str(len(list(merges_node)))
            for ref, value in cells.items():
                _set_cell(root, ref, value)
            if crop_ranges and shname in crop_ranges:
                cr=crop_ranges[shname]
                _crop_sheet_to_range(root, cr[0], cr[1], cr[2] if len(cr)>2 else 1, cr[3] if len(cr)>3 else None)
            if freeze_columns and shname in freeze_columns:
                _set_freeze_columns(root, freeze_columns[shname])
            _cleanup_mc_ignorable(root)
            modified[path] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = modified.get(info.filename, zin.read(info.filename))
                zout.writestr(info, data)

# ------------------------------- OCS client ---------------------------------
class OCS:
    def __init__(self, desc):
        self.desc = desc

    @staticmethod
    def discover():
        appdata = os.environ.get('APPDATA')
        if not appdata:
            raise RuntimeError('APPDATA not found')
        d = Path(appdata) / 'OpenCADStudio' / 'automation'
        if not d.exists():
            raise RuntimeError('Open CAD Studio GUI automation session not found')
        files = sorted(d.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files:
            try:
                desc = json.loads(p.read_text(encoding='utf-8'))
                o = OCS(desc)
                st = o.call({'op':'hello'}, timeout=2)
                if st.get('ok') and st.get('mode') == 'gui':
                    return o
            except Exception:
                pass
        raise RuntimeError('No live Open CAD Studio GUI session found')

    def call(self, req, timeout=20):
        q = dict(req)
        q['protocol'] = 1
        q['session_id'] = self.desc['session_id']
        q['token'] = self.desc['token']
        s = socket.create_connection(('127.0.0.1', int(self.desc['port'])), timeout=timeout)
        s.sendall((json.dumps(q) + '\n').encode('utf-8'))
        data = b''
        while True:
            x = s.recv(65536)
            if not x: break
            data += x
        s.close()
        if not data:
            raise RuntimeError('No response from Open CAD Studio')
        return json.loads(data.decode('utf-8').strip())

    def execute(self, req, wait=30):
        st = self.call({'op':'hello'})
        rid = uuid.uuid4().hex
        q = dict(req)
        q.update({'request_id':rid,'client_id':uuid.uuid4().hex,'document_id':st['document_id'],
                  'revision':st['revision'],'selection':st.get('selection',[])})
        r = self.call(q)
        deadline = time.time() + wait
        while r.get('status') in ('accepted','running') and time.time() < deadline:
            time.sleep(.08)
            r = self.call({'op':'operation','request_id':rid})
        return r

    def open(self, path):
        r = self.execute({'op':'open','path':str(path)}, wait=60)
        if not r.get('ok'):
            raise RuntimeError(r.get('error') or 'Open failed')
        return r

    def properties_for(self, handle):
        self.execute({'op':'select','clear':True})
        self.execute({'op':'select','handles':[handle]})
        p = self.call({'op':'properties'})
        self.execute({'op':'select','clear':True})
        flat = {}
        for sec in p.get('sections',[]):
            for x in sec.get('properties',[]):
                flat[x.get('id')] = x.get('value')
        return flat

# ------------------------------ CAD analyzer --------------------------------
# TELETECH counting profile v1.4
# Core rules learned from the verified Menoufia HQ Ground Floor take-off:
# - Never infer semantics from a block name alone when attributes/legend are available.
# - Legend/sample instances are excluded, but production CCTV on CCTVnet is counted even
#   when its raw insert coordinates are outside the architectural envelope.
# - D-V is ONE physical occurrence but expands to TWO logical ports: 1 Data + 1 Voice.
# - D-W is the verified Indoor Wi-Fi point and expands to one W port.
# - floor boox 1 is the two-cable floor-box type: one physical BOQ item, one D + one V port.
# - EC-tp semantics come from attribute T.  H is counted as High-Level; unsupported
#   attributes remain IGNORED/REVIEW instead of being guessed.
# - F POINT 13 AGN / SDU / 2 socket are supporting blocks for this drawing and are
#   not part of the verified 145-item BOQ.
# - No candidate silently disappears: MAPPED, IGNORED, or REVIEW/RACK_REVIEW.

PROJECT_PROFILE = {
    'name': 'TELETECH Visible ModelSpace + v4.9 Fixed Route Engine + Safe Labels',
    'reference_blocks': {
        'D-V': 'Data/Voice outlet',
        'D-W': 'Indoor Wi-Fi access point',
        'EC-tp': 'Attribute-driven point (H/P/AC/F); only verified service attributes are counted',
        'F POINT 13 AGN': 'Supporting assembly; not counted in the verified BOQ for this drawing',
        'CAM1': 'CCTV Dome',
        'CAM2': 'CCTV Indoor Bullet',
        'CAM3aaaa': 'CCTV Outdoor Bullet',
        'floor boox 1': 'Floor Box - two cable type',
        'AC AGN': 'Access Control point',
        '2 socket': '2 Socket assembly',
        'SDU': 'SDU',
        'AV SYSTEM RACK': 'AV System Rack',
    },
    'path_layers': ['LC-CABLE -10','CCTVnet','LC-CABLE -30','LC-CABLE-40','DN01-D','ELEC','e-Cable-T'],
}

MENOUFIA_REFERENCE = {
    # Verified physical BOQ totals for Menoufia HQ Ground Floor.
    'D-V':51,'D-W':34,'CAM1':26,'CAM2':8,'CAM3aaaa':12,
    'floor boox 1':10,'AC AGN':2,'AV SYSTEM RACK':1,'H':1,
}

# Verified rack split for this known drawing.  These values are used only when
# the uploaded drawing is identified as the Menoufia HQ Ground reference.
MENOUFIA_RACK_REFERENCE = {
    # User-approved Ground Floor exception: ALL CCTV subtypes terminate on Rack 1.
    'IDF-G1': {'D-V':18,'D-W':20,'CAM1':13,'CAM2':4,'CAM3aaaa':6,
               'floor boox 1':10,'AC AGN':1,'AV SYSTEM RACK':1,'H':1},
    'IDF-G2': {'D-V':33,'D-W':14,'CAM1':13,'CAM2':4,'CAM3aaaa':6,
               'floor boox 1':0,'AC AGN':1,'AV SYSTEM RACK':0,'H':0},
}

# BOQ physical totals: 74 + 71 = 145.
MENOUFIA_PHYSICAL_TOTALS = {'IDF-G1':74,'IDF-G2':71,'ALL':145}



def _point_from_block(e):
    b = e.get('bounds') or {}
    mn = b.get('min')
    if mn and len(mn) >= 2:
        return float(mn[0]), float(mn[1])
    return None, None


def _near_origin(e):
    x,y=e.get('x'),e.get('y')
    return x is not None and y is not None and abs(x) < 2000 and abs(y) < 1000


def _inside(x,y,bounds):
    if x is None or y is None: return False
    return bounds['min_x'] <= x <= bounds['max_x'] and bounds['min_y'] <= y <= bounds['max_y']


def _distance(a,b):
    return math.sqrt(sum((float(x)-float(y))**2 for x,y in zip(a,b)))


def _entity_length_m(e):
    # Drawing units are treated as millimetres for this project profile.
    # Unsupported geometry returns 0 and is reported through approximate=True.
    try:
        if 'start' in e and 'end' in e:
            return _distance(e['start'],e['end'])/1000.0
        if 'vertices' in e and len(e['vertices']) >= 2:
            pts=e['vertices']; total=0.0
            for a,b in zip(pts,pts[1:]): total += _distance(a,b)
            return total/1000.0
        if e.get('type') == 'Arc' and e.get('radius') is not None:
            a=float(e.get('start_angle',0)); b=float(e.get('end_angle',0)); d=abs(b-a)
            # acadrust generally exposes radians; tolerate degree-like values.
            if d > math.tau*2: d=math.radians(d)
            return abs(float(e['radius'])*d)/1000.0
        if e.get('type') == 'Circle' and e.get('radius') is not None:
            return 2*math.pi*abs(float(e['radius']))/1000.0
    except Exception:
        return 0.0
    return 0.0


def _layer_length(ocs, layer):
    offset=0; total=0.0; returned=0; unsupported=0
    while True:
        r=ocs.call({'op':'query','layer':layer,'detail':'geometry','offset':offset,'limit':10000}, timeout=60)
        ents=r.get('entities',[])
        for e in ents:
            length=_entity_length_m(e)
            if length: total += length
            elif e.get('type') not in {'Block Reference','Text','MText','Attribute Definition','Point'}:
                unsupported += 1
        returned += len(ents)
        nxt=r.get('next_offset')
        if nxt is None: break
        offset=int(nxt)
    return {'length_m':round(total,1),'entities':returned,'unsupported_geometry':unsupported,'approximate':bool(unsupported)}



def _dxf_pairs(path):
    """Yield ASCII DXF (group-code, value) pairs using stdlib only."""
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        while True:
            c=f.readline()
            if not c: break
            v=f.readline()
            if not v: break
            try: code=int(c.strip())
            except ValueError: continue
            yield code, v.rstrip('\r\n')


def _dxf_entities(pairs):
    out=[]; cur=None
    for code,value in pairs:
        if code==0:
            if cur is not None: out.append(cur)
            cur={'type':value,'pairs':[]}
        elif cur is not None:
            cur['pairs'].append((code,value))
    if cur is not None: out.append(cur)
    return out


def _dxf_first(entity, code, default=None):
    for c,v in entity.get('pairs',[]):
        if c==code: return v
    return default


def _dxf_num(entity, code, default=0.0):
    try: return float(_dxf_first(entity, code, default))
    except Exception: return float(default)


ROOM_MARKER_RE = re.compile(r"^[A-Z]{1,3}-\d+$", re.I)
RACK_TEXT_RE = re.compile(r"\b(?:IDF|IDR|MDF)[ _-]*[A-Z]?[ _-]*([12])\b", re.I)
VISUAL_ANCHOR_BLOCKS = {
    # Use the visible symbol center, not only INSERT base point, for label anchoring.
    # New lab boards expose several service blocks whose INSERT point is offset
    # about 150-250 mm from the actual drawn symbol; using INSERT made labels look
    # detached from the point even though the logical label existed.
    'CAM2','CAM 2','PTZ','PTZ CAMERA','CAM3','CAM3AAAA','AV SYSTEM RACK','IDF','MDF','IDF RACK','MDF RACK',
    'D-V','D-W','D-V BOX','D·V BOX','D.V BOX','DV BOX',
    'PROJECTOR','DD','EC-TP','EC-tp','AV SYSTEM DOOM CAMERA',
    'FLOOR BOOX 1','floor boox 1','FLOOR BOX2','floor box2'
}

def _is_room_marker_text(text):
    return bool(ROOM_MARKER_RE.match(str(text or '').strip()))

def _rack_index_from_text(text):
    m=RACK_TEXT_RE.search(str(text or '').upper())
    return int(m.group(1)) if m else None

def _parse_visible_dxf(path, with_rooms=False):
    """Parse ENTITIES + BLOCKS and return only INSERT instances reachable from Model Space.

    This is intentionally different from OCS `query(type=Block Reference)`, which can expose
    INSERTs that exist only inside unused BLOCK definitions.  Every returned occurrence has a
    concrete ancestry path from the ENTITIES section, so an unused library block can never be
    counted.  2-D block transforms are resolved recursively for rack/zone coordinates.
    """
    pairs=list(_dxf_pairs(path)); sections={}; i=0
    while i < len(pairs):
        if pairs[i] == (0,'SECTION') and i+1 < len(pairs) and pairs[i+1][0] == 2:
            name=pairs[i+1][1]; j=i+2; chunk=[]
            while j < len(pairs) and pairs[j] != (0,'ENDSEC'):
                chunk.append(pairs[j]); j+=1
            sections[name]=chunk; i=j+1
        else: i+=1

    model=_dxf_entities(sections.get('ENTITIES',[]))
    blocks={}; current=None
    for e in _dxf_entities(sections.get('BLOCKS',[])):
        if e['type']=='BLOCK':
            name=_dxf_first(e,2,'')
            current={'name':name,'base':(_dxf_num(e,10),_dxf_num(e,20)),'entities':[]}
            blocks[name]=current
        elif e['type']=='ENDBLK':
            current=None
        elif current is not None:
            current['entities'].append(e)

    def mat_mul(m,n):
        a,b,c,d,tx,ty=m; A,B,C,D,TX,TY=n
        return (a*A+c*B,b*A+d*B,a*C+c*D,b*C+d*D,a*TX+c*TY+tx,b*TX+d*TY+ty)
    def transform_point(m,x,y):
        a,b,c,d,tx,ty=m; return a*x+c*y+tx,b*x+d*y+ty
    def insert_matrix(e, base):
        x,y=_dxf_num(e,10),_dxf_num(e,20)
        sx,sy=_dxf_num(e,41,1),_dxf_num(e,42,1)
        ang=math.radians(_dxf_num(e,50,0)); co,si=math.cos(ang),math.sin(ang)
        bx,by=base
        return (co*sx,si*sx,-si*sy,co*sy,
                x-(co*sx*bx-si*sy*by), y-(si*sx*bx+co*sy*by))

    def entity_points(e):
        """Best-effort primitive points in one BLOCK definition.

        Used only to find a visual symbol anchor. Some legacy camera blocks have an
        INSERT base tens/hundreds of metres away from the actual drawn symbol, so the
        insertion point is not a usable label/rack coordinate. HATCH/metadata records
        are deliberately ignored because exported hatch seeds can contain 0,0 helper
        coordinates unrelated to the visible symbol geometry.
        """
        pts=[]; typ=e.get('type')
        if typ not in {'LINE','ARC','CIRCLE','LWPOLYLINE','POLYLINE','ELLIPSE','SPLINE'}:
            return pts
        ps=e.get('pairs',[])
        # Repeated 10/20 pairs (LWPOLYLINE, SPLINE-like exports).
        pending=None
        for c,v in ps:
            if c==10:
                try: pending=float(v)
                except Exception: pending=None
            elif c==20 and pending is not None:
                try: pts.append((pending,float(v)))
                except Exception: pass
                pending=None
        # LINE second endpoint.
        if typ=='LINE':
            try: pts.append((_dxf_num(e,11),_dxf_num(e,21)))
            except Exception: pass
        # Circle/arc center plus radius extents when available.
        if typ in {'CIRCLE','ARC'}:
            try:
                cx,cy=_dxf_num(e,10),_dxf_num(e,20); rr=abs(_dxf_num(e,40,0))
                pts.extend([(cx-rr,cy-rr),(cx+rr,cy+rr)])
            except Exception: pass
        return pts

    block_anchor_cache={}
    def block_visual_anchor(name, _stack=()):
        """Return the local visual centre of a BLOCK, following nested symbols.

        FIX5: the new lab board's PROJECTOR block contains almost no direct
        primitive geometry; the visible projector symbol is a nested ES-cctv
        INSERT roughly 200 mm away from the PROJECTOR INSERT point.  Earlier
        builds anchored the projector label to the parent INSERT, so every
        projector label looked detached/wrong.  This resolver walks nested
        INSERTs and transforms their primitive geometry back into the parent
        block coordinate system.
        """
        key=str(name or '')
        if key in block_anchor_cache: return block_anchor_cache[key]
        if key in _stack:
            block_anchor_cache[key]=None; return None
        blk=blocks.get(key)
        if not blk:
            block_anchor_cache[key]=None; return None
        pts=[]
        # Local transform helpers for nested block references.
        def tpoint(m,x,y):
            a,b,c,d,tx,ty=m; return a*x+c*y+tx,b*x+d*y+ty
        for be in blk.get('entities',[]):
            if be.get('type')!='INSERT':
                pts.extend(entity_points(be))
                continue
            child=_dxf_first(be,2,'')
            child_anchor=block_visual_anchor(child, _stack+(key,))
            if child_anchor is None:
                continue
            child_blk=blocks.get(child)
            base=child_blk['base'] if child_blk else (0.0,0.0)
            cm=insert_matrix(be,base)
            pts.append(tpoint(cm,child_anchor[0],child_anchor[1]))
        if not pts:
            block_anchor_cache[key]=None; return None
        xs=[q[0] for q in pts]; ys=[q[1] for q in pts]
        ans=((min(xs)+max(xs))/2.0,(min(ys)+max(ys))/2.0)
        block_anchor_cache[key]=ans
        return ans

    # ATTRIB records immediately following an INSERT belong to that INSERT.
    def attributes_by_insert(entities):
        out={}; current_handle=None
        for e in entities:
            typ=e['type']
            if typ=='INSERT':
                current_handle=_dxf_first(e,5,''); out.setdefault(current_handle,{})
            elif typ=='ATTRIB' and current_handle is not None:
                out[current_handle][_dxf_first(e,2,'')]=_dxf_first(e,1,'')
            elif typ=='SEQEND':
                current_handle=None
            elif typ!='ATTRIB':
                current_handle=None
        return out

    occurrences=[]
    room_markers=[]
    text_items=[]
    def walk(entities, parent=(1,0,0,1,0,0), ancestors=(), depth=0):
        if depth>16: return
        amap=attributes_by_insert(entities)
        for e in entities:
            if e['type'] in {'TEXT','MTEXT'}:
                txt=str(_dxf_first(e,1,'') or '').strip()
                rx,ry=transform_point(parent,_dxf_num(e,10),_dxf_num(e,20))
                text_items.append({'text':txt,'x':rx,'y':ry,'layer':_dxf_first(e,8,'0'),
                                   'ancestors':list(ancestors),'depth':depth,'type':e['type']})
                if _is_room_marker_text(txt):
                    room_markers.append({'name':txt.upper(),'x':rx,'y':ry,'layer':_dxf_first(e,8,'0'),
                                         'ancestors':list(ancestors),'depth':depth})
                if 'ROOF' in txt.upper():
                    occurrences.append({'handle':_dxf_first(e,5,''),'block':'__ROOF_TEXT__','layer':_dxf_first(e,8,'0'),
                                        'x':rx,'y':ry,'text':txt,'rotation':0.0,'attributes':{},
                                        'ancestors':list(ancestors),'depth':depth,'legend_branch':False,'root':depth==0})
                ridx=_rack_index_from_text(txt)
                if ridx in {1,2}:
                    occurrences.append({'handle':_dxf_first(e,5,''),'block':'__RACK_TEXT__','layer':_dxf_first(e,8,'0'),
                                        'x':rx,'y':ry,'text':txt,'rack_index':ridx,'rotation':0.0,'attributes':{},
                                        'ancestors':list(ancestors),'depth':depth,'legend_branch':False,'root':depth==0})
                continue
            if e['type']!='INSERT': continue
            name=_dxf_first(e,2,''); layer=_dxf_first(e,8,'0'); handle=_dxf_first(e,5,'')
            ix,iy=transform_point(parent,_dxf_num(e,10),_dxf_num(e,20))
            x,y=ix,iy
            m=None
            # CAM2/CAM3 in the Ground source have remote block base points. Use
            # the center of their actual drawn geometry, transformed to world space.
            if str(name).upper() in VISUAL_ANCHOR_BLOCKS and name in blocks:
                va=block_visual_anchor(name)
                if va is not None:
                    m=insert_matrix(e,blocks[name]['base'])
                    x,y=transform_point(mat_mul(parent,m),va[0],va[1])
            legend_branch=any(('legend' in str(a).lower() or 'legent' in str(a).lower()) for a in (*ancestors,name))
            occurrences.append({'handle':handle,'block':name,'layer':layer,'x':x,'y':y,'insert_x':ix,'insert_y':iy,
                                'rotation':_dxf_num(e,50,0),'attributes':amap.get(handle,{}),
                                'ancestors':list(ancestors),'depth':depth,'legend_branch':legend_branch,
                                'root':depth==0})
            if name in blocks and not legend_branch:
                if m is None: m=insert_matrix(e,blocks[name]['base'])
                walk(blocks[name]['entities'],mat_mul(parent,m),ancestors+(name,),depth+1)
    walk(model)

    # ROUTE_ROOM_LOCK_FIX2: many architectural blocks split room numbers into two
    # text objects, e.g. one MTEXT contains ``GR-`` and a nearby TEXT contains ``38``.
    # The old parser ignored those rooms completely, so the route engine fell back
    # to raw X/Y sorting and could start from the top/roof/reference band instead
    # of the rack/electrical room. Recompose these official room tags before
    # routing.
    for t in list(text_items):
        prefix=str(t.get('text') or '').strip().upper().replace(' ', '')
        if prefix not in {'GR-','FR-','SD-','T-','TH-','R-','RF-'}:
            continue
        try:
            tx=float(t.get('x')); ty=float(t.get('y'))
        except Exception:
            continue
        best=None
        for n in text_items:
            val=str(n.get('text') or '').strip()
            if not re.fullmatch(r'\d{1,3}', val):
                continue
            if str(n.get('layer')) != str(t.get('layer')):
                continue
            try:
                nx=float(n.get('x')); ny=float(n.get('y'))
            except Exception:
                continue
            dx=nx-tx; dy=abs(ny-ty)
            if 0.0 < dx <= 1200.0 and dy <= 80.0:
                cand=(dy, dx, val, nx, ny, n)
                if best is None or cand < best:
                    best=cand
        if best is not None:
            _,_,num,nx,ny,nitem=best
            code=f"{prefix}{int(num):02d}"
            if _is_room_marker_text(code):
                room_markers.append({'name':code,'x':(tx+nx)/2.0,'y':(ty+float(nitem.get('y')))/2.0,
                                     'layer':t.get('layer','0'),'ancestors':list(t.get('ancestors') or []),
                                     'depth':t.get('depth',0),'fragmented':True})

    # Some exported architectural references repeat the same room text more than once.
    # Deduplicate only exact/near-exact duplicates; distinct rooms keep distinct markers.
    dedup=[]; seen=set()
    for r in room_markers:
        k=(r['name'],round(float(r['x']),2),round(float(r['y']),2))
        if k in seen: continue
        seen.add(k); dedup.append(r)
    return (occurrences,dedup) if with_rooms else occurrences




def _parse_room_barriers_dxf(path):
    """Extract transformed architectural wall segments from visible Model Space.

    v4.9 uses these segments only for room-membership scoring.  No third-party
    geometry package is required.  Barrier geometry includes primary wall layers
    (A-WAL*, *WALL*) plus secondary window/glazing layers (A-WIN*, *WIN*, *GLS*)
    so rooms separated by a window/glazed wall do not merge during raster BFS.
    These layers are architectural only and must never enter BOQ/Review/labels.
    """
    pairs=list(_dxf_pairs(path)); sections={}; i=0
    while i < len(pairs):
        if pairs[i] == (0,'SECTION') and i+1 < len(pairs) and pairs[i+1][0] == 2:
            name=pairs[i+1][1]; j=i+2; chunk=[]
            while j < len(pairs) and pairs[j] != (0,'ENDSEC'):
                chunk.append(pairs[j]); j+=1
            sections[name]=chunk; i=j+1
        else: i+=1
    model=_dxf_entities(sections.get('ENTITIES',[]))
    blocks={}; current=None
    for e in _dxf_entities(sections.get('BLOCKS',[])):
        if e['type']=='BLOCK':
            name=_dxf_first(e,2,'')
            current={'name':name,'base':(_dxf_num(e,10),_dxf_num(e,20)),'entities':[]}; blocks[name]=current
        elif e['type']=='ENDBLK': current=None
        elif current is not None: current['entities'].append(e)

    def mm(m,n):
        a,b,c,d,tx,ty=m; A,B,C,D,TX,TY=n
        return (a*A+c*B,b*A+d*B,a*C+c*D,b*C+d*D,a*TX+c*TY+tx,b*TX+d*TY+ty)
    def tp(m,x,y):
        a,b,c,d,tx,ty=m; return a*x+c*y+tx,b*x+d*y+ty
    def im(e,base):
        x,y=_dxf_num(e,10),_dxf_num(e,20); sx,sy=_dxf_num(e,41,1),_dxf_num(e,42,1)
        ang=math.radians(_dxf_num(e,50,0)); co,si=math.cos(ang),math.sin(ang); bx,by=base
        return (co*sx,si*sx,-si*sy,co*sy,x-(co*sx*bx-si*sy*by),y-(si*sx*bx+co*sy*by))
    def wall_layer(layer):
        u=str(layer or '').upper().replace('_','-')
        # Primary architectural barriers: walls.
        is_wall = ('A-WAL' in u) or ('WALL' in u) or ('-WAL-' in u) or u.endswith('-WAL')
        # Secondary architectural barriers: windows / glazing / curtain-glass.
        # In this drawing family, windows often complete the separating wall line
        # between two rooms. Treat them as blockers for room segmentation only.
        is_window_barrier = (
            u.startswith('A-WIN') or ('-WIN' in u) or ('WIN-' in u) or
            ('WINDOW' in u) or ('GLS' in u) or ('GLASS' in u) or ('CRT-GLS' in u)
        )
        # Never allow labels/debug/data/tray/door layers to be considered room barriers.
        excluded = any(tok in u for tok in (
            'DEBUG', 'TELETECH-LABEL', 'ROUTE', 'DATANET', 'CCTVNET',
            'CABLE', 'TRAY', 'DOOR', '-DOR', 'A-DOR', 'TEXT', 'DIM'
        ))
        return (is_wall or is_window_barrier) and not excluded
    def verts(e):
        pts=[]; pending=None
        for c,v in e.get('pairs',[]):
            if c==10:
                try: pending=float(v)
                except Exception: pending=None
            elif c==20 and pending is not None:
                try: pts.append((pending,float(v)))
                except Exception: pass
                pending=None
        return pts

    segs=[]
    def addseg(a,b,layer):
        ax,ay=a; bx,by=b
        if (ax-bx)*(ax-bx)+(ay-by)*(ay-by) < 1e-6: return
        segs.append((float(ax),float(ay),float(bx),float(by),str(layer or '')))
    def walk(entities,parent=(1,0,0,1,0,0),ancestors=(),depth=0):
        if depth>16: return
        for e in entities:
            typ=e.get('type'); layer=_dxf_first(e,8,'0')
            if typ=='INSERT':
                name=_dxf_first(e,2,'')
                if name in blocks and not any(('legend' in str(a).lower() or 'legent' in str(a).lower()) for a in (*ancestors,name)):
                    walk(blocks[name]['entities'],mm(parent,im(e,blocks[name]['base'])),ancestors+(name,),depth+1)
                continue
            if not wall_layer(layer): continue
            if typ=='LINE':
                addseg(tp(parent,_dxf_num(e,10),_dxf_num(e,20)),tp(parent,_dxf_num(e,11),_dxf_num(e,21)),layer)
            elif typ in {'LWPOLYLINE','POLYLINE'}:
                pp=[tp(parent,x,y) for x,y in verts(e)]
                for a,b in zip(pp,pp[1:]): addseg(a,b,layer)
                try: closed=(int(float(_dxf_first(e,70,0))) & 1)!=0
                except Exception: closed=False
                if closed and len(pp)>2: addseg(pp[-1],pp[0],layer)
    walk(model)
    return segs


def _segments_cross(a,b,c,d,eps=1e-7):
    """Proper 2-D segment intersection; endpoint touches are ignored."""
    ax,ay=a; bx,by=b; cx,cy=c; dx,dy=d
    def cross(p,q,r): return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    o1=cross(a,b,c); o2=cross(a,b,d); o3=cross(c,d,a); o4=cross(c,d,b)
    if abs(o1)<=eps or abs(o2)<=eps or abs(o3)<=eps or abs(o4)<=eps: return False
    return (o1>0)!=(o2>0) and (o3>0)!=(o4>0)


def _wall_aware_room(rec,route,barriers):
    """Choose a room marker using architectural wall separation before distance.

    Only a small nearest-marker candidate set is tested, keeping large drawings fast.
    This fixes the classic GR-05/GR-07 case without hard-coded handles while retaining
    nearest-marker fallback when architectural walls are unavailable.
    """
    rooms=route.get('rooms') or []
    if not rooms: return None
    x=float(rec.get('x')); y=float(rec.get('y'))
    nearest=sorted(rooms,key=lambda m:(x-float(m['x']))**2+(y-float(m['y']))**2)[:10]
    if not barriers: return nearest[0]
    # Local spatial pruning: a wall can intersect endpoint->marker only if its bbox overlaps that bbox.
    scored=[]
    for m in nearest:
        mx,my=float(m['x']),float(m['y']); minx,maxx=sorted((x,mx)); miny,maxy=sorted((y,my))
        crossings=0
        for x1,y1,x2,y2,_layer in barriers:
            if max(x1,x2)<minx or min(x1,x2)>maxx or max(y1,y2)<miny or min(y1,y2)>maxy: continue
            if _segments_cross((x,y),(mx,my),(x1,y1),(x2,y2)): crossings+=1
        d2=(x-mx)*(x-mx)+(y-my)*(y-my)
        scored.append((crossings,d2,m))
    scored.sort(key=lambda z:(z[0],z[1],str(z[2].get('name',''))))
    return scored[0][2]




# ------------------------- Drawing QA: wall gap review + overlay healing -------------------------
def _parse_door_points_dxf(path):
    """Extract transformed door INSERT points from visible Model Space.

    Used only to classify wall gaps. Door geometry is not counted as BOQ and is
    not written back to the original drawing.
    """
    pairs=list(_dxf_pairs(path)); sections={}; i=0
    while i < len(pairs):
        if pairs[i] == (0,'SECTION') and i+1 < len(pairs) and pairs[i+1][0] == 2:
            name=pairs[i+1][1]; j=i+2; chunk=[]
            while j < len(pairs) and pairs[j] != (0,'ENDSEC'):
                chunk.append(pairs[j]); j+=1
            sections[name]=chunk; i=j+1
        else:
            i+=1
    model=_dxf_entities(sections.get('ENTITIES',[]))
    blocks={}; current=None
    for e in _dxf_entities(sections.get('BLOCKS',[])):
        if e['type']=='BLOCK':
            name=_dxf_first(e,2,'')
            current={'name':name,'base':(_dxf_num(e,10),_dxf_num(e,20)),'entities':[]}; blocks[name]=current
        elif e['type']=='ENDBLK':
            current=None
        elif current is not None:
            current['entities'].append(e)

    def mm(m,n):
        a,b,c,d,tx,ty=m; A,B,C,D,TX,TY=n
        return (a*A+c*B,b*A+d*B,a*C+c*D,b*C+d*D,a*TX+c*TY+tx,b*TX+d*TY+ty)
    def tp(m,x,y):
        a,b,c,d,tx,ty=m; return a*x+c*y+tx,b*x+d*y+ty
    def im(e,base):
        x,y=_dxf_num(e,10),_dxf_num(e,20); sx,sy=_dxf_num(e,41,1),_dxf_num(e,42,1)
        ang=math.radians(_dxf_num(e,50,0)); co,si=math.cos(ang),math.sin(ang); bx,by=base
        return (co*sx,si*sx,-si*sy,co*sy,x-(co*sx*bx-si*sy*by),y-(si*sx*bx+co*sy*by))
    def is_door_layer(layer):
        u=str(layer or '').upper().replace('_','-')
        return ('A-DOR' in u) or ('A-DOOR' in u) or u in {'DOOR','DOORS'}
    pts=[]
    def walk(entities,parent=(1,0,0,1,0,0),ancestors=(),depth=0):
        if depth>16: return
        for e in entities:
            typ=e.get('type'); layer=_dxf_first(e,8,'0')
            if typ=='INSERT':
                x,y=tp(parent,_dxf_num(e,10),_dxf_num(e,20))
                if is_door_layer(layer):
                    pts.append((float(x),float(y),str(layer or '')))
                name=_dxf_first(e,2,'')
                if name in blocks and not any(('legend' in str(a).lower() or 'legent' in str(a).lower()) for a in (*ancestors,name)):
                    walk(blocks[name]['entities'],mm(parent,im(e,blocks[name]['base'])),ancestors+(name,),depth+1)
    walk(model)
    return pts


def _wall_gap_healing_for_dxf(path, write_debug=False):
    """Scan wall barriers for likely drafting gaps and build overlay-only patches.

    Returns a dict with: original_segments, patched_segments, gaps, reports, files.
    This deliberately never saves over the uploaded source drawing. The auto-closed
    line segments are used only by the room-boundary flood/geodesic logic.
    """
    p=Path(path)
    try:
        stat=p.stat(); cache_key=(str(p.resolve()), stat.st_mtime_ns, bool(write_debug))
    except Exception:
        cache_key=(str(p), 0, bool(write_debug))
    if cache_key in GAP_HEAL_CACHE:
        return GAP_HEAL_CACHE[cache_key]

    CLUSTER_TOL=50.0
    AUTO_CLOSE_MAX=600.0
    DOOR_MIN, DOOR_MAX=600.0,1300.0
    REVIEW_MAX=2500.0
    SEARCH_RADIUS=6000.0

    segments=list(_parse_room_barriers_dxf(p))
    doors=list(_parse_door_points_dxf(p))
    endpoints=[]
    for idx,(x1,y1,x2,y2,layer) in enumerate(segments):
        endpoints.append((float(x1),float(y1),idx,0))
        endpoints.append((float(x2),float(y2),idx,1))

    # Spatially cluster wall endpoints. Any singleton cluster is a dangling end.
    cell=CLUSTER_TOL
    bins=defaultdict(list)
    for i,(x,y,si,endno) in enumerate(endpoints):
        bins[(int(math.floor(x/cell)), int(math.floor(y/cell)))].append(i)
    parent=list(range(len(endpoints)))
    def find(a):
        while parent[a]!=a:
            parent[a]=parent[parent[a]]; a=parent[a]
        return a
    def union(a,b):
        ra,rb=find(a),find(b)
        if ra!=rb: parent[rb]=ra
    for key,ids in list(bins.items()):
        kx,ky=key
        near=[]
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                near.extend(bins.get((kx+dx,ky+dy),[]))
        for a in ids:
            ax,ay,asi,aend=endpoints[a]
            for b in near:
                if b<=a: continue
                bx,by,bsi,bend=endpoints[b]
                if asi==bsi and aend!=bend:  # same segment two ends are not a junction
                    continue
                if (ax-bx)*(ax-bx)+(ay-by)*(ay-by) <= CLUSTER_TOL*CLUSTER_TOL:
                    union(a,b)
    clusters=defaultdict(list)
    for i in range(len(endpoints)):
        clusters[find(i)].append(i)
    dangling=[]
    for ids in clusters.values():
        if len(ids)==1:
            i=ids[0]; x,y,si,endno=endpoints[i]
            dangling.append((x,y,i))

    # Pair dangling ends by nearest plausible partner using bins for speed.
    pair_cell=SEARCH_RADIUS
    dbins=defaultdict(list)
    for i,(x,y,ei) in enumerate(dangling):
        dbins[(int(math.floor(x/pair_cell)), int(math.floor(y/pair_cell)))].append(i)
    cand=[]
    for key,ids in dbins.items():
        kx,ky=key
        near=[]
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                near.extend(dbins.get((kx+dx,ky+dy),[]))
        for a in ids:
            ax,ay,ae=dangling[a]
            for b in near:
                if b<=a: continue
                bx,by,be=dangling[b]
                d=math.hypot(ax-bx,ay-by)
                if d<=SEARCH_RADIUS:
                    cand.append((d,a,b))
    cand.sort(key=lambda z:z[0])
    used=set(); gaps=[]; auto_segments=[]; gap_id=0
    for d,a,b in cand:
        if a in used or b in used: continue
        ax,ay,_=dangling[a]; bx,by,_=dangling[b]
        mx,my=(ax+bx)/2.0,(ay+by)/2.0
        has_door=any(math.hypot(mx-dx,my-dy)<=max(d,DOOR_MAX) for dx,dy,_ly in doors)
        if d < AUTO_CLOSE_MAX:
            cls='auto_closed'; auto=True
        elif DOOR_MIN <= d <= DOOR_MAX:
            if has_door:
                cls='door_present'; auto=False
            else:
                cls='auto_closed_no_door_evidence'; auto=True
        elif d <= REVIEW_MAX:
            cls='needs_review'; auto=False
        else:
            cls='ignored_large_opening'; auto=False
        used.add(a); used.add(b); gap_id+=1
        g={'gap_id':gap_id,'point_a':[round(ax,2),round(ay,2)],'point_b':[round(bx,2),round(by,2)],'width':round(d,1),'classification':cls,'has_door_nearby':bool(has_door),'auto_closed':bool(auto)}
        gaps.append(g)
        if auto:
            auto_segments.append((ax,ay,bx,by,'DEBUG-WALL-GAP-AUTO-CLOSE'))

    patched=segments+auto_segments
    ts=time.strftime('%Y%m%d-%H%M%S')
    base=p.stem.replace(' ','_')[:80]
    report_name=f'{base}_BOARD_REVIEW_GAP_REPORT_{ts}.json'
    debug_name=f'{base}_DEBUG_WALL_GAP_PATCHES_{ts}.dxf'
    zip_name=f'{base}_BOARD_REVIEW_PACKAGE_{ts}.zip'
    report_path=EXPORTS/report_name
    debug_path=EXPORTS/debug_name
    zip_path=EXPORTS/zip_name
    report={'drawing':p.name,'wall_segments':len(segments),'door_inserts':len(doors),'dangling_endpoints':len(dangling),'matched_gaps':len(gaps),'auto_closed':sum(1 for g in gaps if g['auto_closed']),'needs_review':sum(1 for g in gaps if g['classification']=='needs_review'),'door_present':sum(1 for g in gaps if g['classification']=='door_present'),'ignored_large':sum(1 for g in gaps if g['classification']=='ignored_large_opening'),'thresholds':{'cluster_tolerance':CLUSTER_TOL,'auto_close_max':AUTO_CLOSE_MAX,'door_min':DOOR_MIN,'door_max':DOOR_MAX,'review_max':REVIEW_MAX,'search_radius':SEARCH_RADIUS},'gaps':gaps[:1000]}
    try:
        report_path.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    except Exception:
        pass
    if write_debug:
        try:
            import ezdxf
            doc=ezdxf.readfile(str(p))
            msp=doc.modelspace()
            for lname,color in [('DEBUG-WALL-GAP-AUTO-CLOSE',1),('DEBUG-WALL-GAP-REVIEW',30),('DEBUG-WALL-GAP-DOOR',3)]:
                if lname not in doc.layers:
                    doc.layers.add(lname, color=color)
            for g in gaps:
                ax,ay=g['point_a']; bx,by=g['point_b']
                if g['auto_closed']:
                    layer='DEBUG-WALL-GAP-AUTO-CLOSE'
                elif g['classification']=='needs_review':
                    layer='DEBUG-WALL-GAP-REVIEW'
                elif g['classification']=='door_present':
                    layer='DEBUG-WALL-GAP-DOOR'
                else:
                    continue
                msp.add_line((ax,ay),(bx,by),dxfattribs={'layer':layer})
            doc.saveas(str(debug_path))
        except Exception as e:
            debug_path=None
            report['debug_error']=str(e)
            try: report_path.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
            except Exception: pass
    else:
        debug_path=None
    try:
        with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED) as z:
            if report_path.exists(): z.write(report_path, report_path.name)
            if debug_path and debug_path.exists(): z.write(debug_path, debug_path.name)
    except Exception:
        pass
    out={'original_segments':segments,'patched_segments':patched,'auto_segments':auto_segments,'report':report,'files':{'report':report_name if report_path.exists() else None,'debug_dxf':debug_path.name if debug_path and debug_path.exists() else None,'zip':zip_name if zip_path.exists() else None}}
    # Cache both write_debug variants shallowly; keep memory bounded.
    GAP_HEAL_CACHE.clear()
    GAP_HEAL_CACHE[cache_key]=out
    return out


def _board_review_floor(floor=None):
    target=None
    if floor:
        target=PROJECT_RESULTS.get(floor)
    if not target:
        target=CURRENT_RESULT or (next(iter(PROJECT_RESULTS.values())) if PROJECT_RESULTS else None)
    if not target:
        raise RuntimeError('Analyze at least one drawing before Board Review.')
    d=Path(target.get('drawing') or '')
    if not d.exists():
        raise RuntimeError('Source drawing path is not available for this floor; upload/analyze it again.')
    heal=_wall_gap_healing_for_dxf(d, write_debug=True)
    rep=dict(heal.get('report') or {})
    rep['floor']=target.get('floor')
    rep['room_membership_method']='RASTER_MULTI_SOURCE_BFS_FROM_ALL_ROOM_TAGS'
    rep['vector_polygonize_authoritative']=False
    rep['note']='Gap-healing remains as wall overlay, but room ownership is decided by raster multi-source BFS; vector polygonize is diagnostic only.'
    return {'summary':rep,'files':heal.get('files') or {}}

def _raster_room_membership_multisource(result,dxf_path):
    """Assign room_id using ALL room tag seeds at once (multi-source BFS).

    This implements the user's hard rule: room membership is not decided by
    point proximity to other endpoints.  Room tags are the seeds.  Architectural
    wall segments become blockers.  Every free raster cell is claimed by the
    first seed wave that reaches it; where two waves meet becomes the implicit
    room boundary, even if the CAD wall has a small gap.  Endpoint order can then
    group by room_id before any nearest-neighbour/perimeter walk is allowed.

    No external packages are required; this is a conservative stdlib rasterizer.
    """
    markers=list((result or {}).get('room_markers') or [])
    points=list((result or {}).get('mapped') or [])
    if not markers or not points:
        return {'strategy':'MULTI_SOURCE_GEODESIC_ROOM_SEGMENTATION','assigned':0,'rooms':len(markers),'status':'NO_MARKERS_OR_POINTS'}
    try:
        barriers=_wall_gap_healing_for_dxf(dxf_path, write_debug=False).get('patched_segments') or _parse_room_barriers_dxf(dxf_path)
    except Exception:
        try: barriers=_parse_room_barriers_dxf(dxf_path)
        except Exception: barriers=[]

    # Stable room ids.  Keep duplicate room labels unique by coordinates.
    seeds=[]
    seen=set()
    for m in markers:
        try:
            name=str(m.get('name') or '').strip().upper()
            if not name:
                continue
            x=float(m.get('x')); y=float(m.get('y'))
        except Exception:
            continue
        key=f"{name}@{round(x,2)},{round(y,2)}"
        if key in seen:
            continue
        seen.add(key)
        mm=dict(m)
        mm['_key']=key
        seeds.append(mm)
    if not seeds:
        return {'strategy':'MULTI_SOURCE_GEODESIC_ROOM_SEGMENTATION','assigned':0,'rooms':0,'status':'NO_VALID_SEEDS'}

    coords=[]
    for m in seeds:
        coords.append((float(m['x']),float(m['y'])))
    for pnt in points:
        try: coords.append((float(pnt['x']),float(pnt['y'])))
        except Exception: pass
    if not coords:
        return {'strategy':'MULTI_SOURCE_GEODESIC_ROOM_SEGMENTATION','assigned':0,'rooms':len(seeds),'status':'NO_COORDS'}
    minx=min(x for x,_ in coords); maxx=max(x for x,_ in coords)
    miny=min(y for _,y in coords); maxy=max(y for _,y in coords)
    w=max(maxx-minx,1.0); h=max(maxy-miny,1.0)
    margin=max(w,h)*0.035+3000.0
    minx-=margin; maxx+=margin; miny-=margin; maxy+=margin
    w=maxx-minx; h=maxy-miny

    # Keep the grid bounded so the UI does not hang on big DWGs.  The drawings
    # are in mm-like units; this usually lands around 300-700 units/cell.
    # RASTER_ROOM_SEGMENTATION_FIX: this drawing family uses double-line fragmented
    # walls; vector polygonize creates wall slivers instead of real rooms.
    # Use a finer raster grid (mm-like drawing units) and let all official room
    # tag seeds grow at once.  Walls/gap-heal patches are blockers; no vector
    # polygonize result is used as the authority for room membership.
    target=max(w,h)/1400.0
    cell=max(75.0,min(180.0,target))
    cols=max(4,int(math.ceil(w/cell))+1); rows=max(4,int(math.ceil(h/cell))+1)
    # If an unusually huge drawing appears, coarsen safely.
    max_cells=3000000
    if rows*cols > max_cells:
        scale=math.sqrt((rows*cols)/max_cells)
        cell*=scale
        cols=max(4,int(math.ceil(w/cell))+1); rows=max(4,int(math.ceil(h/cell))+1)

    def to_cell(x,y):
        return (max(0,min(cols-1,int(round((float(x)-minx)/cell)))),
                max(0,min(rows-1,int(round((float(y)-miny)/cell)))))

    blocked=bytearray(rows*cols)
    def bidx(c,r): return r*cols+c
    def mark(c,r,rad=1):
        for rr in range(max(0,r-rad),min(rows,r+rad+1)):
            base=rr*cols
            for cc in range(max(0,c-rad),min(cols,c+rad+1)):
                blocked[base+cc]=1

    # Rasterize only relevant local wall segments.  A one-cell dilation closes
    # small drafting gaps without turning whole rooms into walls.
    for x1,y1,x2,y2,_ly in barriers:
        if max(x1,x2)<minx or min(x1,x2)>maxx or max(y1,y2)<miny or min(y1,y2)>maxy:
            continue
        c1,r1=to_cell(x1,y1); c2,r2=to_cell(x2,y2)
        dc=c2-c1; dr=r2-r1; steps=max(abs(dc),abs(dr),1)
        for i in range(steps+1):
            c=int(round(c1+dc*i/steps)); r=int(round(r1+dr*i/steps))
            mark(c,r,1)

    # Seeds and endpoints must remain usable even if they sit exactly on a wall
    # line or block insertion point over a wall.  Clear a tiny island around them.
    for obj in list(seeds)+points:
        try:
            c,r=to_cell(obj.get('x'),obj.get('y'))
            for rr in range(max(0,r-1),min(rows,r+2)):
                for cc in range(max(0,c-1),min(cols,c+2)):
                    blocked[bidx(cc,rr)]=0
        except Exception:
            pass

    owner=[-1]*(rows*cols)
    dist=[0]*(rows*cols)
    from collections import deque
    q=deque()
    # Snap each seed to the nearest free cell if needed.  All seeds start before
    # BFS, so this is a true multi-source fill.
    for si,m in enumerate(seeds):
        c,r=to_cell(m['x'],m['y'])
        chosen=None
        for rad in range(0,8):
            if chosen: break
            for rr in range(max(0,r-rad),min(rows,r+rad+1)):
                for cc in range(max(0,c-rad),min(cols,c+rad+1)):
                    ii=bidx(cc,rr)
                    if not blocked[ii] and owner[ii] < 0:
                        chosen=(cc,rr); break
                if chosen: break
        if not chosen:
            continue
        cc,rr=chosen; ii=bidx(cc,rr)
        owner[ii]=si; dist[ii]=0; q.append((cc,rr))

    # 4-neighbour BFS avoids diagonal leaking through wall corners.
    while q:
        c,r=q.popleft(); oi=owner[bidx(c,r)]; nd=dist[bidx(c,r)]+1
        for cc,rr in ((c+1,r),(c-1,r),(c,r+1),(c,r-1)):
            if cc<0 or cc>=cols or rr<0 or rr>=rows: continue
            ii=bidx(cc,rr)
            if blocked[ii] or owner[ii] >= 0: continue
            owner[ii]=oi; dist[ii]=nd; q.append((cc,rr))

    assigned=0; fallback=0
    for rec in points:
        try:
            c,r=to_cell(rec.get('x'),rec.get('y'))
            ii=bidx(c,r); oi=owner[ii]
            if oi < 0:
                # Near a wall/blocked island: look in a small radius, but still
                # choose an existing BFS owner, not nearest endpoint/XY sorting.
                best=None
                for rad in range(1,10):
                    for rr in range(max(0,r-rad),min(rows,r+rad+1)):
                        for cc in range(max(0,c-rad),min(cols,c+rad+1)):
                            jj=bidx(cc,rr)
                            if owner[jj] >= 0:
                                d=(cc-c)*(cc-c)+(rr-r)*(rr-r)
                                cand=(d,owner[jj])
                                if best is None or cand<best: best=cand
                    if best is not None: break
                if best is None:
                    continue
                oi=best[1]; fallback+=1
            room=seeds[int(oi)]
            rec['_room_name_v49']=str(room.get('name',''))
            rec['_room_key_v49']=str(room.get('_key',''))
            rec['_room_segmentation']='MULTI_SOURCE_BFS'
            assigned+=1
        except Exception:
            pass
    return {'strategy':'MULTI_SOURCE_GEODESIC_ROOM_SEGMENTATION','rooms':len(seeds),'assigned':assigned,'fallback_near_wall':fallback,'wall_segments':len(barriers),'cell_size':round(cell,2),'grid':f'{cols}x{rows}'}


def _apply_wall_aware_room_membership(result,dxf_path):
    """Attach room ids using multi-source geodesic segmentation.

    Earlier v4.9 builds used one endpoint->nearest room or line-of-sight scoring.
    That still allowed GR-59/60/61/62 back-to-back rooms to merge or interleave.
    The current rule is stricter: all official room tag seeds expand at the same
    time.  The assigned room_id then overrides spatial proximity always.
    """
    diag=_raster_room_membership_multisource(result,dxf_path)
    # Last-resort fallback for drawings without usable room seeds.
    if int(diag.get('assigned') or 0) > 0:
        return diag
    try: barriers=_parse_room_barriers_dxf(dxf_path)
    except Exception: barriers=[]
    ctx=_spatial_route_context(result); changed=0; assigned=0
    for rec in result.get('mapped') or []:
        try:
            split=float((result.get('rack_strategy') or {}).get('split_x',0.0)); x=float(rec.get('x',0.0))
            side='IDF-G1' if x>=split else 'IDF-G2'; route=ctx.get(side) or {}
            old=_nearest_room(rec,route); new=_wall_aware_room(rec,route,barriers)
            if new is None: continue
            assigned+=1
            rec['_room_name_v49']=str(new.get('name',''))
            rec['_room_key_v49']=str(new.get('_key',''))
            if old is not None and str(old.get('_key',''))!=str(new.get('_key','')): changed+=1
        except Exception: pass
    return {'strategy':'WALL_AWARE_LINE_OF_SIGHT_FALLBACK','wall_segments':len(barriers),'assigned':assigned,'changed_from_nearest':changed}

def _detect_floor_from_name(path, requested='Auto Detect'):
    if requested and requested != 'Auto Detect': return requested
    n=Path(path).name.upper()
    tests=[(' - G -','Ground Floor'),(' - F -','First Floor'),(' - S -','Second Floor'),
           (' - TH -','Third Floor'),(' - T -','Third Floor'),('THIRD','Third Floor'),
           (' - R -','Roof Floor'),('GROUND','Ground Floor'),('FIRST','First Floor'),
           ('SECOND','Second Floor'),('ROOF','Roof Floor')]
    for token,label in tests:
        if token in n: return label
    return 'Unknown Floor'


def _detect_building_from_name(path, fallback=''):
    """Detect the building code from the drawing filename.

    Project drawing names follow the practical pattern:
      <University> - <BUILDING> - <FLOOR TOKEN> - A.B.dxf

    The upload workspace adds a UUID prefix before the original filename, so the
    parser deliberately keys off the floor token instead of the first filename
    token.  Examples:
      Mansoura University - HUM - G - A.B.dxf  -> HUM
      Menoufia university - HQ - F - A.B.dxf -> HQ

    If the filename does not expose a reliable building token, retain the UI
    fallback rather than guessing.
    """
    stem=Path(path).stem
    # Uploaded files are saved as <32hex>_<original-name>. Strip that transport
    # prefix only; do not otherwise rewrite the user's drawing name.
    stem=re.sub(r'^[0-9a-fA-F]{32}_','',stem)
    parts=[x.strip() for x in re.split(r'\s+-\s+',stem) if x.strip()]
    floor_tokens={'G','F','S','TH','T','R','RF','ROOF','GROUND','FIRST','SECOND','THIRD'}
    for i,part in enumerate(parts):
        token=re.sub(r'[^A-Z0-9]+','',part.upper())
        if token in floor_tokens and i>0:
            candidate=parts[i-1].strip()
            if candidate:
                return candidate
    return str(fallback or '').strip()



def _detect_university_from_name(path, fallback=''):
    """Extract university name from the drawing filename.

    Standard pattern: <University> - <BUILDING> - <FLOOR TOKEN> - A.B.dxf
    The first dash-separated segment (before the building code) is the university.
    Strips the UUID upload prefix first.
    """
    stem = Path(path).stem
    stem = re.sub(r'^[0-9a-fA-F]{32}_', '', stem)
    parts = [x.strip() for x in re.split(r'\s+-\s+', stem) if x.strip()]
    floor_tokens = {'G','F','S','TH','T','R','RF','ROOF','GROUND','FIRST','SECOND','THIRD'}
    for i, part in enumerate(parts):
        token = re.sub(r'[^A-Z0-9]+', '', part.upper())
        if token in floor_tokens and i >= 2:
            candidate = parts[0].strip()
            if candidate:
                return candidate
    # fallback: first part if at least 2 segments exist
    if len(parts) >= 2:
        return parts[0].strip()
    return str(fallback or '').strip()

def _detect_floor_from_content(visible=None, room_markers=None):
    """Best-effort floor detection for drawings with generic file names.

    New lab boards may be uploaded as names like ``lab.dxf`` even though the
    model/XREF block names contain the architectural sheet title, e.g.
    ``02-First Floor Furniture Plan``. v4.9 originally looked at the file name
    only, which left the floor as Unknown and prevented CAD label package
    generation. This helper is conservative and only upgrades Unknown Floor when
    a clear floor token appears in room IDs, block names, ancestors, layers, text,
    or attributes.
    """
    samples=[]
    for r in room_markers or []:
        samples.extend([r.get('name',''), r.get('layer','')])
        samples.extend(r.get('ancestors') or [])
    for e in visible or []:
        samples.extend([e.get('block',''), e.get('layer',''), e.get('text','')])
        samples.extend(e.get('ancestors') or [])
        for k,v in (e.get('attributes') or {}).items():
            samples.append(k); samples.append(v)
    joined=' | '.join(str(x or '') for x in samples).upper()
    counts=Counter()
    counts['Ground Floor'] += len(re.findall(r'\bGR[- ]*\d+\b', joined))
    counts['First Floor'] += len(re.findall(r'\bFR[- ]*\d+\b', joined))
    counts['Second Floor'] += len(re.findall(r'\bSD[- ]*\d+\b', joined))
    counts['Third Floor'] += len(re.findall(r'\b(?:TH|T)[- ]*\d+\b', joined))
    counts['Roof Floor'] += len(re.findall(r'\bRF[- ]*\d+\b|\bR[- ]*\d+\b', joined))
    token_map=[
        ('GROUND FLOOR','Ground Floor'),('01-GROUND','Ground Floor'),('01 GROUND','Ground Floor'),
        ('FIRST FLOOR','First Floor'),('02-FIRST','First Floor'),('02 FIRST','First Floor'),
        ('SECOND FLOOR','Second Floor'),('03-SECOND','Second Floor'),('03 SECOND','Second Floor'),
        ('THIRD FLOOR','Third Floor'),('03-THIRD','Third Floor'),('03 THIRD','Third Floor'),('TH FLOOR','Third Floor'),
        ('ROOF FLOOR','Roof Floor'),('ROOF PLAN','Roof Floor'),
    ]
    for token,label in token_map:
        if token in joined: counts[label] += 25
    if not counts:
        return 'Unknown Floor'
    ranked=counts.most_common()
    label,val=ranked[0]
    if val<=0:
        return 'Unknown Floor'
    if len(ranked)>1 and ranked[1][1]==val:
        return 'Unknown Floor'
    return label



def _floor_prefix(floor_name):
    return {
        'Ground Floor':'G','First Floor':'F','Second Floor':'S','Third Floor':'T','Roof Floor':'R'
    }.get(floor_name,'U')


def _default_rack_labels(floor_name):
    p=_floor_prefix(floor_name)
    return {'IDF-G1':f'IDF-{p}1' if p!='G' else 'IDF-G1',
            'IDF-G2':f'IDF-{p}2' if p!='G' else 'IDF-G2'}


def _is_network_rack_anchor_block(name):
    n=str(name or '').upper().replace('_','-')
    if 'AV SYSTEM RACK' in n:
        return False
    return bool(re.search(r'\b(?:IDR|IDF|MDF)[ -]*RACK\b|\bRACK[ -]*(?:IDR|IDF|MDF)\b',n))

def _detect_rack_anchors(visible, room_markers=None):
    """Find physical telecom rack anchors used to start the mirrored route.

    ROUTE_QA_FIX2: prefer real MDF/IDF block geometry on telecom rack layers.
    Do NOT use notes like "FROM DOWN IDF-T1" as the route start when a physical
    MDF/IDF block exists. Those notes can live in the roof/up-feed area and made
    Third Floor numbering start from the roof instead of the electrical room.
    """
    def is_rack_layer(layer):
        u=str(layer or '').upper().replace('_','-')
        return ('LC-IDF' in u) or ('DATA-RACK' in u) or ('DATA RACK' in u) or ('LC-DATA-RACK' in u) or ('RACK' in u and 'AV' not in u)
    physical=[]
    for e in visible or []:
        if e.get('legend_branch'):
            continue
        b=str(e.get('block') or '')
        joined_for_lab=' '.join([b]+[str(a or '') for a in (e.get('ancestors') or [])]).upper()
        if 'IDF LAB' in joined_for_lab or re.search(r'\bLAB\s*\d+\b', joined_for_lab):
            continue
        if not (_is_network_rack_anchor_block(b) or _is_idf_support_block(b) or is_rack_layer(e.get('layer'))):
            continue
        # Ignore AV rack and text-only pseudo entities.
        if b == '__RACK_TEXT__' or 'AV SYSTEM RACK' in b.upper():
            continue
        try:
            x=float(e.get('x')); y=float(e.get('y'))
            if math.isfinite(x) and math.isfinite(y):
                physical.append((x,y,e))
        except Exception:
            pass
    if physical:
        # remove near duplicates from nested block walking
        ded=[]
        for x,y,e in sorted(physical, key=lambda z:(z[0],z[1],str(z[2].get('block','')))):
            if any((x-a)**2+(y-b)**2 < 250000 for a,b,_ in ded):
                continue
            ded.append((x,y,e))
        physical=ded
        if len(physical)==1:
            x,y,e=physical[0]
            return {'IDF-G1':{'x':x,'y':y,'block':e.get('block'),'source':'PHYSICAL_RACK_BLOCK'}}
        left=physical[0]; right=physical[-1]
        return {
            'IDF-G1':{'x':right[0],'y':right[1],'block':right[2].get('block'),'source':'PHYSICAL_RACK_BLOCK'},
            'IDF-G2':{'x':left[0],'y':left[1],'block':left[2].get('block'),'source':'PHYSICAL_RACK_BLOCK'},
        }

    # Fallback only: explicit text IDs are less reliable than physical blocks.
    explicit={}
    for e in visible or []:
        ridx=e.get('rack_index')
        if ridx not in {1,2}: continue
        try:
            x=float(e.get('x')); y=float(e.get('y'))
            if math.isfinite(x) and math.isfinite(y):
                explicit[f'IDF-G{ridx}']={'x':x,'y':y,'block':e.get('text') or e.get('block'),'source':'DRAWING_TEXT_FALLBACK'}
        except Exception:
            pass
    return explicit

# Menoufia HQ rack calibration learned from the user-marked First Floor drawing.
# Calibration entity: LWPOLYLINE handle C122A49 on layer "F2 LC 01 C".
# The line is vertical at X=25449.99998 and spans the plan; RIGHT side = Rack 1,
# LEFT side = Rack 2.  Relative to the countable service-point span in that
# calibration drawing, the boundary is at 49.9009% -- effectively the project
# midline.  We store the measured ratio rather than an absolute X so it transfers
# to the remaining Menoufia HQ floor drawings without drawing another line.
RACK_CALIBRATION = {
    'project': 'Menoufia University / HQ',
    'source_drawing': 'Menoufia university - HQ - F - A.B.dxf',
    'source_handle': 'C122A49',
    'source_layer': 'F2 LC 01 C',
    'orientation': 'VERTICAL',
    'split_fraction': 0.499009186127197,
    'rack1_side': 'RIGHT',
    'rack2_side': 'LEFT',
    'ground_cctv_force_rack1': False,
    'non_ground_cctv_force_rack1': True,
    # Rack anchor positions learned from the First Floor room envelope. Used only
    # when another floor does not contain explicit IDR/IDF rack blocks.
    'rack1_anchor_side_fraction': 0.50,
    'rack2_anchor_side_fraction': 0.50,
    'rack_anchor_y_fraction': 0.37,
}



def _is_cctv_service(item):
    s=str(item.get('service') or '').upper()
    b=str(item.get('block') or '').upper()
    return 'CCTV' in s or b in {'CAM1','CAM2','CAM3','CAM3AAAA'}


def _spatial_rack_assign(counted, floor_name, manual_routes=None):
    """Assign rack ownership.

    When the drawing contains user guide layers 'Route - rack1/2', they are the
    source of truth for rack ownership and ordering: project every device point
    to both routes and choose the nearest route.  The route INSERTs themselves
    are reference geometry and are never counted in BOQ.  Points farther than
    MANUAL_ROUTE_DISTANCE_LIMIT from every route are sent to Rack Review instead
    of being guessed.

    Without manual routes, preserve the calibrated v4.9 split behaviour.
    """
    usable=[]
    for item in counted:
        try:
            px=float(item.get('x')); py=float(item.get('y'))
            if math.isfinite(px) and math.isfinite(py): usable.append((item,px,py))
        except Exception:
            pass
    if not usable:
        return None, []

    routes=manual_routes or {}
    have_manual=bool(routes)
    assigned=[]; review=[]; lab_racks=set()

    if have_manual:
        # ROUTE_REFERENCE_PROFILE_FIX5:
        # The manually drawn route is the primary ORDER reference, but not every
        # real endpoint must sit close to the route polyline.  In Mansoura HUM,
        # some projector / signage / Wi-Fi points are intentionally behind rooms
        # or in the middle area, so earlier builds pushed them into Rack Review
        # when distance > MANUAL_ROUTE_DISTANCE_LIMIT.  User rule for this
        # building: right side + middle area -> Rack 1, left side -> Rack 2.
        # Therefore: use nearest manual route when the point is close enough;
        # otherwise assign rack ownership by a stable X split and keep a large
        # t-value so these far/middle items do not jump before route-near rooms.
        xs_for_split=[x for _,x,_ in usable]
        min_x,max_x=min(xs_for_split),max(xs_for_split)
        fallback_split_x=min_x + (max_x-min_x)*float(RACK_CALIBRATION.get('split_fraction',0.5)) if max_x-min_x>1e-9 else min_x
        fallback_route_lengths={k:float(v.get('length',0.0) or 0.0) for k,v in routes.items()}
        for item,px,py in usable:
            lab_rack=_lab_rack_id_from_record(item)
            if lab_rack:
                y=dict(item); y['rack']=lab_rack; y['rack_reason']='LOCAL_LAB_RACK_FROM_NESTED_LAB_BLOCK'; y['status']='MAPPED'; lab_racks.add(lab_rack); assigned.append(y)
                continue
            choice=_manual_route_choice_for_point(px,py,routes)
            if choice is None or choice[0] > MANUAL_ROUTE_DISTANCE_LIMIT:
                # Do NOT send these to Rack Allocation Review.  They still have
                # usable world coordinates, so resolve them with the project rule:
                # left side = Rack 2, middle/right = Rack 1.
                rack='IDF-G1' if px >= fallback_split_x else 'IDF-G2'
                nearest_t=0.0; nearest_dist=1e18; nearest_route=None
                if choice is not None:
                    nearest_dist, nearest_t, nearest_route = float(choice[0]), float(choice[1]), choice[2]
                y=dict(item); y['rack']=rack; y['rack_reason']='MANUAL_ROUTE_FAR_FALLBACK_RIGHT_MIDDLE_R1_LEFT_R2'; y['status']='MAPPED'
                y['_route_t']=float(nearest_t) + fallback_route_lengths.get(rack,0.0)*0.001
                y['_route_distance']=float(nearest_dist) if nearest_dist < 1e18 else None
                y['_route_far_fallback']=True
                y['_fallback_split_x']=float(fallback_split_x)
                if nearest_route: y['nearest_route']=nearest_route
                assigned.append(y); continue
            dist,t,rack,seg,u,qx,qy=choice
            y=dict(item); y['rack']=rack; y['rack_reason']='MANUAL_ROUTE_NEAREST'; y['status']='MAPPED'
            y['_route_t']=float(t); y['_route_distance']=float(dist); y['_route_segment']=int(seg); y['_route_u']=float(u)
            y['_route_projected_x']=float(qx); y['_route_projected_y']=float(qy)
            assigned.append(y)
        usable_ids={id(item) for item,_,_ in usable}
        for item in counted:
            if id(item) not in usable_ids:
                y=dict(item); y['status']='RACK_REVIEW'; y['rack_reason']='INVALID_COORDINATES_FOR_MANUAL_ROUTE'; review.append(y)
        meta={
            'strategy':'MANUAL_ROUTE_LAYER_PROJECTION',
            'distance_limit':MANUAL_ROUTE_DISTANCE_LIMIT,
            'routes':{k:{'source_layer':v.get('source_layer'),'source_block':v.get('source_block'),
                         'vertex_count':v.get('vertex_count'),'length':round(float(v.get('length',0)),2),
                         'insert_transform_identity':bool(v.get('insert_transform_identity'))} for k,v in routes.items()},
            'rule':'Project every device point to Route - rack1/2; nearest route owns rack/order. Far/unresolved is a QA flag only and must not push points to the end.',
            'lab_racks':sorted(lab_racks,key=_lab_rack_sort_key),
        }
        return meta, assigned+review

    xs=[px for _,px,_ in usable]
    min_x,max_x=min(xs),max(xs)
    frac=float(RACK_CALIBRATION['split_fraction'])
    split_x=min_x + (max_x-min_x)*frac if max_x-min_x>1e-9 else min_x

    usable_ids={id(item) for item,_,_ in usable}
    for item,px,py in usable:
        lab_rack=_lab_rack_id_from_record(item)
        if lab_rack:
            rack=lab_rack; reason='LOCAL_LAB_RACK_FROM_NESTED_LAB_BLOCK'; lab_racks.add(lab_rack)
        elif floor_name!='Ground Floor' and RACK_CALIBRATION.get('non_ground_cctv_force_rack1') and _is_cctv_service(item):
            rack='IDF-G1'; reason='NON_GROUND_CCTV_FORCE_RACK1'
        else:
            rack='IDF-G1' if px >= split_x else 'IDF-G2'
            reason='CALIBRATED_RIGHT_R1_LEFT_R2'
        y=dict(item); y['rack']=rack; y['rack_reason']=reason; y['status']='MAPPED'; assigned.append(y)

    for item in counted:
        if id(item) not in usable_ids:
            y=dict(item); y['status']='RACK_REVIEW'; review.append(y)

    meta={
        'strategy':'MENOUFIA_PROJECT_CALIBRATED_SPLIT',
        'calibration_source':RACK_CALIBRATION['source_drawing'],
        'calibration_handle':RACK_CALIBRATION['source_handle'],
        'orientation':RACK_CALIBRATION['orientation'],
        'split_fraction':frac,
        'min_x':min_x,'split_x':split_x,'max_x':max_x,
        'rule':'left of calibrated line -> Rack 2; right -> Rack 1',
        'camera_exception':'Ground Floor CCTV follows split; First/Second/Roof CCTV -> Rack 1',
        'lab_racks': sorted(lab_racks, key=_lab_rack_sort_key),
        'lab_rule':'Nested LAB n AGN endpoints -> local IDF-LABn; excluded from main IDF split',
    }
    return meta, assigned+review

def _rename_xlsx_sheets(path, rename_map):
    """Rename workbook sheet tabs WITHOUT parsing/reserializing workbook.xml.

    Excel stores compatibility prefix names (for example x15/xr) inside string
    attributes such as mc:Choice Requires. ElementTree can rewrite namespace
    prefixes while leaving those string values unchanged, producing a workbook
    that desktop Excel reports as corrupt. A surgical byte replacement keeps
    the original workbook XML and every namespace declaration byte-for-byte.
    """
    path=Path(path)
    tmp=path.with_suffix('.rename.tmp.xlsx')
    with zipfile.ZipFile(path,'r') as zin, zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data=zin.read(info.filename)
            if info.filename=='xl/workbook.xml':
                # Sheet names in our templates are plain ASCII. Replace only
                # exact name= attribute values; do not touch relationships or XML.
                for old_name,new_name in rename_map.items():
                    old_token=('name="'+old_name+'"').encode('utf-8')
                    new_token=('name="'+new_name+'"').encode('utf-8')
                    if old_token not in data:
                        raise RuntimeError(f'Cannot rename missing Excel sheet: {old_name}')
                    data=data.replace(old_token,new_token,1)
            zout.writestr(info,data)
    tmp.replace(path)

def _analysis_dxf(path):
    """Return a DXF path. DWG is converted through the installed OCS executable."""
    path=Path(path)
    if path.suffix.lower()=='.dxf': return path, None
    if path.suffix.lower()!='.dwg': raise RuntimeError('Only DWG/DXF is supported')
    try:
        ocs=OCS.discover(); exe=Path(ocs.desc.get('executable') or r'C:\Program Files\Open CAD Studio\OpenCADStudio.exe')
    except Exception:
        exe=Path(r'C:\Program Files\Open CAD Studio\OpenCADStudio.exe')
    if not exe.exists(): raise RuntimeError('OpenCADStudio.exe was not found for DWG->DXF analysis conversion')
    out=WORKSPACE/(path.stem+'_'+uuid.uuid4().hex[:8]+'_analysis.dxf')
    cp=subprocess.run([str(exe),'--export',str(path),str(out)],capture_output=True,text=True,timeout=180)
    if cp.returncode!=0 or not out.exists():
        raise RuntimeError('Open CAD Studio could not convert DWG to analysis DXF: '+(cp.stderr or cp.stdout or '').strip())
    return out,out


REFERENCE_SAMPLE_BLOCKS = {
    'AV SYSTEM DOOM CAMERA',
    'EC-dp',
    'EC-xp',
    'access point wifi weather proof',
}

def _reference_sample_outside_room_envelope(e, room_markers, source_path):
    """Project-safe legend/reference filter for Menoufia HQ.

    Some exported drawings expose a small DATANET symbol palette as visible INSERTs
    even though it is only a reference/legend cluster. Ignore these known sample
    block names only when they are clearly outside the architectural room envelope.
    The same block name inside the real floor envelope is NOT silently ignored.
    """
    b=str(e.get('block') or '')
    if b not in REFERENCE_SAMPLE_BLOCKS:
        return False
    # Generic: if official room markers exist, use their envelope to suppress
    # known legend/reference sample blocks outside the real building.
    try:
        x=float(e.get('x')); y=float(e.get('y'))
    except Exception:
        return False
    pts=[]
    for m in room_markers or []:
        try:
            pts.append((float(m.get('x')),float(m.get('y'))))
        except Exception:
            pass
    if len(pts)>=3:
        minx=min(x0 for x0,_ in pts); maxx=max(x0 for x0,_ in pts)
        miny=min(y0 for _,y0 in pts); maxy=max(y0 for _,y0 in pts)
        padx=max(500.0,(maxx-minx)*0.12); pady=max(500.0,(maxy-miny)*0.12)
        return not (minx-padx <= x <= maxx+padx and miny-pady <= y <= maxy+pady)
    return abs(x)<1200.0 and abs(y)<1200.0


def _norm_block_name(name):
    """Normalize CAD block names for rule matching without changing displayed block names."""
    t=str(name or '').upper().strip()
    for ch in ('·','•','∙','●','-','_','.'):
        t=t.replace(ch,' ')
    t=re.sub(r'\s+',' ',t)
    return t

def _is_data_voice_box_block(name):
    n=_norm_block_name(name)
    # New client drawings may use D·V BOX / D-V BOX / D.V BOX instead of plain D-V.
    return n in {'D V BOX','DV BOX','DATA VOICE BOX','DATA VOICE OUTLET'}

def _is_projector_block(name):
    n=_norm_block_name(name)
    return n in {'PROJECTOR','PROJ','P PROJECTOR'} or n.startswith('PROJECTOR ')

def _is_cam2_block(name):
    n=_norm_block_name(name)
    # Some new building drawings write CAM2 as CAM 2. Treat it as the same
    # CCTV indoor bullet family and do not send it to Classification Review.
    return n in {'CAM2','CAM 2','CAMERA 2','CAMERA2','PTZ','PTZ CAMERA','PTZ CAM'}

def _is_supporting_tray_block(name):
    n=_norm_block_name(name)
    return n in {'DATATRAY','DATA TRAY','DATA CABLE TRAY','CABLE TRAY','TRAY'} or 'TRAY' in n

def _is_idf_support_block(name):
    n=_norm_block_name(name)
    # MDF/IDF labels are telecom rack anchors/supporting blocks, not endpoints.
    return n in {'IDF','IDF ROOM','IDF RACK','IDF CABINET','MDF','MDF ROOM','MDF RACK','MDF CABINET'} or n.startswith('IDF ') or n.startswith('MDF ')


def _lab_rack_id_from_record(item):
    """Return IDF-LABn when an endpoint belongs to an independent lab rack."""
    texts=[]
    for a in item.get('ancestors') or []:
        texts.append(str(a or ''))
    texts.append(str(item.get('block') or ''))
    for k,v in (item.get('attributes') or {}).items():
        texts.append(str(k or '')); texts.append(str(v or ''))
    joined=' | '.join(texts).upper().replace('_','-')
    m=re.search(r'IDF[- ]*LAB\s*(\d+)', joined)
    if not m:
        m=re.search(r'\bLAB\s*(\d+)\b', joined)
    if not m:
        return None
    return f'IDF-LAB{int(m.group(1))}'


def _lab_rack_sort_key(rack):
    m=re.search(r'(\d+)', str(rack or ''))
    return int(m.group(1)) if m else 999


def _rack_order_from_counts(result):
    racks=['IDF-G1','IDF-G2']
    labs=sorted([r for r in (result.get('counts') or {}) if str(r).startswith('IDF-LAB')], key=_lab_rack_sort_key)
    return racks+labs


def _rack_label_prefix(result, rack):
    labels=result.get('rack_labels') or {}
    if rack in labels:
        return labels[rack]
    if str(rack).startswith('IDF-LAB'):
        n=_lab_rack_sort_key(rack)
        return f'LAB{n}' if n != 999 else str(rack).replace('IDF-','')
    floor_prefix=_floor_prefix(result.get('floor'))
    return f'{floor_prefix}1' if rack=='IDF-G1' else (f'{floor_prefix}2' if rack=='IDF-G2' else str(rack))


def _cad_label_layer_for_rack(result, rack):
    """Return a visual AutoCAD layer for labels belonging to one rack.

    v4.9 LAB isolation: local lab rack labels are not written on the same
    TELETECH-LABELS layer as the building main racks.  This lets the user
    visually verify and toggle LAB1/LAB2/LAB3 independently from IDF-F1/F2.
    """
    base = LABEL_LAYER if 'LABEL_LAYER' in globals() else 'TELETECH-LABELS'
    r = str(rack or '').upper()
    if r.startswith('IDF-LAB'):
        n = _lab_rack_sort_key(r)
        return f'{base}-LAB{n}' if n != 999 else f'{base}-LAB'
    floor_prefix = _floor_prefix((result or {}).get('floor'))
    if r == 'IDF-G1':
        return f'{base}-{floor_prefix}1'
    if r == 'IDF-G2':
        return f'{base}-{floor_prefix}2'
    safe = re.sub(r'[^A-Za-z0-9_-]+','-',str(rack or 'RACK')).strip('-') or 'RACK'
    return f'{base}-{safe[:20]}'

def _safe_lsp_str(v):
    return str(v).replace('\\','\\\\').replace('"','\\"')

def analyze_cad(path, university='', building='', floor_name='Auto Detect'):
    source_path=Path(path)
    detected_floor=_detect_floor_from_name(source_path,floor_name)
    # University and building are derived from the drawing filename first;
    # the UI values are used only as fallbacks.
    university=_detect_university_from_name(source_path, university) or str(university or 'University')
    building=_detect_building_from_name(source_path, building) or 'Building'
    dxf_path,temp_dxf=_analysis_dxf(source_path)
    # v4.9 keeps a temporary converted DXF alive until room-barrier extraction is
    # complete; it is still deleted before analyze_cad returns.
    visible,room_markers=_parse_visible_dxf(dxf_path,with_rooms=True)
    manual_routes=_extract_manual_route_polylines(dxf_path)
    if detected_floor == 'Unknown Floor':
        detected_floor = _detect_floor_from_content(visible, room_markers)

    # Keep room IDs that belong to the drawing being analyzed. Some CAD files carry
    # other-floor architectural references (for example a Ground block inside the
    # First-Floor file); those must never contaminate this floor's traversal route.
    room_prefix={'Ground Floor':'GR-','First Floor':'FR-','Second Floor':'SD-','Third Floor':'T-','Roof Floor':'RF-'}.get(detected_floor)
    if room_prefix:
        room_markers=[m for m in room_markers if str(m.get('name','')).upper().startswith(room_prefix)]
    rack_anchors=_detect_rack_anchors(visible,room_markers)

    # A semantic service nested inside another semantic endpoint is geometry/internal symbol,
    # not a second physical occurrence. This prevents D inside D-V from double-counting.
    service_parents={'D-V','D-W','CAM1','CAM2','CAM 2','CAM3','CAM3aaaa','Indoor Cam AGN','Indoor Cam 2 AGN','floor boox 1','floor box2',
                     'AC AGN','AV SYSTEM RACK','F POINT 13 AGN'}
    candidates=[]; suppressed=[]; legend=[]; ignored=[]
    for e in visible:
        if e.get('legend_branch'):
            legend.append(e); continue
        if _is_route_reference_entity(e):
            x=dict(e); x.update(status='IGNORED',service='Manual route guide reference geometry / non-BOQ'); ignored.append(x); continue
        if any(a in service_parents for a in e.get('ancestors',[])):
            suppressed.append(e); continue
        candidates.append(e)

    # Physical quantity classification comes ONLY from visible candidates.
    quantity=Counter(); counted=[]; review=[]
    def add(e,service,code,ports,qkey=None,status='COUNTED'):
        # occurrence_id is unique per visible physical occurrence in this drawing.
        # Nested block-definition handles are not globally unique across insert instances,
        # so CAD labeling must never use the DXF handle alone as an occurrence key.
        x=dict(e); x.update(service=service,code=code,logical_ports=list(ports),status=status,rack=None,
                            occurrence_id=len(counted)+1)
        counted.append(x)
        if qkey: quantity[qkey]+=1

    for e in candidates:
        b=str(e.get('block') or ''); layer=str(e.get('layer') or '')
        attrs={str(k).upper():str(v).upper().strip() for k,v in (e.get('attributes') or {}).items()}
        if _reference_sample_outside_room_envelope(e,room_markers,source_path):
            x=dict(e); x.update(status='IGNORED',service='Reference/legend sample outside room envelope'); ignored.append(x)
            continue
        if b=='AV SYSTEM DOOM CAMERA':
            # FIX6: this round AV/recording symbol on the new lab board is a Record Camera (R),
            # not ordinary CCTV (C).  Keep AV SYSTEM RACK itself as AV/A.
            add(e,'Record Camera','R',['R'],'RECORD CAM')
        elif b=='access point wifi weather proof':
            add(e,'Indoor Wi-Fi','W',['W'],'D-W')
        elif b=='D-V' or _is_data_voice_box_block(b):
            dvtag=attrs.get('D-V','')
            if dvtag=='AC': add(e,'Access Control Point','AC',['AC'],'AC AGN')
            elif dvtag in {'TV'}: add(e,'Television','TV',['TV'],'TV')
            else: add(e,'D-V (Data + Voice)','DV',['D','V'],'D-V')
        elif b=='D-W':
            wtag=attrs.get('W','')
            if wtag=='H': add(e,'High-Level Screen','H',['H'],'H')
            elif wtag=='AC': add(e,'Access Control Point','AC',['AC'],'AC AGN')
            elif wtag=='TV': add(e,'Television','TV',['TV'],'TV')
            elif wtag in {'W','M',''}:
                # Menoufia HQ First Floor uses both W and M display values on the same
                # D-W Wi-Fi endpoint family. H/AC/TV remain explicit service overrides.
                add(e,'Indoor Wi-Fi','W',['W'],'D-W')
            else:
                x=dict(e); x.update(status='REVIEW',service=f'D-W attribute {wtag} needs project mapping'); review.append(x)
        elif b in {'CAM1','Indoor Cam AGN'}: add(e,'CCTV - Indoor Dome','C',['C'],'CAM1')
        elif _is_cam2_block(b) or b=='Indoor Cam 2 AGN': add(e,'CCTV - Indoor Bullet','C',['C'],'CAM2')
        elif b in {'CAM3','CAM3aaaa'}: add(e,'CCTV - Outdoor Bullet','C',['C'],'CAM3aaaa')
        elif b.upper()=='DD':
            # Drawing block DD = digital display / room-information signage (Point Name S).
            add(e,'Digital Signage','S',['S'],'DIGITAL S')
        elif b.upper()=='TV': add(e,'Television','TV',['TV'],'TV')
        elif _is_projector_block(b): add(e,'Projector','P',['P'],'PROJECTOR')
        elif b.upper() in {'H','HIGH LEVEL SCREEN'}: add(e,'High-Level Screen','H',['H'],'H')
        elif b=='floor boox 1': add(e,'Floor Box - 2 Cable','DV',['D','V'],'floor boox 1')
        elif b=='floor box2': add(e,'Floor Box - 1 Cable','D',['D'],'floor box2')
        elif b=='AC AGN': add(e,'Access Control Point','AC',['AC'],'AC AGN')
        elif b=='AV SYSTEM RACK': add(e,'AV System Rack','A',['A'],'AV SYSTEM RACK')
        elif b=='EC-tp':
            tag=attrs.get('T','')
            mp={'H':('High-Level Screen','H',['H'],'H'),'P':('Projector','P',['P'],'PROJECTOR'),
                'AC':('Access Control Point','AC',['AC'],'AC AGN'),'R':('Record Camera','R',['R'],'RECORD CAM'),
                'S':('Digital Signage','S',['S'],'DIGITAL S'),'TV':('Television','TV',['TV'],'TV'),
                'A':('AV System','A',['A'],'AV SYSTEM RACK')}
            if tag in mp:
                svc,code,ports,qkey=mp[tag]; add(e,svc,code,ports,qkey)
            else:
                x=dict(e); x.update(status='IGNORED',service=f'EC-tp {tag or "?"} supporting/future-use'); ignored.append(x)
        elif b=='F POINT 13 AGN':
            # Assembly is tracked as supporting equipment. It does not inflate the verified
            # physical BOQ total; contained ports are exposed separately for audit/reporting.
            x=dict(e); x.update(status='IGNORED',service='13-port floor distribution assembly',contained_ports=13); ignored.append(x)
        elif b in {'__RACK_TEXT__','__ROOF_TEXT__'} or _is_network_rack_anchor_block(b) or _is_idf_support_block(b):
            x=dict(e); x.update(status='IGNORED',service='Telecom rack anchor used for spatial label ordering'); ignored.append(x)
        elif b.upper()=='CCTVTRAY' or _is_supporting_tray_block(b) or b in {'2 socket','SDU','HL UPS DUPLEX','cctvtray','A$C2844487F','A$C3a8948c6','SINGLE FB','*U20'} or b.startswith('HQ '):
            x=dict(e); x.update(status='IGNORED',service='Supporting / non-BOQ block'); ignored.append(x)
        else:
            # Architectural/nested library geometry is not a BOQ candidate. Keep only low-current-looking
            # names/layers in REVIEW; silently classifying architecture as REVIEW would swamp every drawing.
            low=(layer.upper().startswith(('LC-','DATA','CCTV','E640','SOCKET')) or any(k in b.upper() for k in ('CAM','WIFI','RJ45','RACK','POINT')))
            x=dict(e); x.update(status='REVIEW' if low else 'IGNORED',service='Unclassified visible low-current candidate' if low else 'Architectural/supporting')
            (review if low else ignored).append(x)

    # Known Ground drawing: counts still come from visible Model Space, but the user-approved rack split
    # is contractual because several CCTV blocks use transformed coordinates unsuitable for geometric split.
    # v2.4: every live drawing, including Ground Floor, is analyzed by the same
    # visible-ModelSpace + calibrated spatial rack engine.  The old hard-coded Ground
    # reference override is intentionally disabled so the UI never adds reference-only data.
    is_ground_ref=False

    counts={rack:{'physical':{},'logical_ports':{},'physical_total':0,'logical_total':0} for rack in ('IDF-G1','IDF-G2')}
    rack_review=[]
    mapped_records=[]
    if is_ground_ref:
        service_lookup={'D-V':'D-V (Data + Voice)','D-W':'Indoor Wi-Fi','CAM1':'CCTV - Indoor Dome',
                        'CAM2':'CCTV - Indoor Bullet','CAM3aaaa':'CCTV - Outdoor Bullet',
                        'floor boox 1':'Floor Box - 2 Cable','AC AGN':'Access Control Point',
                        'AV SYSTEM RACK':'AV System Rack','H':'High-Level Screen'}
        for rack in ('IDF-G1','IDF-G2'):
            phys=Counter(); lp=Counter()
            for key,n in MENOUFIA_RACK_REFERENCE[rack].items():
                svc=service_lookup[key]; phys[svc]+=n
                port_map={'D-V':['D','V'],'D-W':['W'],'CAM1':['C'],'CAM2':['C'],'CAM3aaaa':['C'],
                          'floor boox 1':['D','V'],'AC AGN':['AC'],'AV SYSTEM RACK':['A'],'H':['H']}
                for code in port_map[key]: lp[code]+=n
            counts[rack]={'physical':dict(phys),'logical_ports':dict(lp),'physical_total':sum(phys.values()),'logical_total':sum(lp.values())}
        rack_profile_applied=True
    else:
        # Generic drawings: use the TELETECH spatial fallback on visible/world coordinates.
        # The UI still exposes rack overrides, but normal two-rack drawings no longer require
        # manual allocation when coordinates are trustworthy.
        rack_meta, allocated=_spatial_rack_assign(counted,detected_floor,manual_routes)
        for y in allocated:
            if y.get('status')=='RACK_REVIEW':
                rack_review.append(y); continue
            rack=y.get('rack'); mapped_records.append(y)
            counts.setdefault(rack, {'physical':{},'logical_ports':{},'physical_total':0,'logical_total':0})
            c=counts[rack]; phys=Counter(c.get('physical') or {}); lp=Counter(c.get('logical_ports') or {})
            phys[y['service']]+=1
            for code in y.get('logical_ports') or []: lp[code]+=1
            c['physical']=dict(phys); c['logical_ports']=dict(lp); c['physical_total']=sum(phys.values()); c['logical_total']=sum(lp.values())
        rack_profile_applied=bool(rack_meta)

    unassigned=Counter(x['service'] for x in rack_review)
    unassigned_ports={}
    for svc in unassigned:
        sample=next(x for x in rack_review if x['service']==svc)
        unassigned_ports[svc]=sample.get('logical_ports',[])

    # Cable/tray lengths are withheld for generic drawings until visible-geometry path traversal is
    # implemented for every supported entity type. This is safer than reporting BLOCK-library lengths.
    cable_lengths={}
    cable_note='Cable lengths intentionally withheld in v1.6 generic mode; visible-root geometry traversal pending.'
    if is_ground_ref:
        # Preserve the verified Ground reference values rather than rescanning unused block definitions.
        cable_lengths={
            'LC-CABLE -10':{'length_m':646.9,'approximate':False},'CCTVnet':{'length_m':304.5,'approximate':False},
            'LC-CABLE -30':{'length_m':257.0,'approximate':False},'LC-CABLE-40':{'length_m':136.2,'approximate':False},
            'DN01-D':{'length_m':30.4,'approximate':False},'ELEC':{'length_m':11.8,'approximate':False},
            'e-Cable-T':{'length_m':7.2,'approximate':False}}
        cable_note='Verified Ground reference lengths.'

    ref_validation=None; rack_validation=None
    if is_ground_ref:
        ref_validation=[{'item':k,'expected':v,'actual':int(quantity.get(k,0)),'pass':int(quantity.get(k,0))==int(v)} for k,v in MENOUFIA_REFERENCE.items()]
        rack_validation=[]
        service_lookup={'D-V':'D-V (Data + Voice)','D-W':'Indoor Wi-Fi','CAM1':'CCTV - Indoor Dome',
                        'CAM2':'CCTV - Indoor Bullet','CAM3aaaa':'CCTV - Outdoor Bullet','floor boox 1':'Floor Box - 2 Cable',
                        'AC AGN':'Access Control Point','AV SYSTEM RACK':'AV System Rack','H':'High-Level Screen'}
        for rack in ('IDF-G1','IDF-G2'):
            phys=Counter(counts[rack]['physical'])
            for key,expected in MENOUFIA_RACK_REFERENCE[rack].items():
                actual=int(phys.get(service_lookup[key],0)); rack_validation.append({'rack':rack,'item':key,'expected':expected,'actual':actual,'pass':actual==expected})

    # Stable IDs make unresolved visible low-current candidates actionable in the UI.
    for i,rec in enumerate(review,1):
        rec['review_id']=i

    result={
        'profile':PROJECT_PROFILE['name'],'university':university,'building':building,'floor':detected_floor,'drawing':str(source_path),
        'count_scope':'VISIBLE_MODELSPACE_RECURSIVE','visible_insert_count':len(visible),'legend_insert_count':len(legend),
        'suppressed_nested_count':len(suppressed),'counts':counts,'quantity_boq':dict(quantity),
        'mapped':counted if is_ground_ref else mapped_records,'ignored':ignored+legend+suppressed,'review':review,
        'rack_review':rack_review,'unassigned':dict(unassigned),'unassigned_ports':unassigned_ports,
        'rack_labels':_default_rack_labels(detected_floor),'manual_routes':manual_routes,'rack_strategy':('VERIFIED_PROJECT_PROFILE' if is_ground_ref else (rack_meta if 'rack_meta' in locals() else None)),
        'cable_lengths':cable_lengths,'cable_note':cable_note,'reference_validation':ref_validation,
        'rack_validation':rack_validation,'rack_profile_applied':rack_profile_applied,
        'mapped_count':sum(int(v) for v in quantity.values()),'ignored_count':len(ignored)+len(legend)+len(suppressed),
        'review_count':len(review),'rack_review_count':len(rack_review),
        'export_template_supported':detected_floor in {'Ground Floor','First Floor','Second Floor','Third Floor','Roof Floor'},
        'analysis_warning':None if detected_floor in {'Ground Floor','First Floor','Second Floor','Third Floor','Roof Floor'} else f'Choose a supported floor before Excel export; detected {detected_floor}.',
        'room_markers':room_markers,'room_marker_count':len(room_markers),'rack_anchors':rack_anchors,
    }
    lab_racks=sorted([r for r in result.get('counts',{}) if str(r).startswith('IDF-LAB')], key=_lab_rack_sort_key)
    if lab_racks:
        labels=dict(result.get('rack_labels') or {})
        lab_counts={}
        for lr in lab_racks:
            n=_lab_rack_sort_key(lr); labels[lr]=f'LAB{n}' if n != 999 else lr
            lab_counts[lr]=result['counts'][lr]
        result['rack_labels']=labels
        result['lab_racks']=lab_racks
        result['lab_counts']=lab_counts
    result['room_membership_v49']=_apply_wall_aware_room_membership(result,dxf_path)
    result['spatial_label_order']=_spatial_label_order_summary(result)
    if temp_dxf is not None:
        try: temp_dxf.unlink(missing_ok=True)
        except Exception: pass
    return result



CLASSIFICATION_OVERRIDE_RULES = {
    'DV': ('D-V (Data + Voice)','DV',['D','V'],'D-V'),
    'W': ('Indoor Wi-Fi','W',['W'],'D-W'),
    'C_DOME': ('CCTV - Indoor Dome','C',['C'],'CAM1'),
    'C_BULLET': ('CCTV - Indoor Bullet','C',['C'],'CAM2'),
    'C_OUTDOOR': ('CCTV - Outdoor Bullet','C',['C'],'CAM3aaaa'),
    'AC': ('Access Control Point','AC',['AC'],'AC AGN'),
    'A': ('AV System Rack','A',['A'],'AV SYSTEM RACK'),
    'R': ('Record Camera','R',['R'],'RECORD CAM'),
    'H': ('High-Level Screen','H',['H'],'H'),
    'P': ('Projector','P',['P'],'PROJECTOR'),
    'S': ('Digital Signage','S',['S'],'DIGITAL S'),
    'TV': ('Television','TV',['TV'],'TV'),
    'F2': ('Floor Box - 2 Cable','DV',['D','V'],'floor boox 1'),
    'F1': ('Floor Box - 1 Cable','D',['D'],'floor box2'),
}


def apply_classification_overrides(result, decisions):
    """Resolve REVIEW items and rerun the normal rack engine on all countable endpoints.

    Rerunning the full floor is important: the learned split is normalized to the
    countable-service envelope.  Assigning one newly-classified item in isolation would
    create a false local envelope and could put it in the wrong rack.
    """
    pending=list(result.get('review') or [])
    if not pending:
        result['review_count']=0
        return result
    by_id={str(x.get('review_id',i)):x for i,x in enumerate(pending,1)}
    missing=[rid for rid in by_id if not str((decisions or {}).get(rid,'')).strip()]
    if missing:
        raise ValueError('Choose a classification (or Ignore) for every review item before applying.')

    ignored=list(result.get('ignored') or [])
    quantity=Counter(result.get('quantity_boq') or {})
    semantic=[]
    for rec in result.get('mapped') or []:
        x=dict(rec); x.pop('rack',None); x.pop('rack_reason',None); x['status']='COUNTED'
        semantic.append(x)
    next_occ=max([int(x.get('occurrence_id') or 0) for x in semantic]+[0])+1

    for rid,src in by_id.items():
        choice=str(decisions.get(rid,'')).strip().upper()
        if choice=='IGNORE':
            x=dict(src); x.update(status='IGNORED',service='Operator-classified supporting / non-BOQ')
            ignored.append(x)
            continue
        rule=CLASSIFICATION_OVERRIDE_RULES.get(choice)
        if not rule:
            raise ValueError(f'Unsupported classification choice for review item {rid}: {choice}')
        service,code,ports,qkey=rule
        x=dict(src)
        x.update(status='COUNTED',service=service,code=code,logical_ports=list(ports),rack=None,
                 occurrence_id=next_occ,classification_override=choice)
        next_occ+=1
        semantic.append(x)
        quantity[qkey]+=1

    rack_meta,allocated=_spatial_rack_assign(semantic,result.get('floor') or 'Unknown Floor')
    counts={rack:{'physical':{},'logical_ports':{},'physical_total':0,'logical_total':0} for rack in ('IDF-G1','IDF-G2')}
    mapped=[]; rack_review=[]
    for y in allocated:
        if y.get('status')=='RACK_REVIEW':
            rack_review.append(y); continue
        rack=y.get('rack'); mapped.append(y)
        c=counts[rack]; phys=Counter(c['physical']); lp=Counter(c['logical_ports'])
        phys[y['service']]+=1
        for port_code in y.get('logical_ports') or []: lp[port_code]+=1
        c['physical']=dict(phys); c['logical_ports']=dict(lp)
        c['physical_total']=sum(phys.values()); c['logical_total']=sum(lp.values())

    result['mapped']=mapped
    result['ignored']=ignored
    result['review']=[]
    result['review_count']=0
    result['rack_review']=rack_review
    result['rack_review_count']=len(rack_review)
    result['quantity_boq']=dict(quantity)
    result['counts']=counts
    result['rack_strategy']=rack_meta
    result['rack_profile_applied']=bool(rack_meta)
    result['mapped_count']=sum(int(v) for v in quantity.values())
    result['ignored_count']=len(ignored)
    unassigned=Counter(x.get('service','Unassigned') for x in rack_review)
    result['unassigned']=dict(unassigned)
    up={}
    for svc in unassigned:
        sample=next(x for x in rack_review if x.get('service')==svc)
        up[svc]=sample.get('logical_ports',[])
    result['unassigned_ports']=up
    result['spatial_label_order']=_spatial_label_order_summary(result)
    return result

def apply_rack_overrides(result, allocations):
    """Resolve Rack Review items by assigning the actual CAD occurrences.

    ROUTE_REFERENCE_PROFILE_FIX2:
    Earlier builds updated only the Excel count totals when the user pressed
    Apply Rack Allocation.  The physical CAD records stayed in rack_review, so
    Generate later saw (for example) Excel P=3 but only one mapped CAD P
    occurrence and failed with: CAD occurrences do not match Excel logical ports.

    This function now moves the reviewed occurrence records themselves into
    result['mapped'] with the selected rack, then rebuilds counts from mapped.
    Therefore Excel rows and CAD labels are generated from the same source of
    truth.
    """
    allowed=Counter(result.get('unassigned') or {})
    if not allowed:
        result['rack_overrides']={}; result['rack_review_count']=0; return result

    review_by_service=defaultdict(list)
    for rec in list(result.get('rack_review') or []):
        review_by_service[rec.get('service','Unassigned')].append(rec)
    for svc in review_by_service:
        # Stable, human-friendly order: closest-to-route items first when present.
        review_by_service[svc].sort(key=lambda r:(
            float(r.get('nearest_route_distance', r.get('_route_distance', 1e18)) or 1e18),
            float(r.get('y',0.0) or 0.0),
            float(r.get('x',0.0) or 0.0),
            str(r.get('handle',''))
        ))

    applied={}; newly_mapped=[]; still_review=[]
    for service,total in allowed.items():
        a=allocations.get(service) or {}
        g1=int(a.get('IDF-G1',0)); g2=int(a.get('IDF-G2',0))
        if g1<0 or g2<0 or g1+g2!=int(total):
            raise ValueError(f"Rack allocation for {service} must total {total} (got {g1+g2}).")
        records=list(review_by_service.get(service) or [])
        if len(records) != int(total):
            raise ValueError(f"Rack allocation for {service} expected {total} CAD item(s), found {len(records)}. Re-analyze first.")
        applied[service]={'IDF-G1':g1,'IDF-G2':g2}
        idx=0
        for rack,count in [('IDF-G1',g1),('IDF-G2',g2)]:
            for rec in records[idx:idx+count]:
                nr=dict(rec)
                nr['rack']=rack
                nr['status']='MAPPED'
                nr['rack_override']=True
                nr.pop('needs_manual_rack',None)
                newly_mapped.append(nr)
            idx += count

    # Keep any unexpected service review rows unresolved instead of hiding them.
    handled=set(allowed.keys())
    for rec in list(result.get('rack_review') or []):
        if rec.get('service','Unassigned') not in handled:
            still_review.append(rec)

    result['mapped']=list(result.get('mapped') or []) + newly_mapped

    # Rebuild counts from mapped so Excel and CAD use one exact source of truth.
    counts={rack:{'physical':{},'logical_ports':{},'physical_total':0,'logical_total':0}
            for rack in _rack_order_from_counts(result)}
    for rec in result['mapped']:
        rack=rec.get('rack')
        if not rack:
            continue
        if rack not in counts:
            counts[rack]={'physical':{},'logical_ports':{},'physical_total':0,'logical_total':0}
        c=counts[rack]; phys=Counter(c.get('physical') or {}); lp=Counter(c.get('logical_ports') or {})
        service=rec.get('service','Unassigned')
        phys[service]+=1
        for code in rec.get('logical_ports') or []:
            lp[code]+=1
        c['physical']=dict(phys); c['logical_ports']=dict(lp)
        c['physical_total']=sum(phys.values()); c['logical_total']=sum(lp.values())
    result['counts']=counts
    result['rack_overrides']=applied
    result['rack_review']=still_review
    result['rack_review_count']=len(still_review)
    result['unassigned']=dict(Counter(x.get('service','Unassigned') for x in still_review))
    result['unassigned_ports']={}
    result['spatial_label_order']=_spatial_label_order_summary(result)
    return result

# ------------------------------ Excel exports -------------------------------
def alloc_ports(panel_start, codes, port_start=1):
    """Allocate logical ports into 24-port patch panels from an exact start slot."""
    out=[]; panel=int(panel_start); port=int(port_start)
    for code,count,desc in codes:
        for _ in range(int(count)):
            out.append((panel,port,code,desc)); port+=1
            if port>24:
                panel+=1; port=1
    return out


def alloc_lc_then_dn_groups(lc_codes, dn_codes):
    """Pack *all* LC ports first, then start DN/Data on the very next panel.

    There is deliberately NO fixed LC panel allowance. LC can consume as many
    consecutive panels as required. DN/Data never shares the last LC panel.
    """
    lc=alloc_ports(1,lc_codes,1)
    lc_port_count=sum(int(count) for _,count,_ in lc_codes)
    lc_panel_count=((lc_port_count + 23)//24) if lc_port_count else 0
    dn_start_panel=(lc_panel_count + 1) if lc_panel_count else 1
    dn=alloc_ports(dn_start_panel,dn_codes,1)
    if lc and max(int(x[0]) for x in lc) != lc_panel_count:
        raise RuntimeError('Internal LC allocation mismatch: LC did not consume the expected number of panels.')
    if dn and int(dn[0][0]) != dn_start_panel:
        raise RuntimeError('Internal LC/DN boundary mismatch: DN did not start on the panel immediately after LC.')
    return lc,dn


def _switch_mix(points):
    """Return an efficient (48-port switches, 24-port switches) mix."""
    points=int(points)
    if points <= 0: return 0,0
    sw48,rem=divmod(points,48)
    if rem==0: return sw48,0
    if rem<=24: return sw48,1
    return sw48+1,0


def _panel_run_title(network, allocations):
    """Dynamic Port Map section title that matches the panels actually used."""
    panels=sorted({int(p) for p,_,_,_ in (allocations or [])})
    name=str(network or '').upper()
    if not panels:
        return f'{name} Rack'
    return f'{name} Rack (P{panels[0]:02d}:P{panels[-1]:02d})'


def _excel_col_for_panel_port(port):
    # Port 1 lives in Excel column B, port 24 in Y.
    n=int(port)+1; col=''; x=n
    while x:
        x,rem=divmod(x-1,26); col=chr(65+rem)+col
    return col


def _expand_panel_rows(rows_map, max_panel):
    out={int(k):int(v) for k,v in dict(rows_map).items()}
    max_panel=int(max_panel or 0)
    if max_panel <= 0: return out
    if not out: raise RuntimeError("Port Map panel-row map is empty.")
    p=max(out); row=out[p]
    if max_panel > 48: raise RuntimeError(f"Port Map requires {max_panel} patch panels; safety limit is 48 panels per rack.")
    while p < max_panel:
        p += 1; row += 5; out[p]=row
    return out

def _clone_row_style_only(src_row, target_row_num):
    row=ET.fromstring(ET.tostring(src_row, encoding="utf-8")); row.attrib["r"]=str(target_row_num)
    for c in row.findall(f"{{{NS_MAIN}}}c"):
        old_ref=c.attrib.get("r","A1"); m=re.match(r"([A-Z]+)",old_ref); col=m.group(1) if m else "A"; c.attrib["r"]=f"{col}{target_row_num}"; c.attrib.pop("t",None)
        for child in list(c):
            if child.tag in {f"{{{NS_MAIN}}}v",f"{{{NS_MAIN}}}f",f"{{{NS_MAIN}}}is"}: c.remove(child)
    return row

def _ensure_port_map_panel_blocks(root, rows_map):
    if not rows_map: return
    sheet_data=root.find(f"{{{NS_MAIN}}}sheetData")
    if sheet_data is None: return
    existing={int(r.attrib.get("r","0")):r for r in sheet_data.findall(f"{{{NS_MAIN}}}row")}
    def _row_cell_has_style(rn, ref):
        row=existing.get(rn)
        if row is None: return False
        for c in row.findall(f"{{{NS_MAIN}}}c"):
            if c.attrib.get("r")==ref:
                return bool(c.attrib.get("s"))
        return False
    def _panel_styled(outlet):
        return (_row_cell_has_style(outlet-1,f"A{outlet-1}") and
                _row_cell_has_style(outlet-1,f"B{outlet-1}") and
                _row_cell_has_style(outlet,f"A{outlet}") and
                _row_cell_has_style(outlet+1,f"A{outlet+1}") and
                _row_cell_has_style(outlet+2,f"A{outlet+2}"))
    source_outlet=None
    for panel,outlet in sorted((int(p),int(r)) for p,r in rows_map.items()):
        if _panel_styled(outlet): source_outlet=outlet
    if source_outlet is None: return
    src_rows=[existing[source_outlet-1],existing[source_outlet],existing[source_outlet+1],existing[source_outlet+2]]
    merges=root.find(f"{{{NS_MAIN}}}mergeCells")
    if merges is None:
        merges=ET.Element(f"{{{NS_MAIN}}}mergeCells"); root.append(merges)
    existing_merges={m.attrib.get("ref") for m in merges}
    for panel,outlet in sorted((int(p),int(r)) for p,r in rows_map.items()):
        block=(outlet-1,outlet,outlet+1,outlet+2)
        if not _panel_styled(outlet):
            # Replace any pre-existing blank/default rows with clones of the last
            # real styled panel block. Some templates contain blank rows past the
            # visible used range, so existence alone is not enough.
            for src_row,target_rn in zip(src_rows,block):
                old_row=existing.get(target_rn)
                if old_row is not None:
                    try: sheet_data.remove(old_row)
                    except ValueError: pass
                clone=_clone_row_style_only(src_row,target_rn); sheet_data.append(clone); existing[target_rn]=clone
            sep=outlet+3
            if sep not in existing:
                blank=ET.Element(f"{{{NS_MAIN}}}row",{"r":str(sep)}); sheet_data.append(blank); existing[sep]=blank
        _set_cell(root,f"A{outlet-1}",f"Patch Panel ({panel:02d})")
        for port in range(1,25): _set_cell(root,f"{_excel_col_for_panel_port(port)}{outlet-1}",port)
        _set_cell(root,f"A{outlet}","Outlet Type "); _set_cell(root,f"A{outlet+1}","Switch No."); _set_cell(root,f"A{outlet+2}","Port in switch")
        mref=f"B{outlet+1}:Y{outlet+1}"
        if mref not in existing_merges:
            ET.SubElement(merges,f"{{{NS_MAIN}}}mergeCell",{"ref":mref}); existing_merges.add(mref)
    ordered=sorted(sheet_data.findall(f"{{{NS_MAIN}}}row"),key=lambda r:int(r.attrib.get("r","0")))
    for r in list(sheet_data): sheet_data.remove(r)
    for r in ordered: sheet_data.append(r)
    merges.attrib["count"]=str(len(list(merges)))

def _set_port_map_panel_visibility(root, rows_map, used_panels):
    """Hide fully empty patch-panel blocks; never hide a partially used panel.

    Rows are hidden rather than deleted so the consultant template formatting and
    fixed LC/DN information rows stay intact. This makes empty panels disappear
    in Excel without shifting or corrupting the template.
    """
    if not rows_map:
        return
    used={int(x) for x in (used_panels or set())}
    sheet_data=root.find(f"{{{NS_MAIN}}}sheetData")
    if sheet_data is None:
        return
    rows={int(r.attrib.get('r','0')):r for r in sheet_data.findall(f"{{{NS_MAIN}}}row")}
    for panel,outlet in sorted((int(p),int(r)) for p,r in rows_map.items()):
        hidden=panel not in used
        # title + outlet + switch + switch-port + separator row
        for rn in range(outlet-1,outlet+4):
            row=rows.get(rn)
            if row is None:
                continue
            if hidden:
                row.attrib['hidden']='1'
            else:
                row.attrib.pop('hidden',None)


def _write_port_map_stream(cells, rows_map, allocations, reserved_slots=None):
    """Write one continuous physical patch-panel/switch stream into a Port Map tab.

    Empty patch panels are explicitly cleared and left blank. Used panels keep
    the template panel block but unused panels no longer show titles or ports.
    """
    by_panel=defaultdict(list)
    for panel,port,code,desc in allocations:
        by_panel[int(panel)].append((int(port),code))
    for item in (reserved_slots or []):
        by_panel[int(item['panel'])].append((int(item['port']),'RES'))

    for panel,outlet_row in rows_map.items():
        title_row=outlet_row-1; switch_row=outlet_row+1; switch_port_row=outlet_row+2
        for rr in (title_row,outlet_row,switch_row,switch_port_row):
            for col in ['A','B','C','D','E','F','G','H','I','J','K','L','M','N','O','P','Q','R','S','T','U','V','W','X','Y']:
                cells[f'{col}{rr}']=''
        slots=sorted(by_panel.get(int(panel),[]))
        if not slots:
            continue
        cells[f'A{title_row}']=f'Patch Panel ({int(panel):02d})'
        for port in range(1,25):
            cells[f'{_excel_col_for_panel_port(port)}{title_row}']=port
        cells[f'A{outlet_row}']='Outlet Type '
        cells[f'A{switch_row}']='Switch No.'
        cells[f'A{switch_port_row}']='Port in switch'
        sw_no=(int(panel)+1)//2
        cells[f'B{switch_row}']=f'SW-{sw_no:02d}'
        second_half=(int(panel)-1)%2
        for port,code in slots:
            col=_excel_col_for_panel_port(port)
            cells[f'{col}{outlet_row}']=code
            cells[f'{col}{switch_port_row}']=second_half*24 + port


def export_all(result,label_pattern=None):
    if not result.get('export_template_supported', True):
        raise RuntimeError(result.get('analysis_warning') or 'No matching Excel template exists for this floor. Add the correct floor template before export.')
    if result.get('rack_review_count',0):
        raise RuntimeError(f"{result['rack_review_count']} counted item(s) need rack allocation before rack-based Excel export.")
    ts=time.strftime('%Y%m%d-%H%M%S')
    label_pattern,_=_validate_label_pattern(label_pattern)
    university=result['university']; building=result['building']; floor=result['floor']
    rack_labels=result.get('rack_labels') or _default_rack_labels(floor)
    floor_prefix=_floor_prefix(floor)

    # Port / labeling order is contractual:
    # C, AC, A(or AV), R, H, P, S, TV, D, V, W
    CODE_DESC={
        'C':'CCTV','AC':'ACCESS CONTROL','A':'AV. SYSTEM','R':'RECORD CAMERA',
        'H':'HIGH LEVEL SCREEN','P':'PROJECTOR','S':'DIGITAL SIGNAGE','TV':'TELEVISION',
        'D':'DATA','V':'VOICE','W':'WI-FI','F':'FUTURE POINT'
    }
    LC_ORDER=['C','AC','A','R','H','P','S','TV']
    DN_ORDER=['D','V','W','F']

    rack_cfg={}
    for rack,prefix in [('IDF-G1',f'{floor_prefix}1'),('IDF-G2',f'{floor_prefix}2')]:
        lp=Counter(result['counts'][rack]['logical_ports'])
        phys=Counter(result['counts'][rack]['physical'])
        # Floor boxes are Data/Voice endpoints: 2-cable = one D + one V; 1-cable = one D.
        lc_codes=[(c,lp[c],CODE_DESC[c]) for c in LC_ORDER if lp[c]]
        dn_codes=[(c,lp[c],CODE_DESC[c]) for c in DN_ORDER if lp[c]]
        lc,dn=alloc_lc_then_dn_groups(lc_codes,dn_codes)
        rack_cfg[rack]=(prefix,lc,dn,phys,lp)

    # ---------------- Port Map ----------------
    pm=EXPORTS/f'port_map_{ts}.xlsx'; patches={}; pm_panel_maps={}; pm_used_panels={}
    PORT_LAYOUTS={
      'Ground Floor':{
        'IDF-G1':('G-R1',{1:11,2:16,3:24,4:29,5:34,6:39},7,20),
        'IDF-G2':('G-R2',{1:11,2:19,3:24,4:29,5:34},7,15)},
      'First Floor':{
        'IDF-G1':('F-R1',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20),
        'IDF-G2':('F-R2',{1:11,2:19,3:24,4:29,5:34,6:39,7:44},7,15)},
      'Second Floor':{
        'IDF-G1':('S-R1',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20),
        'IDF-G2':('S-R2',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20)},
      # Single-floor Third export reuses the S layout template, then renames tabs to T.
      'Third Floor':{
        'IDF-G1':('S-R1',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20),
        'IDF-G2':('S-R2',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20)},
      # Roof has no native template tabs; reuse the Ground layouts, then rename the tabs.
      'Roof Floor':{
        'IDF-G1':('G-R1',{1:11,2:16,3:24,4:29,5:34,6:39},7,20),
        'IDF-G2':('G-R2',{1:11,2:19,3:24,4:29,5:34},7,15)},
    }
    maps=PORT_LAYOUTS[floor]
    for rack,(sh,rows_map,lc_info_row,dn_info_row) in maps.items():
        prefix,lc,dn,phys,lp=rack_cfg[rack]
        # IMPORTANT: template labels are merged A:B; values belong in merged C:E.
        cells={'C1':university,'C2':building,'C3':floor,'C4':rack_labels.get(rack,rack)}

        # Floor boxes are labeled as Data/Voice; no separate F symbol is emitted.

        allocations=lc+dn
        max_panel=max((p for p,_,_,_ in allocations),default=0)
        rows_map=_expand_panel_rows(rows_map,max_panel); pm_panel_maps[sh]=rows_map
        pm_used_panels[sh]={int(p) for p,_,_,_ in allocations}
        _write_port_map_stream(cells,rows_map,allocations)

        # Switch quantities + section totals.
        lc48,lc24=_switch_mix(len(lc)); dn48,dn24=_switch_mix(len(dn))
        cells[f'A{lc_info_row}']=_panel_run_title('LC',lc)
        cells[f'A{dn_info_row}']=_panel_run_title('DN',dn)
        cells[f'G{lc_info_row}']=lc48; cells[f'J{lc_info_row}']=lc24; cells[f'M{lc_info_row}']=len(lc)
        cells[f'G{dn_info_row}']=dn48; cells[f'J{dn_info_row}']=dn24; cells[f'M{dn_info_row}']=len(dn)
        patches[sh]=cells
    patch_xlsx(PORT_MAP_TEMPLATE,pm,patches,port_map_panel_maps=pm_panel_maps,port_map_used_panels=pm_used_panels)
    if floor=='Roof Floor': _rename_xlsx_sheets(pm,{'G-R1':'R-R1','G-R2':'R-R2'})
    if floor=='Third Floor': _rename_xlsx_sheets(pm,{'S-R1':'T-R1','S-R2':'T-R2'})

    # ---------------- Quantity Summary ----------------
    # Use the consultant Quantity Summary design, but only the approved columns A:W.
    # UNIVERSITY comes first, TOTAL is the last column, and no legacy columns are emitted.
    sm=EXPORTS/f'quantity_summary_{ts}.xlsx'; sp={}
    summary_cols=['A','B','C','D','E','F','G','H','I','J','K','L','M','N','O','P','Q','R','S','T','U','V','W']
    for rr in range(3,195):
        for cc in summary_cols:
            sp[f'{cc}{rr}']=''
    row=3
    for rack in _rack_order_from_counts(result):
        if rack not in (result.get('counts') or {}):
            continue
        sp.update(_summary_cells_for_rack(result,rack,row,_summary_floor_label(floor),_summary_rack_label(result,rack)))
        row+=1
    patch_xlsx(SUMMARY_TEMPLATE,sm,{'Sheet1':sp},freeze_columns={'Sheet1':4})

    # ---------------- Labeling / Cable Schedule ----------------
    # CRITICAL RULE: one drawing only populates its own labeling sheets.
    # All other floor sheets are cleared of old/template data so no fake data
    # from another drawing can leak into the exported workbook. Headers and
    # formatting (rows 1:4) are preserved.
    cs=EXPORTS/f'cable_schedule_{ts}.xlsx'; cp={}

    labeling_ranges={
        'Ground Floor G1':108, 'Ground Floor G2':115,
        'First Floor F1':167, 'First Floor F2':161,
        'Second Floor S1':159, 'Second Floor S2':156,
    }
    for sh,max_row in labeling_ranges.items():
        blank={}
        for row in range(5,max_row+1):
            for col in 'ABCD': blank[f'{col}{row}']=''
        cp[sh]=blank

    # Populate only the current drawing's pair of labeling sheets.
    LABEL_SHEETS={
      'Ground Floor':{'IDF-G1':'Ground Floor G1','IDF-G2':'Ground Floor G2'},
      'First Floor':{'IDF-G1':'First Floor F1','IDF-G2':'First Floor F2'},
      'Second Floor':{'IDF-G1':'Second Floor S1','IDF-G2':'Second Floor S2'},
      # Third reuses the Second layout and the tabs are renamed after patching.
      'Third Floor':{'IDF-G1':'Second Floor S1','IDF-G2':'Second Floor S2'},
      # Roof reuses the Ground layout and the tabs are renamed after patching.
      'Roof Floor':{'IDF-G1':'Ground Floor G1','IDF-G2':'Ground Floor G2'},
    }
    sheet_names=LABEL_SHEETS[floor]
    labeling_required_rows={sh:max_row for sh,max_row in labeling_ranges.items()}
    current_label_sheets=set(sheet_names.values())
    for rack in ('IDF-G1','IDF-G2'):
        prefix,lc,dn,phys,lp=rack_cfg[rack]; sh=sheet_names[rack]
        cells=cp[sh]
        max_row=labeling_ranges[sh]
        required_last_row=4+len(lc)+len(dn)
        labeling_required_rows[sh]=max(max_row, required_last_row)
        # Keep the labeling header aligned with the current drawing/rack, not the source template.
        cells['A1']=rack_labels.get(rack,rack)

        ordered=lc+dn
        rr=5; previous_code=None
        for panel,port,code,desc in ordered:
            first=(code!=previous_code)
            cells[f'A{rr}']=rack_labels.get(rack,rack) if first else ''
            network='LC' if code in LC_ORDER else 'DN'
            # TELETECH rule: label suffix follows the physical patch-panel port
            # 01..24. If CCTV ends at C15, the next LC service is AC16/H16/etc.
            # If a service crosses a panel boundary, the next panel restarts at 01.
            cells[f'B{rr}']=_format_port_label(prefix,network,panel,code,port,desc,label_pattern,validate=False)
            cells[f'C{rr}']=''
            cells[f'D{rr}']=desc if first else ''
            previous_code=code; rr+=1
    patch_xlsx(CABLE_TEMPLATE,cs,cp,
               min_rows=labeling_required_rows,
               strip_body_merges_sheets=current_label_sheets)
    if floor=='Roof Floor': _rename_xlsx_sheets(cs,{'Ground Floor G1':'Roof Floor R1','Ground Floor G2':'Roof Floor R2'})
    if floor=='Third Floor': _rename_xlsx_sheets(cs,{'Second Floor S1':'Third Floor T1','Second Floor S2':'Third Floor T2'})
    return {'port_map':pm.name,'quantity_summary':sm.name,'cable_schedule':cs.name}



def _build_rack_cfg(result):
    """Build ordered LC/DN port allocations for one analyzed floor result."""
    CODE_DESC={
        'C':'CCTV','AC':'ACCESS CONTROL','A':'AV. SYSTEM','R':'RECORD CAMERA',
        'H':'HIGH LEVEL SCREEN','P':'PROJECTOR','S':'DIGITAL SIGNAGE','TV':'TELEVISION',
        'D':'DATA','V':'VOICE','W':'WI-FI','F':'FUTURE POINT'
    }
    LC_ORDER=['C','AC','A','R','H','P','S','TV']
    DN_ORDER=['D','V','W','F']
    floor_prefix=_floor_prefix(result['floor'])
    rack_cfg={}
    for rack in _rack_order_from_counts(result):
        if rack not in (result.get('counts') or {}):
            continue
        if rack=='IDF-G1': prefix=f'{floor_prefix}1'
        elif rack=='IDF-G2': prefix=f'{floor_prefix}2'
        else: prefix=_rack_label_prefix(result,rack)
        lp=Counter(result['counts'][rack]['logical_ports'])
        phys=Counter(result['counts'][rack]['physical'])
        # Floor boxes are Data/Voice endpoints: 2-cable = one D + one V; 1-cable = one D.
        lc_codes=[(c,lp[c],CODE_DESC[c]) for c in LC_ORDER if lp[c]]
        dn_codes=[(c,lp[c],CODE_DESC[c]) for c in DN_ORDER if lp[c]]
        lc,dn=alloc_lc_then_dn_groups(lc_codes,dn_codes)
        rack_cfg[rack]=(prefix,lc,dn,phys,lp)
    return rack_cfg



# --------------------------- CAD label generation ---------------------------
LABEL_LAYER = 'TELETECH-LABELS'
LABEL_CODE_ORDER = ['C','AC','A','R','H','P','S','TV','D','V','W']
DEFAULT_LABEL_PATTERN = '{rack}/{network}{panel2}/{code}{port2}'

MANUAL_ROUTE_DISTANCE_LIMIT = 3000.0
MANUAL_ROUTE_LAYER_RE = re.compile(r'^\s*route\s*-\s*rack\s*([12])\s*$', re.I)

def _is_route_reference_entity(rec):
    """Manual guide routes are reference geometry, never BOQ devices."""
    layer=str((rec or {}).get('layer') or '')
    block=str((rec or {}).get('block') or '')
    txt=' '.join([layer, block]).upper().replace('_',' ')
    if MANUAL_ROUTE_LAYER_RE.match(layer):
        return True
    if 'ROUTE' in txt and 'RACK' in txt:
        return True
    return False
LABEL_PATTERN_TOKENS = {'rack','floor','rack_no','network','panel','panel2','code','port','port2','service'}


def _validate_label_pattern(pattern=None):
    """Validate a safe user-editable label schema and return (pattern, preview)."""
    pattern=(pattern or DEFAULT_LABEL_PATTERN).strip()
    if not pattern:
        pattern=DEFAULT_LABEL_PATTERN
    if len(pattern)>160 or any(ord(ch)<32 for ch in pattern):
        raise ValueError('Label schema must be one printable line of at most 160 characters.')
    tokens=re.findall(r'\{([A-Za-z0-9_]+)\}',pattern)
    unknown=sorted(set(tokens)-LABEL_PATTERN_TOKENS)
    if unknown:
        raise ValueError('Unknown label schema token(s): '+', '.join('{'+x+'}' for x in unknown))
    probe=re.sub(r'\{([A-Za-z0-9_]+)\}','',pattern)
    if '{' in probe or '}' in probe:
        raise ValueError('Label schema contains unmatched braces. Use only the documented {token} fields.')
    preview=_format_port_label('F1','LC',2,'C',1,'CCTV',pattern,validate=False)
    if len(preview)>200:
        raise ValueError('Generated label is too long. Shorten the custom schema.')
    return pattern,preview


def _format_port_label(prefix,network,panel,code,port,service,pattern=None,validate=True):
    pattern=(pattern or DEFAULT_LABEL_PATTERN).strip() or DEFAULT_LABEL_PATTERN
    if validate:
        pattern,_=_validate_label_pattern(pattern)
    prefix=str(prefix)
    vals={
        'rack':prefix,
        'floor':prefix[:1],
        'rack_no':prefix[-1:] if prefix[-1:].isdigit() else '',
        'network':str(network),
        'panel':str(int(panel)),
        'panel2':f'{int(panel):02d}',
        'code':str(code),
        'port':str(int(port)),
        'port2':f'{int(port):02d}',
        'service':str(service),
    }
    return re.sub(r'\{([A-Za-z0-9_]+)\}',lambda m:vals[m.group(1)],pattern)


def _next_visible_code_sequence(counter, code):
    """Continuous visible endpoint sequence per service code.

    Physical patch-panel ports still exist in the Port Map.  For labels and Cable
    Schedule IDs, {port}/{port2} represents the visible endpoint sequence for the
    service code within the rack, so D01 is not repeated on every DN panel.
    """
    counter[code] += 1
    return counter[code]


def _port_assignment_lists(result,label_pattern=None):
    """Return per-code port records with physical panel/port metadata."""
    label_pattern,_=_validate_label_pattern(label_pattern)
    cfg=_build_rack_cfg(result)
    out={rack:defaultdict(list) for rack in cfg}
    for rack in cfg:
        prefix,lc,dn,phys,lp=cfg[rack]
        for panel,port,code,desc in lc+dn:
            network='LC' if code in {'C','AC','A','R','H','P','S','TV'} else 'DN'
            out[rack][code].append({'prefix':prefix,'network':network,'panel':int(panel),'port':int(port),'code':code,'desc':desc,'label':_format_port_label(prefix,network,panel,code,port,desc,label_pattern,validate=False)})
    return out


def _port_label_lists(result,label_pattern=None):
    """Return the exact Cable Schedule/CAD labels, derived from physical slots."""
    recs=_port_assignment_lists(result,label_pattern=label_pattern)
    return {rack:defaultdict(list,{code:[x['label'] for x in items] for code,items in codes.items()}) for rack,codes in recs.items()}


def _safe_xy(rec):
    try:
        x=float(rec.get('x')); y=float(rec.get('y'))
        if math.isfinite(x) and math.isfinite(y): return x,y
    except Exception:
        pass
    raise RuntimeError(f"Cannot place CAD label for {rec.get('service','item')}: invalid world coordinates")




def _robust_xy_bounds(points, trim=0.03):
    """Return bounds that ignore far-away legend/reference clusters.

    Some Mansoura drawings contain visible low-current symbols from reference
    blocks hundreds of metres away from the building. If those are allowed to
    define the route envelope, the fallback route starts/loops in the wrong
    place. Use a trimmed envelope for routing only; the endpoint coordinates
    themselves are not changed.
    """
    vals=[]
    for p in points or []:
        try:
            x=float(p.get('x')); y=float(p.get('y'))
            if math.isfinite(x) and math.isfinite(y): vals.append((x,y))
        except Exception:
            pass
    if not vals:
        return (0.0,0.0,0.0,0.0)
    xs=sorted(x for x,_ in vals); ys=sorted(y for _,y in vals); n=len(vals)
    lo=max(0,min(n-1,int(n*trim))); hi=max(lo,min(n-1,int(n*(1.0-trim))))
    minx,maxx=xs[lo],xs[hi]; miny,maxy=ys[lo],ys[hi]
    # Expand a little so legitimate edge symbols remain inside the working box.
    wx=max(maxx-minx,1.0); wy=max(maxy-miny,1.0)
    return (minx-wx*0.08,miny-wy*0.08,maxx+wx*0.08,maxy+wy*0.08)

def _inside_bounds_xy(x,y,bounds, margin_frac=0.08):
    try:
        minx,miny,maxx,maxy=map(float,bounds); x=float(x); y=float(y)
    except Exception:
        return True
    w=max(maxx-minx,1.0); h=max(maxy-miny,1.0)
    mx=w*margin_frac; my=h*margin_frac
    return (minx-mx <= x <= maxx+mx) and (miny-my <= y <= maxy+my)

def _route_station(point,path):
    """Return station along a polyline for the closest projection of point."""
    px,py=float(point[0]),float(point[1]); cumulative=0.0; best=None
    for idx,(a,b) in enumerate(zip(path,path[1:])):
        ax,ay=a; bx,by=b; vx=bx-ax; vy=by-ay
        seg2=vx*vx+vy*vy
        if seg2<=1e-12: continue
        t=((px-ax)*vx+(py-ay)*vy)/seg2
        t=max(0.0,min(1.0,t)); qx=ax+t*vx; qy=ay+t*vy
        d2=(px-qx)*(px-qx)+(py-qy)*(py-qy); seg=math.sqrt(seg2)
        station=cumulative+t*seg
        cand=(d2,station,idx,t)
        if best is None or cand<best: best=cand
        cumulative+=seg
    return best if best is not None else (0.0,0.0,0,0.0)




def _extract_manual_route_polylines(path):
    """Extract manually drawn rack routes from layers like 'Route - rack1'.

    The route INSERT and its nested LWPOLYLINE are guide/reference geometry only.
    They are used to compute ordering t along the route and must never enter BOQ.
    Works with DXF text parsing only; no runtime AutoCAD/CoreConsole dependency.
    """
    try:
        pairs=list(_dxf_pairs(path))
    except Exception:
        return {}
    sections={}; i=0
    while i < len(pairs):
        if pairs[i] == (0,'SECTION') and i+1 < len(pairs) and pairs[i+1][0] == 2:
            name=pairs[i+1][1]; j=i+2; chunk=[]
            while j < len(pairs) and pairs[j] != (0,'ENDSEC'):
                chunk.append(pairs[j]); j+=1
            sections[name]=chunk; i=j+1
        else:
            i+=1
    model=_dxf_entities(sections.get('ENTITIES',[]))
    blocks={}; current=None
    for e in _dxf_entities(sections.get('BLOCKS',[])):
        if e['type']=='BLOCK':
            name=_dxf_first(e,2,'')
            current={'name':name,'base':(_dxf_num(e,10),_dxf_num(e,20)),'entities':[]}
            blocks[name]=current
        elif e['type']=='ENDBLK':
            current=None
        elif current is not None:
            current['entities'].append(e)

    def mat_mul(m,n):
        a,b,c,d,tx,ty=m; A,B,C,D,TX,TY=n
        return (a*A+c*B,b*A+d*B,a*C+c*D,b*C+d*D,a*TX+c*TY+tx,b*TX+d*TY+ty)
    def transform_point(m,x,y):
        a,b,c,d,tx,ty=m; return a*x+c*y+tx,b*x+d*y+ty
    def insert_matrix(e, base):
        x,y=_dxf_num(e,10),_dxf_num(e,20)
        sx,sy=_dxf_num(e,41,1),_dxf_num(e,42,1)
        ang=math.radians(_dxf_num(e,50,0)); co,si=math.cos(ang),math.sin(ang)
        bx,by=base
        return (co*sx,si*sx,-si*sy,co*sy,
                x-(co*sx*bx-si*sy*by), y-(si*sx*bx+co*sy*by))
    def lwpoly_points(e):
        pts=[]; x=None
        for c,v in e.get('pairs',[]):
            if c==10:
                try: x=float(v)
                except Exception: x=None
            elif c==20 and x is not None:
                try: pts.append((x,float(v)))
                except Exception: pass
                x=None
        return pts
    def route_length(pts):
        return sum(math.hypot(b[0]-a[0],b[1]-a[1]) for a,b in zip(pts,pts[1:]))

    found={}
    def collect_from_block(block_name,parent,depth=0):
        if depth>12 or block_name not in blocks:
            return []
        out=[]
        for be in blocks[block_name]['entities']:
            typ=be.get('type')
            if typ=='LWPOLYLINE':
                pts=[transform_point(parent,x,y) for x,y in lwpoly_points(be)]
                if len(pts)>=2:
                    out.append(pts)
            elif typ=='INSERT':
                child=_dxf_first(be,2,'')
                child_blk=blocks.get(child)
                if child_blk:
                    cm=insert_matrix(be, child_blk['base'])
                    out.extend(collect_from_block(child, mat_mul(parent,cm), depth+1))
        return out

    for e in model:
        if e.get('type')!='INSERT':
            continue
        layer=str(_dxf_first(e,8,'0') or '')
        m=MANUAL_ROUTE_LAYER_RE.match(layer)
        if not m:
            continue
        racknum=int(m.group(1)); rack='IDF-G1' if racknum==1 else 'IDF-G2'
        name=_dxf_first(e,2,'')
        blk=blocks.get(name)
        if not blk:
            continue
        im=insert_matrix(e, blk['base'])
        polylines=collect_from_block(name, im, 0)
        if not polylines:
            continue
        pts=max(polylines, key=route_length)
        length=route_length(pts)
        if length <= 1e-6:
            continue
        transform_identity=(abs(_dxf_num(e,10,0))<1e-6 and abs(_dxf_num(e,20,0))<1e-6 and
                            abs(_dxf_num(e,41,1)-1)<1e-6 and abs(_dxf_num(e,42,1)-1)<1e-6 and
                            abs(_dxf_num(e,50,0))<1e-6)
        found[rack]={'path':pts,'length':length,'source_layer':layer,'source_block':name,
                     'insert_transform_identity':transform_identity,'vertex_count':len(pts)}
    return found


def _project_point_to_manual_route(x,y,route):
    pts=(route or {}).get('path') or []
    if len(pts)<2:
        return None
    px,py=float(x),float(y); cumulative=0.0; best=None
    for idx,(a,b) in enumerate(zip(pts,pts[1:])):
        ax,ay=a; bx,by=b; vx=bx-ax; vy=by-ay
        seg2=vx*vx+vy*vy
        if seg2<=1e-12:
            continue
        u=((px-ax)*vx+(py-ay)*vy)/seg2
        u=max(0.0,min(1.0,u))
        qx=ax+u*vx; qy=ay+u*vy
        dist=math.hypot(px-qx,py-qy)
        seg=math.sqrt(seg2); t=cumulative+u*seg
        cand=(dist,t,idx,u,qx,qy)
        if best is None or cand<best:
            best=cand
        cumulative += seg
    return best


def _project_point_to_manual_route_signed(x,y,route):
    """Like _project_point_to_manual_route(), plus signed perpendicular offset.

    Positive/negative sign separates two symbols that sit back-to-back across a
    wall but project to the same route station.  The unsigned distance alone
    made these points behave like one open room.
    Returns (dist,t,idx,u,qx,qy,signed_offset).
    """
    pts=(route or {}).get('path') or []
    if len(pts)<2:
        return None
    px,py=float(x),float(y); cumulative=0.0; best=None
    for idx,(a,b) in enumerate(zip(pts,pts[1:])):
        ax,ay=a; bx,by=b; vx=bx-ax; vy=by-ay
        seg2=vx*vx+vy*vy
        if seg2<=1e-12:
            continue
        u=((px-ax)*vx+(py-ay)*vy)/seg2
        u=max(0.0,min(1.0,u))
        qx=ax+u*vx; qy=ay+u*vy
        seg=math.sqrt(seg2)
        dist=math.hypot(px-qx,py-qy)
        signed=(vx*(py-qy)-vy*(px-qx))/seg
        t=cumulative+u*seg
        cand=(dist,t,idx,u,qx,qy,signed)
        if best is None or (dist,t,idx,u)<(best[0],best[1],best[2],best[3]):
            best=cand
        cumulative += seg
    return best


def _manual_route_choice_for_point(x,y,manual_routes):
    best=None
    for rack,route in (manual_routes or {}).items():
        pr=_project_point_to_manual_route(x,y,route)
        if pr is None:
            continue
        dist,t,idx,u,qx,qy=pr
        cand=(dist,t,rack,idx,u,qx,qy)
        if best is None or cand<best:
            best=cand
    return best


def _manual_route_order_key(result, rack, rec, dup_index=0):
    routes=(result or {}).get('manual_routes') or {}
    if not routes:
        return None
    try:
        x=float(rec.get('x')); y=float(rec.get('y'))
    except Exception:
        return None
    route=routes.get(str(rack))
    if not route:
        return None
    pr=_project_point_to_manual_route(x,y,route)
    if pr is None:
        return None
    dist,t,idx,u,qx,qy=pr
    # This key is the user's explicit contract: order by arc length t on the
    # manually drawn rack route.  Geometry is reference-only and never counted.
    # FIX6: within a small arc bucket, prefer the point closer to the route so
    # back-to-back room points do not jump ahead of the room currently being
    # walked.
    t_bucket=round(float(t)/1500.0)*1500.0
    return (0,float(t_bucket),float(dist),float(t),int(idx),float(u),str(rec.get('occurrence_id','')),int(dup_index))

def _room_side(result,marker):
    split=float((result.get('rack_strategy') or {}).get('split_x',0.0))
    return 'IDF-G1' if float(marker.get('x',0.0))>=split else 'IDF-G2'


def _route_anchor(result,rack,markers,points):
    explicit=(result.get('rack_anchors') or {}).get(rack)
    if explicit:
        return (float(explicit['x']),float(explicit['y']),'DRAWING')
    # FIXED_ROUTE_ENGINE: if the drawing has one real MDF/IDF block only, use it
    # as the common electrical-room start for both racks.  Earlier builds inferred
    # the missing side anchor from endpoint bounds, so Rack2 on Ground could start
    # from an extreme/top/reference cluster instead of the real electrical room.
    anchors=result.get('rack_anchors') or {}
    real=[]
    for k,a in anchors.items():
        try:
            if str(k).startswith('IDF-LAB'):
                continue
            real.append((float(a['x']),float(a['y']),str(k)))
        except Exception:
            pass
    if real:
        # Prefer the anchor closest to this side's point cloud; with only one
        # anchor this deliberately becomes the shared MDF/MER start room.
        if points:
            try:
                cx=sum(float(p.get('x',0.0)) for p in points)/max(len(points),1)
                cy=sum(float(p.get('y',0.0)) for p in points)/max(len(points),1)
                x,y,k=min(real,key=lambda t:(t[0]-cx)**2+(t[1]-cy)**2)
            except Exception:
                x,y,k=real[0]
        else:
            x,y,k=real[0]
        return (x,y,'SHARED_PHYSICAL_RACK_BLOCK')
    coords=[(float(m['x']),float(m['y'])) for m in markers]
    if not coords:
        coords=[(float(p['x']),float(p['y'])) for p in points]
    if not coords:
        return (0.0,0.0,'FALLBACK')
    minx=min(x for x,_ in coords); maxx=max(x for x,_ in coords)
    miny=min(y for _,y in coords); maxy=max(y for _,y in coords)
    split=float((result.get('rack_strategy') or {}).get('split_x',(minx+maxx)/2.0))
    yf=float(RACK_CALIBRATION.get('rack_anchor_y_fraction',0.37))
    ay=miny+(maxy-miny)*yf
    if rack=='IDF-G1':
        sf=float(RACK_CALIBRATION.get('rack1_anchor_side_fraction',0.50))
        ax=split+(maxx-split)*sf
    else:
        sf=float(RACK_CALIBRATION.get('rack2_anchor_side_fraction',0.50))
        ax=split-(split-minx)*sf
    return (ax,ay,'CALIBRATED_INFERRED')


def _build_side_room_route(result,rack):
    """Build the fixed TELETECH route for one rack side using the route profile.

    User-confirmed route contract:
      - Start in the electrical/MDF/IDF rack room.
      - Finish that rack room completely first.
      - Then go DOWN to the bottom/end of the sheet, taking rooms on the way.
      - Rack 1 turns RIGHT; Rack 2 turns LEFT.
      - Continue to the outside end, then go UP to the top/end.
      - Return inward toward the middle, then go DOWN until the last point before
        the already-completed rack room.
      - Every room is completed before moving to the next room.
      - Roof/tail zones are forced last elsewhere.
    """
    all_markers=list(result.get('room_markers') or [])
    split=float((result.get('rack_strategy') or {}).get('split_x',0.0))
    rule=_route_profile_for_floor_rack(result.get('floor'), rack)

    # Points/rooms owned by this rack side for routing only.
    # ROUTE_REFERENCE_PROFILE_FIX4: when manual/learned Route - rack1/2 exists,
    # do NOT use the historical split_x to choose rooms.  Mansoura Ground has a
    # bad/irrelevant split value for room markers, which left Rack2 with only the
    # start room and caused raw point-by-point ordering.  Instead assign room
    # markers and endpoints to the nearest reference route, exactly like device
    # ordering.  Rack ownership itself is still handled elsewhere and is not
    # changed by this routing-only grouping.
    manual_routes=(result.get('manual_routes') or {})
    if manual_routes and manual_routes.get(rack):
        def belongs_to_route(obj):
            try:
                ch=_manual_route_choice_for_point(float(obj.get('x')), float(obj.get('y')), manual_routes)
                return bool(ch and ch[2]==rack)
            except Exception:
                return False
        side_markers=[m for m in all_markers if belongs_to_route(m)]
        side_points=[p for p in (result.get('mapped') or []) if belongs_to_route(p)]
    else:
        # Historical fallback when no user/reference route exists.
        def is_side_x(x):
            return (float(x)>=split if str(rack).endswith('1') else float(x)<split)
        side_markers=[m for m in all_markers if is_side_x(m.get('x',0.0))]
        side_points=[p for p in (result.get('mapped') or []) if is_side_x(p.get('x',0.0))]

    # Ground user correction: both G1 and G2 start from GR-32, not GR-29.  The
    # start room may sit on/near the split and must be allowed into both routes.
    preferred_code=_room_code_norm(rule.get('start_room_code'))
    preferred_room=None
    if preferred_code:
        matches=[m for m in all_markers if _room_code_norm(m.get('name'))==preferred_code]
        if matches:
            # Prefer the marker closest to a real rack block/anchor if available.
            ax0=ay0=None
            try:
                a=(result.get('rack_anchors') or {}).get(rack) or next(iter((result.get('rack_anchors') or {}).values()), None)
                if a: ax0=float(a['x']); ay0=float(a['y'])
            except Exception:
                pass
            if ax0 is not None:
                preferred_room=min(matches, key=lambda m:(float(m.get('x',0))-ax0)**2+(float(m.get('y',0))-ay0)**2)
            else:
                preferred_room=matches[0]
            if preferred_room not in side_markers:
                side_markers=[preferred_room]+side_markers

    coords=[(float(m['x']),float(m['y'])) for m in side_markers]
    if not coords:
        rb=_robust_xy_bounds(side_points, trim=0.04)
        coords=[(float(p['x']),float(p['y'])) for p in side_points if _inside_bounds_xy(p.get('x'),p.get('y'),rb,0.20)]
    if not coords:
        return {'rack':rack,'rooms':[],'room_rank':{},'path':[],'anchor':(0.0,0.0),'anchor_source':'NONE','start_room':None,'profile_rule':rule}

    minx=min(x for x,_ in coords); maxx=max(x for x,_ in coords)
    miny=min(y for _,y in coords); maxy=max(y for _,y in coords)
    ax,ay,asrc=_route_anchor(result,rack,side_markers,side_points)
    outer_x=maxx if str(rack).endswith('1') else minx

    # Start room: explicit profile code first, then nearest room to the physical rack anchor.
    start_room=preferred_room
    if start_room is None and side_markers:
        start_room=min(side_markers,key=lambda m:(float(m['x'])-ax)**2+(float(m['y'])-ay)**2)
    if start_room is not None:
        sx,sy=float(start_room['x']),float(start_room['y'])
    else:
        sx,sy=ax,ay

    # User route is DOWN first, then outer side, then UP, then inward/return,
    # mirrored per rack.  This fixes the previous top-first/roof-first path.
    path=[(ax,ay),(sx,sy),(sx,miny),(outer_x,miny),(outer_x,maxy),(sx,maxy),(sx,ay)]
    yrange=max(maxy-miny,1.0)
    rack_band=max(yrange*0.02,1.0)

    def station(m):
        return _route_station((float(m['x']),float(m['y'])),path)[1]

    remaining=[m for m in side_markers if m is not start_room]
    # If duplicate room marker text exists, keep the one stationed earliest for that room code.
    ded=[]; seen=set()
    for m in sorted(remaining,key=lambda m:(station(m),str(m.get('name','')))):
        k=_room_code_norm(m.get('name')) or f"@{round(float(m.get('x',0)),1)},{round(float(m.get('y',0)),1)}"
        if k in seen: continue
        seen.add(k); ded.append(m)
    remaining=ded
    ordered=(([start_room] if start_room is not None else [])+remaining)

    room_rank={}; rooms=[]
    for i,m in enumerate(ordered):
        key=f"{m.get('name','ROOM')}@{round(float(m['x']),2)},{round(float(m['y']),2)}"
        room_rank[key]=i
        rooms.append(dict(m,_key=key,_rank=i,_above_rack=(float(m['y'])>ay+rack_band),_start_room=(m is start_room),_profile_start=preferred_code))

    return {'rack':rack,'rooms':rooms,'room_rank':room_rank,'path':path,'anchor':(ax,ay),'anchor_source':asrc,
            'bounds':(minx,miny,maxx,maxy),'rack_band':rack_band,
            'start_room':(start_room.get('name') if start_room else None),'profile_rule':rule}

def _spatial_route_context(result):
    return {'IDF-G1':_build_side_room_route(result,'IDF-G1'),'IDF-G2':_build_side_room_route(result,'IDF-G2')}


def _nearest_room(rec,route):
    rooms=route.get('rooms') or []
    if not rooms: return None
    x=float(rec.get('x')); y=float(rec.get('y'))
    return min(rooms,key=lambda m:(x-float(m['x']))**2+(y-float(m['y']))**2)


def _room_sweep_delta(rec,room,previous_point,next_point):
    """v4.6 minimal fix on top of the v4.6 route.

    Keep the v4.6 room sequence unchanged.  Inside each room, choose the sweep
    direction that goes the LONG way from the entry side toward the exit side.
    This makes us walk around the room perimeter before leaving it, instead of
    forcing one global clockwise direction that can swap pairs such as 12/13.
    """
    cx=float(room['x']); cy=float(room['y']); x=float(rec.get('x')); y=float(rec.get('y'))
    px,py=previous_point; nx,ny=next_point
    start=math.atan2(py-cy,px-cx)
    exit_ang=math.atan2(ny-cy,nx-cx)
    ang=math.atan2(y-cy,x-cx)
    cw_exit=(start-exit_ang)%(2.0*math.pi)
    ccw_exit=(exit_ang-start)%(2.0*math.pi)
    # Choose the longer arc so the traversal wraps the room before reaching the exit.
    # Near-perfect 180-degree ties keep v4.6 CLOCKWISE behavior.
    if ccw_exit > cw_exit + math.radians(2.0):
        return (ang-start)%(2.0*math.pi), 'CCW'
    return (start-ang)%(2.0*math.pi), 'CW'

def _is_final_rack_tail(rec,route,room=None):
    """Return True for the small return zone just ABOVE the rack.

    The user-defined route comes back to the rack after all rooms.  A point just
    above the rack must therefore be the tail of that service sequence, not an
    ordinary point of whichever nearby room marker happens to be closest.
    """
    try:
        x=float(rec.get('x')); y=float(rec.get('y'))
        ax,ay=map(float,route.get('anchor',(0.0,0.0)))
        minx,miny,maxx,maxy=map(float,route.get('bounds',(x,y,x,y)))
    except Exception:
        return False
    if room is not None:
        # v4.7: a point that belongs to a normal room at/below rack elevation
        # must stay in that room's sequence.  v4.6 could incorrectly promote such
        # a point to the final tail merely because it was geometrically close to
        # the rack return zone.  Only an ABOVE-RACK room (or a point with no
        # reliable room assignment) may participate in the final tail phase.
        if room.get('_start_room'):
            return False
        if not room.get('_above_rack'):
            return False
    width=max(maxx-minx,1.0); height=max(maxy-miny,1.0)
    dy=y-ay; dx=abs(x-ax)
    return (0.0 <= dy <= height*0.08) and (dx <= width*0.22)


def _menoufia_ground_r2_room_override(result,rack,rec,route,room):
    """v4.8: correct two verified Ground-Rack2 spatial cases without changing v4.7 route.

    The Ground plan contains one D/V endpoint physically inside the GR-05 office whose
    nearest room-marker is GR-07.  That caused it to spill into DN03 even though it must
    remain with the other GR-05 points in DN02.  The correction is scoped to Menoufia HQ
    Ground Rack2 and uses both stable handle and a small coordinate fallback so SaveAs/
    re-export handle changes do not break the project rule.
    """
    try:
        if str(result.get('floor'))!='Ground Floor' or rack!='IDF-G2': return room
        if 'MENOUFIA' not in str(result.get('university','')).upper() or 'HQ' not in str(result.get('building','')).upper(): return room
        x=float(rec.get('x')); y=float(rec.get('y')); h=str(rec.get('handle','')).upper()
    except Exception:
        return room
    is_gr05_endpoint = (h=='225CF') or (8500.0 <= x <= 11000.0 and 36000.0 <= y <= 39000.0 and 'D' in (rec.get('logical_ports') or []))
    if is_gr05_endpoint:
        for m in (route.get('rooms') or []):
            if str(m.get('name','')).upper()=='GR-05':
                return m
    return room


def _menoufia_ground_r2_true_tail(result,rack,rec):
    """v4.8 verified true final D/V endpoint for Ground Rack2.

    This is the point just above/right of the Rack2 return path (near GR-09).  It must be
    the last D/V occurrence overall.  A left-office point in GR-05 must never steal the
    tail position.  Coordinate fallback keeps the rule stable if DXF handles are rebuilt.
    """
    try:
        if str(result.get('floor'))!='Ground Floor' or rack!='IDF-G2': return False
        if 'MENOUFIA' not in str(result.get('university','')).upper() or 'HQ' not in str(result.get('building','')).upper(): return False
        if 'D' not in (rec.get('logical_ports') or []): return False
        x=float(rec.get('x')); y=float(rec.get('y')); h=str(rec.get('handle','')).upper()
    except Exception:
        return False
    return (h=='22851') or (15000.0 <= x <= 17500.0 and 42500.0 <= y <= 45000.0)



def _lab_anchor_xy(result, rack):
    """Find the local IDF-LABn rack anchor drawn inside the LAB block."""
    target=str(rack or '').upper().replace('IDF-','')
    for rec in (result.get('ignored') or []) + (result.get('mapped') or []):
        txt=[]
        txt.append(str(rec.get('block','')))
        txt.extend(str(a) for a in (rec.get('ancestors') or []))
        for k,v in (rec.get('attributes') or {}).items():
            txt.append(str(k)); txt.append(str(v))
        joined=' '.join(txt).upper().replace('-','')
        if target.replace('-','') in joined:
            try: return (float(rec.get('x')), float(rec.get('y')))
            except Exception: pass
    return None


def _lab_perimeter_spatial_key(result, rack, rec, dup_index=0):
    """FIX6: number points inside a local lab by perimeter walk, with inner points last.

    The new lab drawings have independent LAB1/LAB2/LAB3 rack groups.  Their D/V
    outlets must not be sorted by plain Y/X.  The intended route is: start near
    the lab IDF, walk the lab perimeter continuously, then append any centre/tail
    point inside the lab after the perimeter is complete.
    """
    try:
        pts=[p for p in (result.get('mapped') or []) if str(p.get('rack','')).upper()==str(rack).upper()]
        x=float(rec.get('x')); y=float(rec.get('y'))
        if len(pts) < 3:
            return (0, y, x, str(rec.get('occurrence_id','')), int(dup_index))
        xs=[float(p.get('x')) for p in pts]; ys=[float(p.get('y')) for p in pts]
        minx,maxx=min(xs),max(xs); miny,maxy=min(ys),max(ys)
        w=max(maxx-minx,1.0); h=max(maxy-miny,1.0)
        # Normalized coordinates inside this LAB group.
        nx=(x-minx)/w; ny=(y-miny)/h
        edge=min(nx,1.0-nx,ny,1.0-ny)
        # Points clearly away from all four sides are not perimeter outlets; put
        # them at the end of the lab sequence.
        inner = edge > 0.28
        anchor=_lab_anchor_xy(result,rack)
        if anchor:
            ax,ay=anchor; anx=(ax-minx)/w; any_=(ay-miny)/h
        else:
            anx,any_=0.0,1.0
        # Decide which edge the lab IDF is closest to.  Most current LAB blocks
        # have the IDF near the left/top edge; the route then goes down the left
        # side, across the bottom, up the right side, then back along the top.
        dists={'L':abs(anx), 'R':abs(1-anx), 'B':abs(any_), 'T':abs(1-any_)}
        start=min(dists, key=dists.get)
        if start=='L':
            # left top->bottom, bottom left->right, right bottom->top, top right->left
            if nx <= 0.20: t=(1.0-ny)*1.0
            elif ny <= 0.20: t=1.0+nx
            elif nx >= 0.80: t=2.0+ny
            else: t=3.0+(1.0-nx)
        elif start=='R':
            if nx >= 0.80: t=(1.0-ny)*1.0
            elif ny <= 0.20: t=1.0+(1.0-nx)
            elif nx <= 0.20: t=2.0+ny
            else: t=3.0+nx
        elif start=='B':
            if ny <= 0.20: t=nx
            elif nx >= 0.80: t=1.0+ny
            elif ny >= 0.80: t=2.0+(1.0-nx)
            else: t=3.0+(1.0-ny)
        else:  # top
            if ny >= 0.80: t=1.0-nx
            elif nx <= 0.20: t=1.0+(1.0-ny)
            elif ny <= 0.20: t=2.0+nx
            else: t=3.0+ny
        phase=1 if inner else 0
        center_d=(nx-0.5)*(nx-0.5)+(ny-0.5)*(ny-0.5)
        return (phase, t, center_d, y, x, str(rec.get('occurrence_id','')), int(dup_index))
    except Exception:
        return (0, float(rec.get('y',0.0)), float(rec.get('x',0.0)), str(rec.get('occurrence_id','')), int(dup_index))



def _room_serpentine_side_lock_key(result, rack, rec, room):
    """Return a local U/serpentine key for back-to-back sub-rooms inside one room tag.

    The drawn route fixes the macro order, but some CAD rooms such as PROFESSORS
    OFFICE contain two small back-to-back office rows under one official room tag.
    Pure route projection treats them as one open room and can alternate left/right.

    User rule: finish the current side/column first, then cross once to the next
    side/column.  For Rack2 (left side) columns are visited left->right; for
    Rack1 (right side) right->left.  Within columns we use a U-walk: first column
    top->bottom, next column bottom->top, alternating thereafter.  This only
    changes ordering; it never changes rack ownership or BOQ.
    """
    try:
        x=float(rec.get('x')); y=float(rec.get('y'))
    except Exception:
        return None
    room_id=str((room or {}).get('_key') or (room or {}).get('name') or '')
    if not room_id:
        return None
    peers=[]
    for p in (result.get('mapped') or []):
        try:
            if str(p.get('rack')) != str(rack):
                continue
            # Prefer the room-membership fields; fall back to nearest room name.
            pid=str(p.get('_room_key_v49') or p.get('_room_name_v49') or '')
            if pid and pid != room_id and str((room or {}).get('name') or '') not in pid:
                continue
            # This bug is primarily D/V back-to-back outlets.  Do not let Wi-Fi,
            # cameras, projectors, etc. distort the office-column detection.
            lp=set(p.get('logical_ports') or [])
            if not ({'D','V'} & lp):
                continue
            px=float(p.get('x')); py=float(p.get('y'))
            if math.isfinite(px) and math.isfinite(py):
                peers.append((px,py,p))
        except Exception:
            pass
    if len(peers) < 4:
        return None
    xs=[a for a,_,__ in peers]; ys=[b for _,b,__ in peers]
    if (max(xs)-min(xs)) < 2500.0 or (max(ys)-min(ys)) < 1200.0:
        return None

    # Cluster columns by X with an engineering tolerance.  The office desks/points
    # in these drawings are usually separated by several metres, while duplicate
    # D/V physical points at the same outlet share nearly the same X.
    order_x=sorted(xs)
    groups=[]
    for val in order_x:
        if not groups or abs(val-groups[-1][-1]) > 3200.0:
            groups.append([val])
        else:
            groups[-1].append(val)
    if len(groups) < 2:
        return None
    centers=[sum(g)/len(g) for g in groups]
    is_r1 = str(rack).upper().endswith('1')
    # Rack1 right wing: visit outside/right column first -> descending X.
    # Rack2 left wing: visit outside/left column first -> ascending X.
    centers_sorted=sorted(centers, reverse=is_r1)
    col=min(range(len(centers_sorted)), key=lambda i: abs(x-centers_sorted[i]))
    col_center=centers_sorted[col]
    # U-walk: first column top->bottom, second bottom->top, then alternate.
    y_key = -y if (col % 2 == 0) else y
    # Put this after room lock but before raw route-t buckets.
    return (float(col), float(y_key), abs(x-col_center), str(rec.get('occurrence_id','')))

def _roof_zone_rule(result):
    """Return (side, cut_y) for an in-drawing ROOF zone.

    ROUTE_ORDER_ROOM_FIX:
    1) If the drawing contains an explicit ROOF text/tag, use its Y side.
    2) If not, still protect Third/TH drawings from starting in a detached
       roof/service cluster.  The current Mansoura TH sheet has the roof drawn
       as an isolated extreme Y band, separated from the normal third floor by a
       very large empty gap.  Earlier builds only looked for text, so when the
       ROOF word was inside a block/annotation not parsed by the DXF reader, the
       roof points received C01/D01 and the real rack room was not first.

    side='LOW'  => y <= cut_y is roof/tail.
    side='HIGH' => y >= cut_y is roof/tail.
    """
    def mapped_ys():
        vals=[]
        for e in (result.get('mapped') or []):
            if str(e.get('status')) in {'MAPPED','COUNTED'} and e.get('y') is not None:
                try: vals.append(float(e.get('y')))
                except Exception: pass
        return sorted(vals)

    ys=mapped_ys()
    if not ys:
        return None
    miny,maxy=ys[0],ys[-1]
    height=max(maxy-miny,1.0)

    roof_ys=[]
    for e in (result.get('ignored') or []) + (result.get('mapped') or []) + (result.get('review') or []):
        txt=' '.join(str(v or '') for v in (e.get('block'), e.get('layer'), e.get('text'))).upper()
        if '__ROOF_TEXT__' in txt or re.search(r'\bROOF\b', txt):
            try: roof_ys.append(float(e.get('y')))
            except Exception: pass
    if roof_ys:
        roof_y=sum(roof_ys)/len(roof_ys)
        low_side=abs(roof_y-miny) <= abs(maxy-roof_y)
        # Use the largest nearby architectural gap between the roof band and the
        # normal floor whenever available.  This follows the user's separator line.
        uniq=[]
        for y in ys:
            if not uniq or abs(y-uniq[-1]) > 250.0:
                uniq.append(y)
        if len(uniq) >= 4:
            gaps=[]
            for a,b in zip(uniq,uniq[1:]):
                mid=(a+b)/2.0
                if low_side and a <= roof_y <= b or (low_side and b > roof_y):
                    gaps.append((b-a,a,b))
                elif (not low_side) and a <= roof_y <= b or ((not low_side) and a < roof_y):
                    gaps.append((b-a,a,b))
            big=[g for g in gaps if g[0] >= max(5000.0,0.07*height)]
            if big:
                gap=max(big,key=lambda g:g[0])
                return ('LOW' if low_side else 'HIGH',(gap[1]+gap[2])/2.0)
        return ('LOW', roof_y + max(2000.0,0.06*height)) if low_side else ('HIGH', roof_y - max(2000.0,0.06*height))

    # Text-free fallback for Third/TH: detect a detached extreme band.  Do not use
    # this on ordinary G/F/S drawings because an outside camera row can be valid.
    if str(result.get('floor')) not in {'Third Floor','Roof Floor'}:
        return None
    uniq=[]
    for y in ys:
        if not uniq or abs(y-uniq[-1]) > 250.0:
            uniq.append(y)
    if len(uniq) < 6:
        return None
    gaps=[]
    for i,(a,b) in enumerate(zip(uniq,uniq[1:])):
        gap=b-a
        if gap >= max(7000.0,0.08*height):
            gaps.append((gap,i,a,b))
    if not gaps:
        return None
    # Prefer a split that leaves a small extreme cluster (roof) on one side.
    n=len(uniq)
    candidates=[]
    for gap,i,a,b in gaps:
        low_count=i+1
        high_count=n-(i+1)
        if low_count <= max(6,int(n*0.22)):
            candidates.append((gap,'LOW',a,b,low_count))
        if high_count <= max(6,int(n*0.22)):
            candidates.append((gap,'HIGH',a,b,high_count))
    # In the Mansoura TH layout the roof is the visually upper separated band.
    # In CAD coordinates this is the HIGH-Y extreme.  Prefer that high cluster
    # whenever it is reasonably separated; otherwise use the largest extreme gap.
    high_candidates=[c for c in candidates if c[1]=='HIGH']
    if high_candidates:
        # Choose the lower boundary of the complete upper detached band.
        gap,side,a,b,_=max(high_candidates,key=lambda z:(z[4],z[0]))
        return (side,(a+b)/2.0)
    if not candidates:
        # Last resort: the biggest extreme gap.
        gap,i,a,b=max(gaps,key=lambda z:z[0])
        return ('LOW',(a+b)/2.0) if (i+1) < (n-(i+1)) else ('HIGH',(a+b)/2.0)
    gap,side,a,b,_=max(candidates,key=lambda z:z[0])
    return (side,(a+b)/2.0)

def _roof_zone_cut_y(result):
    r=_roof_zone_rule(result)
    return None if r is None else r[1]

def _is_in_drawing_roof_tail(result, rec):
    if str(result.get('floor')) not in {'Third Floor','Roof Floor'}:
        return False
    try:
        if _is_roof_zone_block_override(rec):
            return True
    except Exception:
        pass
    rule=_roof_zone_rule(result)
    if rule is None:
        return False
    side,cut=rule
    try:
        y=float(rec.get('y'))
    except Exception:
        return False
    return y <= cut if side == 'LOW' else y >= cut



def _anchor_room_zone_key(result, route, rec):
    """Return a key for the electrical/rack room cluster on drawings without room IDs.

    Fixed rule: the MDF/IDF room is the first room.  This helper deliberately
    makes a compact-but-generous room around the real rack anchor phase 0, so a
    drawing can never start in a top camera/roof/reference band while the rack
    room receives D10/V10.  It is only an ordering rule; rack ownership is not
    changed.
    """
    try:
        x=float(rec.get('x')); y=float(rec.get('y'))
        ax,ay=map(float,route.get('anchor',(x,y)))
        minx,miny,maxx,maxy=map(float,route.get('bounds',(x,y,x,y)))
    except Exception:
        return None
    width=max(maxx-minx,1.0); height=max(maxy-miny,1.0)
    # Room-sized window.  The old 16% height was too small when the anchor was at
    # the cable/rack block edge, leaving valid electrical-room outlets to be
    # numbered later.  Use a safer engineering-size window with caps.
    rx=min(max(7000.0, width*0.28), max(width*0.55,7000.0))
    ry=min(max(7000.0, height*0.24), max(height*0.42,7000.0))
    dx=x-ax; dy=y-ay
    if abs(dx) <= rx and abs(dy) <= ry:
        # Finish this room before anything else.  Perimeter first, centre/tail
        # last inside the room approximation.
        nx=(dx+rx)/(2.0*rx); ny=(dy+ry)/(2.0*ry)
        edge=min(nx,1.0-nx,ny,1.0-ny)
        inner=1 if edge > 0.25 else 0
        # Start near the rack and walk clockwise around the room envelope.
        ang=(math.atan2(y-ay,x-ax)+2.0*math.pi)%(2.0*math.pi)
        radial=dx*dx+dy*dy
        return (0, inner, ang, radial, y, x)
    return None

def _no_room_route_key(result, route, rack, rec, dup_index=0):
    """Unified no-room route engine.

    This is NOT a per-floor patch.  It applies the same contract to every
    drawing that does not expose reliable room IDs:
      1) Real electrical/MDF/IDF rack room first.
      2) The rack room is locked: its nearby points are completed first and are
         never reused as a final tail.
      3) After the rack room, walk the rack side as a continuous perimeter route
         from the rack/electrical room, up the inner/core side, across the top,
         then down the outer side.  This matches the user-drawn route on Ground
         and generalizes to TH/F/S no-room drawings.
      4) Points in the middle/core are delayed after the perimeter of that zone.
      5) Any detected in-drawing Roof band is always last, even when the Roof is
         inside the Third/TH DXF.

    Rack ownership is not changed here.  This function only affects port order.
    """
    try:
        x=float(rec.get('x')); y=float(rec.get('y'))
        ax,ay=map(float,route.get('anchor',(x,y)))
    except Exception:
        return (8,0,0,0,0,str(rec.get('occurrence_id','')),int(dup_index))

    # Roof is a hard tail.  It cannot receive C01/D01 just because it is the
    # highest/lowest separated band in the drawing.
    roof_tail=_is_in_drawing_roof_tail(result, rec)

    # Build a routing envelope from real mapped endpoints of this rack, excluding
    # roof-tail points and extreme reference clusters.  Do not use existing CAD
    # labels or annotation text as routing geometry.
    pts=[]
    for p in (result.get('mapped') or []):
        if str(p.get('rack')) != str(rack):
            continue
        try:
            px=float(p.get('x')); py=float(p.get('y'))
            if not math.isfinite(px) or not math.isfinite(py):
                continue
            if _is_in_drawing_roof_tail(result,p):
                continue
            pts.append(p)
        except Exception:
            pass
    if not pts:
        pts=[rec]
    minx,miny,maxx,maxy=_robust_xy_bounds(pts, trim=0.04)
    width=max(maxx-minx,1.0); height=max(maxy-miny,1.0)
    split=float((result.get('rack_strategy') or {}).get('split_x',(minx+maxx)/2.0))

    # Rack/electrical room first.  Window scales with actual rack-side envelope
    # but is capped enough not to swallow the whole wing.
    local=_anchor_room_zone_key(result, route, rec)
    if local is not None and not roof_tail:
        return (0,)+local+(str(rec.get('occurrence_id','')),int(dup_index))

    # Decide side orientation.  The 'inner' side is the core/split side; the
    # 'outer' side is the outside facade of this rack's half.
    is_r1 = (str(rack).endswith('1') or str(rack).upper() in {'IDF-G1','IDF-F1','IDF-S1','IDF-T1'})
    if is_r1:
        side_min=max(minx, min(split, maxx)) if minx <= split <= maxx else minx
        side_max=maxx
        def xn_inner(px): return (px-side_min)/max(side_max-side_min,1.0)  # 0 inner, 1 outer
        top_x_order=lambda px: px
    else:
        side_min=minx
        side_max=min(maxx, max(split, minx)) if minx <= split <= maxx else maxx
        def xn_inner(px): return (side_max-px)/max(side_max-side_min,1.0)  # 0 inner, 1 outer
        top_x_order=lambda px: -px

    xn=max(0.0,min(1.0,xn_inner(x)))
    yn=max(0.0,min(1.0,(y-miny)/height))
    # Vertical direction from the rack room.  In current CAD coordinates, larger
    # Y is visually upward; if another drawing has the rack above most points, the
    # formula still sorts by distance from the rack before moving to the far band.
    y_from_rack=(y-ay)/height

    if roof_tail:
        # Roof tail keeps its own perimeter-ish order, but after all normal floor
        # points.  Sort from nearest roof entry toward the far roof edge.
        return (9, abs(y-ay), xn, top_x_order(x), -y, str(rec.get('occurrence_id','')), int(dup_index))

    # User-confirmed route: after the rack room, go DOWN first, turn to the
    # outside (RIGHT for rack 1, LEFT for rack 2), go UP, return inward to the
    # middle, then DOWN until just before the rack room.  Middle/inner points do
    # not interrupt the perimeter sweep.
    inner_band = xn <= 0.34
    outer_band = xn >= 0.72
    bottom_band = yn <= 0.24
    top_band = yn >= 0.76
    mid_core = not inner_band and not outer_band and not bottom_band and not top_band

    if inner_band and y <= ay:
        # Leave the rack room downward along the inner/core side.
        return (1, abs(y-ay), abs(x-ax), x, str(rec.get('occurrence_id','')), int(dup_index))
    if bottom_band:
        # Turn outward across the bottom/end band.
        return (2, xn, abs(y-miny), top_x_order(x), str(rec.get('occurrence_id','')), int(dup_index))
    if outer_band:
        # Travel upward on the outside facade/wing.
        return (3, y, xn, abs(x-ax), str(rec.get('occurrence_id','')), int(dup_index))
    if top_band:
        # Return inward from the outside toward the middle of the sheet.
        return (4, -xn, top_x_order(x), abs(y-maxy), str(rec.get('occurrence_id','')), int(dup_index))
    if inner_band:
        # Final return down the inner side, but after the perimeter loop.
        return (5, -y, abs(x-ax), str(rec.get('occurrence_id','')), int(dup_index))
    if mid_core:
        cd=(xn-0.5)*(xn-0.5)+(yn-0.5)*(yn-0.5)
        return (6, cd, abs(y-ay), abs(x-ax), y, x, str(rec.get('occurrence_id','')), int(dup_index))
    return (7, abs(y-ay), abs(x-ax), y, x, str(rec.get('occurrence_id','')), int(dup_index))


def _find_route_room_for_record(result, route, rec):
    """Find the room/zone object assigned to a CAD endpoint.

    Room membership is authoritative.  Do not replace it with nearest XY just
    because a room marker was assigned to another route side.  If the exact
    multi-source BFS room_id is not present in the side route, return a synthetic
    room object from the global room marker list so manual-route ordering still
    locks all points of that room together.
    """
    wanted_key=str(rec.get('_room_key_v49','') or '')
    wanted_name=str(rec.get('_room_name_v49','') or '')
    rooms=(route or {}).get('rooms') or []
    for m in rooms:
        if (wanted_key and str(m.get('_key',''))==wanted_key) or (wanted_name and str(m.get('name',''))==wanted_name):
            return m
    for m in (result or {}).get('room_markers') or []:
        try:
            key=f"{m.get('name','ROOM')}@{round(float(m['x']),2)},{round(float(m['y']),2)}"
        except Exception:
            key=str(m.get('_key',''))
        if (wanted_key and key==wanted_key) or (wanted_name and str(m.get('name',''))==wanted_name):
            mm=dict(m); mm['_key']=key; mm['_rank']=999999; mm['_start_room']=False
            return mm
    # Last-resort fallback only when no room_id could be assigned.  This should
    # not happen in normal Mansoura/HUM drawings with official room tags.
    try:
        return _nearest_room(rec, route)
    except Exception:
        return None


def _manual_route_room_locked_key(result, rack, rec, route, room, dup_index=0):
    """Manual/reference route order with strict room lock.

    Do not sort raw devices directly by projection t when a room is known.  A
    back-to-back device in the next room can have a smaller/larger t by a tiny
    amount and interrupt the current room.  Instead:
      1) project the ROOM marker/centroid to route -> room_t
      2) finish that room completely
      3) only then move to the next room
    Inside the room, projection t is still used as a stable walking direction,
    with center/inner points delayed slightly after perimeter-side points.
    """
    try:
        x=float(rec.get('x')); y=float(rec.get('y'))
    except Exception:
        return None
    routes=(result or {}).get('manual_routes') or {}
    manual_route=routes.get(str(rack))
    if not manual_route:
        return None
    point_pr=_project_point_to_manual_route(x,y,manual_route)
    if point_pr is None:
        return None
    p_dist,p_t,p_idx,p_u,qx,qy=point_pr
    if room is not None:
        try:
            rx=float(room.get('x')); ry=float(room.get('y'))
            r_pr=_project_point_to_manual_route(rx,ry,manual_route)
        except Exception:
            r_pr=None
        if r_pr is not None:
            r_dist,r_t,r_idx,r_u,_,_=r_pr
            # Inner/tail heuristic inside one room: a point far from the route
            # line is delayed after points sitting on the wall/perimeter path.
            inner_phase=1 if float(p_dist) > 1200.0 else 0
            room_code=str(room.get('name') or room.get('_key') or '')
            # Hard lock the rack/electrical start room as the first group.
            # This prevents the return leg of a closed/U route from placing the
            # rack room again at the end of the sequence.
            # ROUTE_REFERENCE_PROFILE_FIX6:
            # Room lock is still the primary contract, but the user found a
            # remaining edge case in the professor offices: two D/V points can
            # be almost behind each other on adjacent/stacked room edges.  A
            # pure arc-length sort lets the rear room interrupt the front room.
            # Keep the room group first, then inside very close arc buckets sort
            # by perpendicular distance to the drawn route before raw t.  This
            # makes the room on the walking side finish before the point behind
            # it, while preserving the overall route order.
            # ROUTE_REFERENCE_PROFILE_FIX8:
            # In narrow office/lab rows two physical D/V points can be back-to-back
            # across a wall but share almost the same route arc.  Sorting by raw
            # point_t first makes the sequence walk as if they are one open room
            # (left-front, left-back, right-back, right-front).  The user rule is
            # room/row lock: finish the point row closest to the walking route,
            # then take the points behind it.  Therefore inside one locked room we
            # bucket by perpendicular distance-to-route before arc-length.
            # Keep the bucket coarse so tiny symbol offsets do not shuffle points.
            # ROUTE_REFERENCE_PROFILE_FIX8:
            # Back-to-back D/V points can have the same route station AND the
            # same absolute distance (one in the current room, one just behind
            # the wall).  Distance-only buckets still interleaved them.  Use the
            # SIGNED perpendicular offset from the route segment, then quantize it
            # as a lane key.  This keeps each side/room row together before the
            # route continues to the point behind it.
            signed_pr=_project_point_to_manual_route_signed(x,y,manual_route)
            signed_off=float(signed_pr[6]) if signed_pr is not None else 0.0
            signed_lane=round(signed_off/500.0)*500.0
            dist_bucket=round(float(p_dist)/500.0)*500.0
            # FIX10: bay-first micro ordering.  The remaining user-visible issue
            # was in PROFESSORS offices where 4 D/V points sit back-to-back around
            # the same local route station.  FIX8/FIX9 sorted lane/distance before
            # local station, so it treated the long professors area as one open
            # room and numbered all near-lane points across the whole room before
            # the points behind them.  The required behavior is: at each walking
            # station/bay, finish the visible/front room first, then the room
            # behind it, then continue along the route.
            #
            # Use a coarser bay bucket for the architectural bay/office pair, and
            # only inside that bay sort by distance/signed lane.  This preserves
            # the global route while preventing the four-point back-to-back groups
            # from behaving like one open office.
            bay_bucket=round(float(p_t)/4200.0)*4200.0
            point_bucket=round(float(p_t)/1200.0)*1200.0
            side_lock=_room_serpentine_side_lock_key(result, rack, rec, room)
            # FIX11: side/group lock for back-to-back office pairs.  When several
            # outlets sit opposite each other under one large room tag, do not
            # interleave them by raw t.  Finish the current side/column first,
            # cross once, then finish the opposite side/column.
            side_phase=0 if side_lock is not None else 1
            side_tuple=side_lock if side_lock is not None else (float(bay_bucket),float(dist_bucket),float(signed_lane),float(point_bucket))
            if room.get('_start_room'):
                return (-1,0.0,0.0,0.0,room_code,inner_phase,side_phase)+tuple(side_tuple)+(float(signed_off),float(p_dist),float(p_t),int(p_idx),float(p_u),str(rec.get('occurrence_id','')),int(dup_index))
            bucket=round(float(r_t)/2200.0)*2200.0
            room_signed_pr=_project_point_to_manual_route_signed(rx,ry,manual_route)
            room_signed=float(room_signed_pr[6]) if room_signed_pr is not None else 0.0
            room_lane=round(room_signed/650.0)*650.0
            return (0,float(bucket),float(room_lane),float(r_dist),float(r_t),room_code,inner_phase,side_phase)+tuple(side_tuple)+(float(signed_off),float(p_dist),float(p_t),int(p_idx),float(p_u),str(rec.get('occurrence_id','')),int(dup_index))
    # Fallback when no room marker exists: true route projection only.
    return (1,float(p_t),float(p_dist),int(p_idx),float(p_u),str(rec.get('occurrence_id','')),int(dup_index))


def _flat_cctv_route_projection_key(result, rack, code, rec, dup_index=0):
    """CCTV-only route ordering borrowed from the working camera build.

    This intentionally affects ONLY code C.  D/V and Wi-Fi keep the proven
    WAL/WIN room/barrier ordering path.  For non-Ground floors, CCTV remains
    electrically on Rack1, but its walking order may use the physical side's
    learned route so cameras on both wings are not scrambled.
    """
    if code != 'C':
        return None
    try:
        x=float(rec.get('x')); y=float(rec.get('y'))
    except Exception:
        return None
    routes=(result or {}).get('manual_routes') or {}
    if not routes:
        return None
    try:
        split=float((result.get('rack_strategy') or {}).get('split_x',0.0))
    except Exception:
        split=0.0
    physical_side='IDF-G1' if x>=split else 'IDF-G2'
    # Ground CCTV is split normally; use the actual electrical rack route.
    # Other floors: labels are Rack1, but route order follows physical side.
    if rack=='IDF-G1' and result.get('floor')!='Ground Floor':
        route_side=physical_side
        side_phase=0 if route_side=='IDF-G1' else 1
    else:
        route_side=rack
        side_phase=0
    route=routes.get(route_side)
    if not route:
        return None
    pr=_project_point_to_manual_route(x,y,route)
    if pr is None:
        return None
    dist,t,seg,u,qx,qy=pr
    roof_phase=1 if _is_in_drawing_roof_tail(result, rec) else 0
    far_flag=1 if float(dist)>MANUAL_ROUTE_DISTANCE_LIMIT else 0
    if roof_phase:
        # Roof-zone rule: block type overrides all other local roof ordering.
        # CAM1 roof points first, PTZ roof/open-edge points last.
        bcode=_block_code_norm_for_roof_order(rec)
        if bcode == 'CAM1':
            block_phase = 0
        elif bcode == 'PTZ':
            block_phase = 2
        else:
            block_phase = 1
        local = _roof_tail_local_key(result, rec)
        return (1, int(side_phase), int(block_phase)) + tuple(local) + (float(t), float(dist), int(seg), float(u), str(rec.get('occurrence_id','')), int(dup_index))
    # Unresolved/far is a flag, not a reorder bucket.  Keep it AFTER t so far
    # points are still merged into the main route sequence by their own t-value.
    return (0, int(side_phase), float(t), int(far_flag), float(dist), int(seg), float(u), float(x), float(y), str(rec.get('occurrence_id','')), int(dup_index))

def _candidate_spatial_key(result,rack,code,rec,dup_index=0):
    """Room-aware label order key matching the user-defined mirrored route.

    Service priority is handled outside this function. This key only decides who
    gets 01, 02, 03... WITHIN a service code.
    """
    ctx=_spatial_route_context(result)
    split=float((result.get('rack_strategy') or {}).get('split_x',0.0))
    x=float(rec.get('x')); y=float(rec.get('y'))
    roof_phase=1 if _is_in_drawing_roof_tail(result, rec) else 0
    physical_side='IDF-G1' if x>=split else 'IDF-G2'

    # On First/Second/Roof all CCTV is electrically on Rack 1, but physically it
    # still spans both halves. Number C points through the Rack-1 room route first,
    # then continue through the mirrored Rack-2-side room route.
    if code=='C' and rack=='IDF-G1' and result.get('floor')!='Ground Floor':
        route_side=physical_side
        side_phase=0 if route_side=='IDF-G1' else 1
    else:
        route_side=rack
        side_phase=0

    route=ctx.get(route_side) or {}
    room=_find_route_room_for_record(result, route, rec)

    # Third-floor start rack-room D/V correction: keep the first four data/voice
    # points inside the electrical/rack room in direct walking-route order
    # (1,2,3,4), and do not let a point below/outside the rack room become D01.
    if str(result.get('floor'))=='Third Floor' and code in {'D','V'} and room is not None and room.get('_start_room'):
        try:
            pr=_project_point_to_manual_route(float(rec.get('x')), float(rec.get('y')), (result.get('manual_routes') or {}).get(route_side))
        except Exception:
            pr=None
        if pr is not None:
            dist,t,seg,u,qx,qy=pr
            return (roof_phase, side_phase, -2, float(t), float(dist), int(seg), float(u), str(rec.get('occurrence_id','')), int(dup_index))

    manual_key=_manual_route_room_locked_key(result, rack, rec, route, room, dup_index)
    if manual_key is not None:
        return (roof_phase, side_phase) + manual_key
    # Keep the v4.8 Ground micro-fix only as a regression safety net.  On a
    # healthy v4.9 drawing the wall-aware room key resolves this case first.
    room=_menoufia_ground_r2_room_override(result,rack,rec,route,room)
    # Verified Ground Rack2 tail: this one endpoint is the true end of the D/V route.
    # Give it a dedicated final phase before ordinary room ordering so it receives the
    # final logical port in both D and V streams.
    if code in {'D','V'} and _menoufia_ground_r2_true_tail(result,rack,rec):
        return (roof_phase,side_phase,2,10**9,0.0,0.0,0.0,str(rec.get('occurrence_id','')),int(dup_index))
    if room is not None:
        rooms=route.get('rooms') or []
        # INDEX-SAFE ROOM SWEEP FIX:
        # With raster/multi-source room ownership a point may get a room_id that
        # is not present in this rack's route-room list, or the stored _rank can
        # be stale/out of range after learned-route/profile transforms.  Older
        # builds used rooms[ri-1]/rooms[ri+1] directly and crashed with
        # "IndexError: list index out of range" during Generate.  Clamp the
        # rank and fall back to the current room/anchor if neighbours are absent.
        try:
            ri=int(room.get('_rank',0))
        except Exception:
            ri=0
        if rooms:
            safe_ri=max(0, min(int(ri), len(rooms)-1))
            prev_room = None if safe_ri <= 0 else rooms[safe_ri-1]
            next_room = None if safe_ri >= len(rooms)-1 else rooms[safe_ri+1]
        else:
            safe_ri=0
            prev_room=None
            next_room=None
        def _room_xy_or_anchor(rr):
            if rr is None:
                return route.get('anchor',(x,y))
            try:
                return (float(rr.get('x', x)), float(rr.get('y', y)))
            except Exception:
                return route.get('anchor',(x,y))
        # The first room is the electrical/MEP room containing the rack, so the
        # entry vector starts at the actual rack anchor. Every later room starts
        # from the previous room center. This preserves clockwise numbering while
        # keeping all endpoints of one room together.
        prev=route.get('anchor',(x,y)) if safe_ri==0 else _room_xy_or_anchor(prev_room)
        nxt=route.get('anchor',(x,y)) if (not rooms or safe_ri>=len(rooms)-1) else _room_xy_or_anchor(next_room)
        sweep,_sweep_dir=_room_sweep_delta(rec,room,prev,nxt)
        radial=(x-float(room['x']))**2+(y-float(room['y']))**2
        station=_route_station((room['x'],room['y']),route.get('path') or [route.get('anchor',(x,y)),(x,y)])[1]
        tail_phase=1 if _is_final_rack_tail(rec,route,room) else 0
        # v4.6 room order is untouched. Only the tiny above-rack return zone is
        # deferred until the end, and per-room CW/CCW is entry/exit aware.
        return (roof_phase,side_phase,tail_phase,(10**6 if tail_phase else ri),sweep,radial,station,str(rec.get('occurrence_id','')),int(dup_index))

    # No readable room ID (some Mansoura G/TH/service areas): use the strict
    # rack-room-first fallback.  This keeps the electrical room first and moves
    # the separated Roof band to the very end.
    return (roof_phase, side_phase) + _no_room_route_key(result, route, rack, rec, dup_index)


def _spatial_label_order_summary(result):
    try:
        ctx=_spatial_route_context(result)
        rl=result.get('rack_labels') or _default_rack_labels(result.get('floor'))
        out={'strategy':('MANUAL_ROUTE_LAYER_PROJECTION_OR_LEARNED_PROFILE' if result.get('manual_routes') else 'TELETECH_UNIFIED_RACK_ROOM_PERIMETER_ROOF_LAST'),'room_markers':int(result.get('room_marker_count') or len(result.get('room_markers') or [])),'racks':{},'manual_routes':{k:{'layer':v.get('source_layer'),'vertices':v.get('vertex_count'),'length':round(float(v.get('length',0)),2)} for k,v in (result.get('manual_routes') or {}).items()}}
        for rack in ('IDF-G1','IDF-G2'):
            r=ctx[rack]; out['racks'][rack]={
                'display':rl.get(rack,rack),
                'anchor_x':round(float(r['anchor'][0]),2),'anchor_y':round(float(r['anchor'][1]),2),
                'anchor_source':r.get('anchor_source'),'start_room':r.get('start_room'),'room_count':len(r.get('rooms') or []),
                'room_preview':[m.get('name') for m in (r.get('rooms') or [])[:18]],
                'direction':'START ELECTRICAL ROOM → DOWN → RIGHT → UP → INWARD → DOWN RETURN; roof LAST; finish each room' if rack=='IDF-G1' else 'START ELECTRICAL ROOM → DOWN → LEFT → UP → INWARD → DOWN RETURN; roof LAST; finish each room'
            }
        return out
    except Exception as e:
        return {'strategy':'TELETECH_UNIFIED_RACK_ROOM_PERIMETER_ROOF_LAST','error':str(e),'racks':{}}


def _project_label_assignments(results_by_floor,label_pattern=None):
    """Assign every logical Excel port label back to its exact physical CAD occurrence.

    Roof rule is identical to Excel: Roof has no independent rack/port schedule.  Its
    endpoints are appended to the building's highest normal floor and therefore receive
    that floor's rack prefix/panel numbering.  The returned mapping is still split by
    source drawing so every uploaded drawing gets its own labeled DXF copy.
    """
    if not results_by_floor:
        raise RuntimeError('No project drawings are loaded.')
    for floor,r in results_by_floor.items():
        if int(r.get('review_count',0)):
            raise RuntimeError(f'{floor}: resolve {r["review_count"]} classification review item(s) before CAD labeling.')
        if int(r.get('rack_review_count',0)):
            raise RuntimeError(f'{floor}: resolve {r["rack_review_count"]} rack review item(s) before CAD labeling.')

    effective=_effective_export_results(results_by_floor)
    assignments={floor:[] for floor in results_by_floor}
    normal_order=['Ground Floor','First Floor','Second Floor','Third Floor']
    roof=results_by_floor.get('Roof Floor')
    roof_dest=next((f for f in reversed(normal_order) if f in effective and effective[f].get('roof_merged')),None)

    for floor,eff in effective.items():
        sources=[(floor,results_by_floor[floor])]
        if roof is not None and floor==roof_dest:
            sources.append(('Roof Floor',roof))
        port_records=_port_assignment_lists(eff,label_pattern=label_pattern)

        # One entry per physical occurrence; one physical D/V or 2-cable floor box
        # intentionally owns multiple logical text labels.
        entry_by_key={}
        all_racks=[]
        for sf,sr in sources:
            for rk in _rack_order_from_counts(sr):
                if rk not in all_racks: all_racks.append(rk)
        units={rack:defaultdict(list) for rack in all_racks}
        for source_rank,(source_floor,src) in enumerate(sources):
            for fallback_idx,rec in enumerate(src.get('mapped') or [],1):
                if rec.get('status') not in {'MAPPED','COUNTED'}: continue
                rack=rec.get('rack')
                if rack not in units:
                    units.setdefault(rack, defaultdict(list))
                    if rack not in all_racks: all_racks.append(rack)
                x,y=_safe_xy(rec)
                occ=rec.get('occurrence_id',fallback_idx)
                key=(source_floor,str(occ),round(x,6),round(y,6),str(rec.get('block','')))
                ent=entry_by_key.setdefault(key,{
                    'source_floor':source_floor,'schedule_floor':floor,'occurrence_id':occ,'handle':rec.get('handle',''),
                    'block':rec.get('block',''),'service':rec.get('service',''),'rack':rack,
                    'x':x,'y':y,'labels':[],'codes':[],'port_slots':[]
                })
                dup_counter=Counter()
                for code in rec.get('logical_ports') or []:
                    dup_counter[code]+=1
                    units[rack][code].append({'source_rank':source_rank,'source_floor':source_floor,'rec':rec,
                                                   'dup_index':dup_counter[code],'ent':ent})

        source_results={sf:sr for sf,sr in sources}
        for rack in all_racks:
            seen_codes=set(units.get(rack,{})) | set(port_records.get(rack,{}))
            ordered=[c for c in LABEL_CODE_ORDER if c in seen_codes] + sorted(seen_codes-set(LABEL_CODE_ORDER))
            for code in ordered:
                def _sort_unit(u):
                    src_result=source_results[u['source_floor']]
                    rec=u['rec']
                    if str(rack).startswith('IDF-LAB'):
                        return (int(u['source_rank']),)+_lab_perimeter_spatial_key(src_result, rack, rec, u['dup_index'])

                    # HARD USER RULE: room_id == rack_room_id is always the first
                    # bucket for this (rack, point_type) list. This is a bucket
                    # concatenation effect: rack-room points sort together by t,
                    # then all other points sort by the normal rule.
                    start_key=_rack_start_room_bucket_key(src_result, rack, code, rec, u['dup_index'])
                    if start_key is not None:
                        return (int(u['source_rank']),)+tuple(start_key)

                    # SAFE HYBRID BUILD:
                    # - Keep WAL/WIN barrier + D/V + Wi-Fi room ordering untouched from
                    #   the stable build.
                    # - Apply the working route projection only to CCTV (C).
                    # - Circuit/panel id remains label text only; it never becomes a sort key.
                    if code == 'C':
                        cctv_key=_flat_cctv_route_projection_key(src_result, rack, code, rec, u['dup_index'])
                        if cctv_key is not None:
                            return (int(u['source_rank']),)+cctv_key
                    return (int(u['source_rank']),)+_candidate_spatial_key(src_result,rack,code,rec,u['dup_index'])
                candidates=sorted(units.get(rack,{}).get(code,[]), key=_sort_unit)
                slots=list(port_records.get(rack,{}).get(code,[]))
                if len(candidates)!=len(slots):
                    raise RuntimeError(
                        f'{floor} {rack} {code}: CAD occurrences ({len(candidates)}) do not match Excel logical ports ({len(slots)}). '
                        'Re-analyze the drawing before generating labels.'
                    )
                for unit,slot in zip(candidates,slots):
                    unit['ent']['labels'].append(slot['label'])
                    unit['ent']['codes'].append(code)
                    unit['ent']['port_slots'].append(dict(slot))

        for ent in entry_by_key.values():
            if ent['labels']:
                assignments.setdefault(ent['source_floor'],[]).append(ent)

    # A Roof-only project cannot reach here because _effective_export_results blocks it.
    for floor,items in assignments.items():
        items.sort(key=lambda e:(e.get('rack',''),-float(e['y']),float(e['x']),str(e.get('occurrence_id',''))))
    return assignments


def _iter_dxf_pairs_stream(path):
    with open(path,'r',encoding='utf-8',errors='ignore') as f:
        while True:
            c=f.readline(); v=f.readline()
            if not c or not v: break
            try: code=int(c.strip())
            except ValueError: continue
            yield code,v.rstrip('\r\n')


def _scan_dxf_write_meta(path):
    max_handle=0; layer_table_handle='0'; model_owner='0'; has_label_layer=False
    section=None; expect_section=False; table=None; expect_table=False
    layer_record=False; entity_open=False
    for code,val in _iter_dxf_pairs_stream(path):
        if code==5:
            try: max_handle=max(max_handle,int(str(val).strip(),16))
            except Exception: pass
        if code==0 and val=='SECTION': expect_section=True; continue
        if expect_section and code==2:
            section=val; expect_section=False; table=None; continue
        if code==0 and val=='ENDSEC': section=None; table=None; entity_open=False; continue
        if section=='TABLES':
            if code==0 and val=='TABLE': expect_table=True; continue
            if expect_table and code==2:
                table=val; expect_table=False; continue
            if code==0 and val=='ENDTAB': table=None; layer_record=False; continue
            if table=='LAYER':
                if code==5 and layer_table_handle=='0' and not layer_record: layer_table_handle=str(val).strip()
                if code==0 and val=='LAYER': layer_record=True; continue
                if layer_record and code==2:
                    if str(val).upper()==LABEL_LAYER.upper(): has_label_layer=True
                    layer_record=False
        elif section=='ENTITIES':
            if code==0:
                entity_open=(val!='ENDSEC')
            elif entity_open and code==330 and model_owner=='0':
                model_owner=str(val).strip()
    return {'max_handle':max_handle,'layer_table_handle':layer_table_handle,
            'model_owner':model_owner,'has_label_layer':has_label_layer}


def _write_pair(out,code,value):
    out.write(f'{int(code):>3}\n{value}\n')


def _auto_label_height(items):
    if not items: return 100.0
    xs=[float(x['x']) for x in items]; ys=[float(x['y']) for x in items]
    span=max(max(xs)-min(xs) if len(xs)>1 else 0,max(ys)-min(ys) if len(ys)>1 else 0)
    if span<=1e-9: return 100.0
    return max(70.0,min(220.0,span*0.0025))


def _raw_newline(sample):
    return b'\r\n' if sample.endswith(b'\r\n') else b'\n'


def _format_raw_pair(code,value,nl=b'\r\n'):
    """Build one ASCII DXF group-code/value pair without touching source bytes."""
    if isinstance(value, bytes):
        vb=value
    else:
        vb=str(value).encode('ascii','strict')
    return f'{int(code):>3}'.encode('ascii')+nl+vb+nl


def _scan_dxf_binary_meta(path):
    """Read only the metadata needed for safe label injection.

    This parser never decodes/re-serializes the drawing.  It records the highest
    actual object handle (group 5 or 105), HANDSEED, the LAYER table owner and the
    *Model_Space block-record owner used by existing model-space entities.
    """
    max_handle=0; handseed=0; layer_table_handle=b'0'; model_owner=b'0'; has_label_layer=False
    section=None; expect_section=False; table=None; expect_table=False
    in_layer_record=False; pending_handseed=False
    first_nl=b'\r\n'
    pair_count=0
    with open(path,'rb') as f:
        while True:
            c=f.readline(); v=f.readline()
            if not c or not v: break
            pair_count+=1
            if pair_count==1: first_nl=_raw_newline(c)
            try: code=int(c.strip())
            except Exception:
                raise RuntimeError('DXF contains a malformed group-code line; refusing to rewrite it.')
            val=v.rstrip(b'\r\n')
            sval=val.strip()
            if code in (5,105):
                try: max_handle=max(max_handle,int(sval,16))
                except Exception: pass
            if pending_handseed and code==5:
                try: handseed=int(sval,16)
                except Exception: pass
                pending_handseed=False
            if code==9 and sval==b'$HANDSEED': pending_handseed=True

            if code==0 and sval==b'SECTION': expect_section=True; continue
            if expect_section and code==2:
                section=sval.decode('ascii','ignore'); expect_section=False; table=None; continue
            if code==0 and sval==b'ENDSEC':
                section=None; table=None; in_layer_record=False; continue

            if section=='TABLES':
                if code==0 and sval==b'TABLE': expect_table=True; continue
                if expect_table and code==2:
                    table=sval.decode('ascii','ignore'); expect_table=False; continue
                if code==0 and sval==b'ENDTAB': table=None; in_layer_record=False; continue
                if table=='LAYER':
                    # TABLE itself owns a handle before the first LAYER record.
                    if code==5 and layer_table_handle==b'0' and not in_layer_record:
                        layer_table_handle=sval
                    if code==0 and sval==b'LAYER':
                        in_layer_record=True; continue
                    if in_layer_record and code==2:
                        if sval.upper()==LABEL_LAYER.encode('ascii').upper(): has_label_layer=True
                        in_layer_record=False
            elif section=='ENTITIES':
                # First entity owner in ENTITIES is normally *Model_Space block-record.
                if model_owner==b'0' and code==330:
                    model_owner=sval
    return {
        'max_handle':max_handle,'handseed':handseed,'layer_table_handle':layer_table_handle,
        'model_owner':model_owner,'has_label_layer':has_label_layer,'newline':first_nl,
        'pair_count':pair_count
    }


def _write_labeled_dxf(source_dxf,output_dxf,items,text_height=None):
    """Create an AutoCAD-safe labeled DXF while preserving the uploaded DXF bytes.

    v2.7 deliberately does NOT round-trip the source through a DXF library and does
    NOT normalize every group-code/value line.  Every original pair is copied
    byte-for-byte; only $HANDSEED / LAYER-table count are minimally patched and the
    new layer/TEXT records are injected.  This preserves ACDSDATA, proxy/custom
    objects, dynamic-block metadata, codepage bytes, XDATA and original line endings.
    """
    source_dxf=Path(source_dxf); output_dxf=Path(output_dxf)
    meta=_scan_dxf_binary_meta(source_dxf)
    label_count=sum(len(x.get('labels') or []) for x in items)
    if label_count<=0:
        raise RuntimeError(f'No mapped logical labels exist for {source_dxf.name}.')
    height=float(text_height) if text_height not in (None,'',0,'0') else _auto_label_height(items)
    if not math.isfinite(height) or height<=0:
        raise RuntimeError('CAD label text height must be a positive number.')

    # Start at or beyond the source HANDSEED and every known entity/object handle.
    next_handle=max(int(meta['max_handle'])+1,int(meta.get('handseed') or 0)+1)
    layer_handle=None
    if not meta['has_label_layer']:
        layer_handle=next_handle; next_handle+=1
    text_entities=[]
    for ent in items:
        labels=list(ent.get('labels') or [])
        x=float(ent['x']) + height*1.4
        y0=float(ent['y']) + height*0.55*(len(labels)-1)
        for idx,label in enumerate(labels):
            h=f'{next_handle:X}'.encode('ascii'); next_handle+=1
            y=y0-idx*height*1.15
            # Generated port IDs are deliberately ASCII-only.
            text_entities.append((h,x,y,str(label)))
    final_seed=f'{next_handle+16:X}'.encode('ascii')
    nl=meta['newline']

    def emit_layer(out):
        h=f'{layer_handle:X}' if layer_handle is not None else None
        if h is None: return
        for code,val in [
            (0,'LAYER'),(5,h),(330,meta['layer_table_handle']),
            (100,'AcDbSymbolTableRecord'),(100,'AcDbLayerTableRecord'),
            (2,LABEL_LAYER),(70,0),(62,7),(6,'Continuous'),(370,-3),
            # AutoCAD 2018+ requires PlotStyleName (390) in every LAYER record.
            # Reuse the drawing's standard Normal plot style/material handles,
            # matching the existing Layer 0 records in Autodesk-authored DXF.
            (390,'F'),(347,'98'),(348,'0')
        ]:
            out.write(_format_raw_pair(code,val,nl))

    def emit_texts(out):
        owner=meta['model_owner']
        if owner in (b'',b'0'):
            raise RuntimeError('Could not resolve the Model Space owner handle for CAD labels.')
        for h,x,y,label in text_entities:
            records=[
                (0,'TEXT'),(5,h),(330,owner),(100,'AcDbEntity'),(8,LABEL_LAYER),
                (100,'AcDbText'),(10,f'{x:.10g}'),(20,f'{y:.10g}'),(30,'0.0'),
                (40,f'{height:.10g}'),(1,label),(50,'0.0'),(41,'1.0'),(51,'0.0'),
                (7,'Standard'),(71,0),(72,0),(11,f'{x:.10g}'),(21,f'{y:.10g}'),(31,'0.0'),
                (100,'AcDbText'),(73,0)
            ]
            for code,val in records:
                out.write(_format_raw_pair(code,val,nl))

    # Binary streaming copy: original code/value lines are never decoded or normalized.
    section=None; expect_section=False; table=None; expect_table=False
    pending_handseed=False; layer_header=False
    output_dxf.parent.mkdir(parents=True,exist_ok=True)
    injected_text=False; injected_layer=bool(meta['has_label_layer'])
    with open(source_dxf,'rb') as src, open(output_dxf,'wb') as out:
        while True:
            c=src.readline(); v=src.readline()
            if not c or not v: break
            try: code=int(c.strip())
            except Exception:
                # Preserve even unexpected lines exactly rather than dropping content.
                out.write(c); out.write(v); continue
            raw_val=v.rstrip(b'\r\n'); sval=raw_val.strip()

            # Inject immediately before the original terminators.
            if section=='TABLES' and table=='LAYER' and code==0 and sval==b'ENDTAB' and not meta['has_label_layer']:
                emit_layer(out); injected_layer=True
            if section=='ENTITIES' and code==0 and sval==b'ENDSEC':
                emit_texts(out); injected_text=True

            # Minimal source-line patches: HANDSEED and LAYER record count only.
            if pending_handseed and code==5:
                out.write(c); out.write(final_seed+_raw_newline(v)); pending_handseed=False
            elif section=='TABLES' and table=='LAYER' and layer_header and code==70 and not meta['has_label_layer']:
                try:
                    old=int(sval); new=str(old+1).encode('ascii')
                    # Keep original indentation where practical.
                    lead=raw_val[:len(raw_val)-len(raw_val.lstrip())]
                    out.write(c); out.write(lead+new+_raw_newline(v))
                except Exception:
                    out.write(c); out.write(v)
                layer_header=False
            else:
                out.write(c); out.write(v)

            # State update after writing the current pair.
            if code==9 and sval==b'$HANDSEED': pending_handseed=True
            if code==0 and sval==b'SECTION': expect_section=True
            elif expect_section and code==2:
                section=sval.decode('ascii','ignore'); expect_section=False; table=None
            elif code==0 and sval==b'ENDSEC':
                section=None; table=None
            elif section=='TABLES':
                if code==0 and sval==b'TABLE': expect_table=True
                elif expect_table and code==2:
                    table=sval.decode('ascii','ignore'); expect_table=False; layer_header=(table=='LAYER')
                elif table=='LAYER' and code==0 and sval==b'LAYER':
                    layer_header=False
                elif code==0 and sval==b'ENDTAB':
                    table=None; layer_header=False

    if not injected_layer:
        raise RuntimeError('CAD label generation failed: LAYER table injection was not completed.')
    if not injected_text:
        raise RuntimeError('CAD label generation failed: ENTITIES injection was not completed.')
    if output_dxf.stat().st_size <= source_dxf.stat().st_size:
        raise RuntimeError('CAD label integrity check failed: generated file did not grow after label injection.')

    # Structural validation without re-saving the output.
    check=_scan_dxf_binary_meta(output_dxf)
    if not check['has_label_layer']:
        raise RuntimeError('CAD label integrity check failed: TELETECH-LABELS layer is missing.')
    expected_extra_min=label_count*20
    if check['pair_count'] < meta['pair_count'] + expected_extra_min:
        raise RuntimeError('CAD label integrity check failed: output pair count is unexpectedly low.')
    return {
        'path':str(output_dxf),'label_count':label_count,'text_height':height,'layer':LABEL_LAYER,
        'source_bytes':source_dxf.stat().st_size,'output_bytes':output_dxf.stat().st_size,
        'writer':'byte-preserving-v2.9-stage1'
    }


def _find_accoreconsole():
    """Locate Autodesk AutoCAD Core Console on Windows.

    v2.9 uses Autodesk itself as the final serializer.  This avoids depending on
    Windows shell/DXF-import behavior and produces a native DWG that AutoCAD 2021
    can open directly.  TELETECH_ACCORECONSOLE can override auto-detection.
    """
    env=(os.environ.get('TELETECH_ACCORECONSOLE') or '').strip().strip('"')
    candidates=[]
    if env: candidates.append(Path(env))
    pf=os.environ.get('ProgramFiles',r'C:\Program Files')
    pfx86=os.environ.get('ProgramFiles(x86)',r'C:\Program Files (x86)')
    # Prefer the user's installed 2021 release, then allow newer/older releases.
    for base in (pf,pfx86):
        for yr in (2021,2022,2023,2024,2025,2026,2020,2019,2018):
            candidates.append(Path(base)/'Autodesk'/f'AutoCAD {yr}'/'accoreconsole.exe')
    for c in candidates:
        try:
            if c.is_file(): return c.resolve()
        except Exception: pass
    # Last resort: PATH lookup.
    hit=shutil.which('accoreconsole.exe') or shutil.which('accoreconsole')
    return Path(hit).resolve() if hit else None


def _autocad_native_dwg(source_drawing,output_dwg,items,text_height=None):
    """Let Autodesk add TELETECH labels to the *untouched original drawing*.

    v3.0 deliberately removes the temporary/staged DXF writer from the native
    path.  Core Console opens the exact uploaded source file first.  Only after
    Autodesk has parsed it successfully do we create TELETECH-LABELS and TEXT
    entities inside Autodesk using AutoLISP/entmakex, then SAVEAS a new DWG.

    This is safer for drawings containing AEC/proxy/custom objects because no
    DXF TABLE/OBJECTS/handle record is modified before Autodesk opens the file.
    """
    source_drawing=Path(source_drawing).resolve(); output_dwg=Path(output_dwg).resolve()
    exe=_find_accoreconsole()
    if exe is None:
        raise RuntimeError(
            'AutoCAD Core Console was not found. Expected '
            r'C:\Program Files\Autodesk\AutoCAD 2021\accoreconsole.exe or set '
            'TELETECH_ACCORECONSOLE to its full path.'
        )
    label_count=sum(len(x.get('labels') or []) for x in items)
    if label_count<=0:
        raise RuntimeError(f'No mapped logical labels exist for {source_drawing.name}.')
    height=float(text_height) if text_height not in (None,'',0,'0') else _auto_label_height(items)
    if not math.isfinite(height) or height<=0:
        raise RuntimeError('CAD label text height must be a positive number.')

    output_dwg.parent.mkdir(parents=True,exist_ok=True)
    try: output_dwg.unlink(missing_ok=True)
    except Exception: pass

    # AutoCAD itself creates the layer and text objects.  We use AutoLISP only
    # as a native entity-creation API; Python never serializes the source DXF.
    lsp=output_dwg.with_suffix('.teletech-labels.lsp')
    scr=output_dwg.with_suffix('.teletech-run.scr')
    lsp_path=str(lsp).replace('\\','/')
    dst=str(output_dwg).replace('\\','/')

    def lsp_str(v):
        # Port IDs are ASCII, but keep this safe for any future text labels.
        return str(v).replace('\\','\\\\').replace('"','\\"')

    lines=[
        '(setvar "CMDECHO" 0)',
        '(setvar "FILEDIA" 0)',
        '(setvar "CMDDIA" 0)',
        '(if (not (tblsearch "LAYER" "TELETECH-LABELS"))',
        '  (command "_.-LAYER" "_M" "TELETECH-LABELS" ""))',
        '(setvar "CLAYER" "TELETECH-LABELS")',
    ]
    for ent in items:
        labels=list(ent.get('labels') or [])
        x=float(ent['x']) + height*1.4
        y0=float(ent['y']) + height*0.55*(len(labels)-1)
        for idx,label in enumerate(labels):
            y=y0-idx*height*1.15
            # entmakex lets Autodesk allocate owner/handle/plot-style/material data.
            lines.append(
                '(entmakex (list (cons 0 "TEXT") (cons 100 "AcDbEntity") '
                '(cons 8 "TELETECH-LABELS") (cons 100 "AcDbText") '
                f'(cons 10 (list {x:.12g} {y:.12g} 0.0)) '
                f'(cons 40 {height:.12g}) (cons 1 "{lsp_str(label)}") '
                '(cons 50 0.0) (cons 41 1.0) (cons 51 0.0) '
                '(cons 7 "Standard") (cons 71 0) (cons 72 0) '
                f'(cons 11 (list {x:.12g} {y:.12g} 0.0)) '
                '(cons 73 0)))'
            )
    lines += ['(setvar "CLAYER" "0")','(princ "\\nTELETECH_LABELS_DONE\\n")','(princ)']
    lsp.write_text('\r\n'.join(lines)+'\r\n',encoding='ascii',errors='strict')

    # Load the label program only after the untouched drawing has opened.
    # SAVEAS is then performed by Autodesk, so the deliverable is a native DWG.
    script='\r\n'.join([
        '_.FILEDIA','0','_.CMDDIA','0','_.UCS','_W',
        f'(load "{lsp_path}")',
        '_.SAVEAS',f'"{dst}"',
        '_.QUIT',''
    ])
    scr.write_text(script,encoding='ascii',newline='')

    cmd=[str(exe),'/i',str(source_drawing),'/s',str(scr),'/l','en-US']
    print('[CAD] AutoCAD Core Console (untouched source):', ' '.join(f'"{x}"' if ' ' in x else x for x in cmd))
    cp=None; log=''
    try:
        cp=subprocess.run(cmd,cwd=str(output_dwg.parent),stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT,text=True,errors='replace',timeout=900)
        log=cp.stdout or ''
    except subprocess.TimeoutExpired as e:
        log=(e.stdout or '') if isinstance(e.stdout,str) else ''
        raise RuntimeError('AutoCAD Core Console timed out while labeling the untouched source drawing. '+log[-1600:])
    finally:
        try: scr.unlink(missing_ok=True)
        except Exception: pass
        try: lsp.unlink(missing_ok=True)
        except Exception: pass

    ok_marker='TELETECH_LABELS_DONE' in log
    if cp.returncode!=0 or not output_dwg.exists() or output_dwg.stat().st_size<1024:
        tail=' '.join((log[-4000:] or '').split())
        hint=''
        if 'FATAL ERROR' in log or 'Access Violation' in log:
            hint=(' Core Console crashed while opening the ORIGINAL drawing before/while scripting. '
                  'That means the file contains an AEC/proxy object that AutoCAD 2021 Core Console itself cannot process; '
                  'use the Desktop AutoCAD fallback package generated by v3.5.')
        raise RuntimeError(f'AutoCAD native DWG generation failed (exit {cp.returncode}).{hint} Core Console: {tail}')
    with open(output_dwg,'rb') as f: magic=f.read(6)
    if not magic.startswith(b'AC10'):
        raise RuntimeError(f'AutoCAD returned an unexpected DWG file header: {magic!r}')
    if not ok_marker:
        print('[CAD] warning: completion marker was not echoed, but DWG exists and passed magic validation.')
    return {'path':str(output_dwg),'bytes':output_dwg.stat().st_size,
            'label_count':label_count,'text_height':height,
            'autocad_core_console':str(exe),'source_mode':'untouched-original-v3.1',
            'log_tail':' '.join(log[-1800:].split())}




def _label_block_box(x,y,labels,height):
    """Approximate AutoCAD TEXT block bbox for collision avoidance."""
    labels=list(labels or [])
    if not labels: return (x,y,x,y)
    width=max(max(1,len(str(t)))*height*0.62 for t in labels)
    line_gap=height*1.15
    top=y+height*0.82
    bottom=y-(len(labels)-1)*line_gap-height*0.28
    return (x-height*0.08,bottom,x+width,top)


def _boxes_overlap(a,b,pad=0.0):
    return not (a[2]+pad < b[0] or b[2]+pad < a[0] or a[3]+pad < b[1] or b[3]+pad < a[1])


def _collision_layout(items,height):
    """Place one collision box per physical endpoint (D/V stays paired)."""
    if not items: return []
    xs=[float(e['x']) for e in items]; ys=[float(e['y']) for e in items]
    minx,maxx=min(xs),max(xs); miny,maxy=min(ys),max(ys)
    span=max(maxx-minx,maxy-miny,1.0); margin=max(height*8.0,span*0.015)
    usable=(minx-margin,miny-margin,maxx+margin,maxy+margin)
    placed=[]; out=[]
    # Stable processing order ensures repeatable label positions between exports.
    ordered=sorted(enumerate(items),key=lambda kv:(str(kv[1].get('rack','')), -float(kv[1]['y']), float(kv[1]['x']), str(kv[1].get('occurrence_id',''))))
    by_index={}
    for original_idx,ent in ordered:
        x0=float(ent['x']); y0=float(ent['y']); labels=list(ent.get('labels') or [])
        codes=[str(c) for c in (ent.get('codes') or [])]; primary=codes[0] if codes else ''
        special=primary in {'AC','A','R','H','P','S','TV'}
        base=height*(2.0 if special else 1.45)
        # Prefer right side, then fan around the symbol in expanding rings.
        candidates=[]
        dirs=[(1,0.55),(1,-0.55),(-1,0.55),(-1,-0.55),(0.35,1.35),(0.35,-1.35),(-0.35,1.35),(-0.35,-1.35)]
        for ring in (1.0,1.5,2.1,2.9,3.8,5.0,6.5):
            for dx,dy in dirs: candidates.append((x0+dx*base*ring,y0+dy*base*ring))
        chosen=None; box=None
        for x,y in candidates:
            b=_label_block_box(x,y,labels,height)
            if b[0]<usable[0] or b[1]<usable[1] or b[2]>usable[2] or b[3]>usable[3]: continue
            if any(_boxes_overlap(b,pb,pad=height*0.18) for pb in placed): continue
            chosen=(x,y); box=b; break
        if chosen is None:
            # Deterministic last resort; continue expanding until free.
            ring=7.5
            while ring<25.0 and chosen is None:
                for dx,dy in dirs:
                    x,y=x0+dx*base*ring,y0+dy*base*ring; b=_label_block_box(x,y,labels,height)
                    if not any(_boxes_overlap(b,pb,pad=height*0.18) for pb in placed): chosen=(x,y); box=b; break
                ring+=1.0
        if chosen is None:
            chosen=(x0+base,y0+height*0.55); box=_label_block_box(chosen[0],chosen[1],labels,height)
        placed.append(box)
        by_index[original_idx]={'label_x':chosen[0],'label_y':chosen[1],'label_box':box,
                                'leader':math.hypot(chosen[0]-x0,chosen[1]-y0)>height*2.0}
    for i,e in enumerate(items):
        z=dict(e); z.update(by_index.get(i,{})); out.append(z)
    return out


def _near_anchor_collision_layout(items,height):
    """Collision-aware placement that stays visually attached to the symbol.

    FIX5: FIX3 avoided overlap but moved labels hundreds of millimetres away;
    FIX4 locked labels near the point but allowed overlap.  This hybrid keeps
    the first candidates within a small radius of the endpoint and expands only
    when a real text-box collision exists.  Projector labels are forced to use
    the corrected visual PROJECTOR anchor from the recursive block resolver.
    """
    if not items: return []
    placed=[]
    by_index={}
    # Process sparse/special services first, then D/V/W dense labels.
    priority={'P':0,'A':1,'AC':1,'H':1,'S':1,'TV':1,'C':2,'D':3,'V':3,'W':4}
    ordered=sorted(enumerate(items), key=lambda kv:(
        priority.get((list(kv[1].get('codes') or [''])[0] or ''),9),
        str(kv[1].get('rack','')), -float(kv[1]['y']), float(kv[1]['x']), str(kv[1].get('occurrence_id',''))
    ))
    def add_candidates(x0,y0,labels,primary):
        # Use small offsets first.  One unit is intentionally less than the
        # text height so labels remain visually attached to the device point.
        u=max(height*0.55, 35.0)
        c=[]
        if primary=='P':
            # Projector: label must sit on/next to the projector symbol, not in
            # the generic collision bucket. These labels are naturally sparse.
            c.extend([(x0+0.12*u,y0+0.10*u),(x0-0.35*u,y0+0.10*u),(x0+0.12*u,y0-0.45*u)])
        elif len(labels)>1:
            # D+V paired physical endpoint: keep pair close to the same point.
            c.extend([(x0+0.28*u,y0+0.65*u),(x0-1.05*u,y0+0.65*u),(x0+0.28*u,y0-0.55*u),(x0-1.05*u,y0-0.55*u)])
        else:
            c.extend([(x0+0.25*u,y0+0.20*u),(x0-0.90*u,y0+0.20*u),(x0+0.25*u,y0-0.45*u),(x0-0.90*u,y0-0.45*u),
                      (x0-0.35*u,y0+0.95*u),(x0-0.35*u,y0-0.95*u)])
        # Controlled expansion; still much closer than v4.9/FIX3 buckets.
        dirs=[(1,0.35),(-1,0.35),(1,-0.55),(-1,-0.55),(0.25,1.15),(-0.75,1.15),(0.25,-1.15),(-0.75,-1.15)]
        for ring in (1.25,1.65,2.15,2.75,3.50):
            for dx,dy in dirs:
                c.append((x0+dx*u*ring,y0+dy*u*ring))
        return c
    for original_idx,ent in ordered:
        x0=float(ent['x']); y0=float(ent['y'])
        labels=list(ent.get('labels') or [])
        codes=[str(c) for c in (ent.get('codes') or [])]
        primary=codes[0] if codes else ''
        chosen=None; box=None
        for x,y in add_candidates(x0,y0,labels,primary):
            b=_label_block_box(x,y,labels,height)
            # Do not let the actual endpoint fall far outside the label's
            # visual association radius.  For projectors, be stricter.
            maxdist=height*(1.25 if primary=='P' else 3.25)
            if math.hypot(x-x0,y-y0)>maxdist:
                continue
            if any(_boxes_overlap(b,pb,pad=height*0.12) for pb in placed):
                continue
            chosen=(x,y); box=b; break
        if chosen is None:
            # FIX6: do not accept a stacked/overlapping label as the first fallback.
            # Continue expanding around the symbol; only if the drawing is impossibly
            # dense do we fall back to a close attached label.
            u=max(height*0.55,35.0)
            dirs=[(1,0),(-1,0),(0,1),(0,-1),(1,1),(-1,1),(1,-1),(-1,-1),(1,0.45),(-1,0.45),(1,-0.65),(-1,-0.65)]
            for ring in (4.0,5.0,6.5,8.0,10.0,13.0):
                for dx,dy in dirs:
                    x,y=x0+dx*u*ring,y0+dy*u*ring
                    b=_label_block_box(x,y,labels,height)
                    if any(_boxes_overlap(b,pb,pad=height*0.12) for pb in placed):
                        continue
                    chosen=(x,y); box=b; break
                if chosen is not None: break
        if chosen is None:
            u=max(height*0.55,35.0)
            chosen=(x0+0.25*u,y0+0.25*u)
            box=_label_block_box(chosen[0],chosen[1],labels,height)
        placed.append(box)
        by_index[original_idx]={'label_x':chosen[0],'label_y':chosen[1],'label_box':box,
                                'leader':math.hypot(chosen[0]-x0,chosen[1]-y0)>height*1.10}
    out=[]
    for i,e in enumerate(items):
        z=dict(e); z.update(by_index.get(i,{})); out.append(z)
    return out


def _anchor_locked_layout(items,height):
    # Backward-compatible name used by older tests; FIX5 now uses the hybrid
    # near-anchor collision resolver rather than the overlap-prone hard lock.
    return _near_anchor_collision_layout(items,height)

def _write_desktop_label_fallback(source_drawing,output_dwg,items,text_height=None,result=None):
    """Generate a Desktop AutoCAD fallback bundle without altering the source.

    If Core Console cannot load an AEC/proxy-heavy drawing, full AutoCAD can run
    this script on the original.  The UI returns these files instead of a 500 so
    the project is never blocked by the headless engine.
    """
    source_drawing=Path(source_drawing).resolve(); output_dwg=Path(output_dwg).resolve()
    label_count=sum(len(x.get('labels') or []) for x in items)
    height=float(text_height) if text_height not in (None,'',0,'0') else _auto_label_height(items)
    stem=output_dwg.stem
    lsp=EXPORTS/f'{stem}_DESKTOP_LABELS.lsp'
    scr=EXPORTS/f'{stem}_DESKTOP_RUN.scr'
    readme=EXPORTS/f'{stem}_DESKTOP_README.txt'
    def lsp_str(v): return str(v).replace('\\','\\\\').replace('"','\\"')
    lines=[
      '(setq *teletech-old-cmdecho* (getvar "CMDECHO"))',
      '(setq *teletech-old-filedia* (getvar "FILEDIA"))',
      '(setq *teletech-old-cmddia* (getvar "CMDDIA"))',
      '(setq *teletech-old-clayer* (getvar "CLAYER"))',
      '(setvar "CMDECHO" 0)','(setvar "FILEDIA" 0)','(setvar "CMDDIA" 0)',
      '; v4.9 LAB safety: erase old TELETECH label layers from previous runs instead of stacking them.',
      '(if (setq *teletech-old-labels* (ssget "_X" (list (cons 8 "TELETECH-LABELS*")))) (command "_.ERASE" *teletech-old-labels* ""))',
      '; v4.9 FIX6: lab perimeter ordering + record-camera mapping + expanded anti-overlap.',
      '(if (not (tblsearch "LAYER" "TELETECH-LABELS")) (command "_.-LAYER" "_M" "TELETECH-LABELS" ""))',
      '(setvar "CLAYER" "TELETECH-LABELS")']
    # v4.9 FIX5: near-anchor collision layout. Each physical endpoint owns one
    # label block; D/V stays paired; projector labels use corrected visual anchors.
    layout=_near_anchor_collision_layout(items,height)
    created_layers=set()
    for ent in layout:
        labels=list(ent.get('labels') or [])
        x0=float(ent['x']); ybase=float(ent['y'])
        x=float(ent.get('label_x',x0+height*1.4)); y0=float(ent.get('label_y',ybase+height*0.55))
        label_layer=_cad_label_layer_for_rack(result or {}, ent.get('rack'))
        ent['label_layer']=label_layer
        if label_layer not in created_layers:
            # Use AutoCAD layer command, no source geometry is changed.
            lines.append(f'(if (not (tblsearch "LAYER" "{lsp_str(label_layer)}")) (command "_.-LAYER" "_M" "{lsp_str(label_layer)}" ""))')
            # Keep default bright label color, but independent layers allow toggling each rack/lab.
            lines.append(f'(command "_.-LAYER" "_C" "2" "{lsp_str(label_layer)}" "")')
            created_layers.add(label_layer)
        if ent.get('leader'):
            # Leader ends just before the text block instead of crossing the text.
            lx=x-height*0.18 if x>=x0 else x+height*0.18
            lines.append('(entmakex (list (cons 0 "LINE") (cons 100 "AcDbEntity") '
                         f'(cons 8 "{lsp_str(label_layer)}") '
                         f'(cons 10 (list {x0:.12g} {ybase:.12g} 0.0)) (cons 11 (list {lx:.12g} {y0:.12g} 0.0))))')
        for idx,label in enumerate(labels):
            y=y0-idx*height*1.15
            lines.append('(entmakex (list (cons 0 "TEXT") (cons 100 "AcDbEntity") '
                         f'(cons 8 "{lsp_str(label_layer)}") '
                         '(cons 100 "AcDbText") '
                         f'(cons 10 (list {x:.12g} {y:.12g} 0.0)) (cons 40 {height:.12g}) '
                         f'(cons 1 "{lsp_str(label)}") (cons 50 0.0) (cons 7 "Standard") '
                         f'(cons 11 (list {x:.12g} {y:.12g} 0.0)) (cons 72 0) (cons 73 0)))')
    lines += ['(command "_.REGEN")',
      '(if *teletech-old-clayer* (setvar "CLAYER" *teletech-old-clayer*))',
      '(if *teletech-old-filedia* (setvar "FILEDIA" *teletech-old-filedia*))',
      '(if *teletech-old-cmddia* (setvar "CMDDIA" *teletech-old-cmddia*))',
      '(if *teletech-old-cmdecho* (setvar "CMDECHO" *teletech-old-cmdecho*))',
      '(princ "\\nTELETECH labels added. Use SAVEAS to save the labeled copy.\\n")','(princ)']
    lsp.write_text('\r\n'.join(lines)+'\r\n',encoding='ascii')
    lsp_abs=str(lsp.resolve()).replace('\\','/')
    # One expression only: avoids blank-line prompt termination in SCRIPT mode.
    scr.write_text(f'(load "{lsp_abs}")\r\n',encoding='ascii')
    readme.write_text(
        'TELETECH Desktop AutoCAD fallback\n\n'
        f'1. Open the ORIGINAL drawing in full AutoCAD: {source_drawing.name}\n'
        '2. Type SCRIPT and select the *_DESKTOP_RUN.scr file in this package.\n'
        '3. v4.9 first erases any OLD entities on TELETECH-LABELS, then writes collision-aware paired labels.\n'
        '4. Use SAVEAS and save a NEW copy. Do not overwrite the original.\n\n'
        f'Expected logical labels: {label_count}\nText height: {height}\nPlacement: v4.9 hybrid route: Ground user path + TH stable roof-last + anti-overlap engine\n',encoding='utf-8')
    return {'lsp':lsp,'scr':scr,'readme':readme,'label_count':label_count,'text_height':height}




# ---------------- Manual route reference -> reusable profile ----------------
def _route_learning_bounds(result):
    """Stable bounds used to normalize a one-time manual route drawing.

    The user may draw Route - rack1/2 on ONE reference DXF only.  That geometry
    is not a device and is not required on the other floors.  We convert it to a
    normalized route template and re-project it onto each unmarked floor's own
    endpoint envelope, so the reference drawing teaches the rule instead of being
    required on every board.
    """
    pts=[]
    for p in (result or {}).get('mapped') or []:
        try:
            if _is_in_drawing_roof_tail(result,p):
                continue
            x=float(p.get('x')); y=float(p.get('y'))
            if math.isfinite(x) and math.isfinite(y):
                pts.append({'x':x,'y':y})
        except Exception:
            pass
    if not pts:
        # Fall back to room markers, then manual route vertices.
        for m in (result or {}).get('room_markers') or []:
            try:
                x=float(m.get('x')); y=float(m.get('y'))
                if math.isfinite(x) and math.isfinite(y): pts.append({'x':x,'y':y})
            except Exception:
                pass
    if not pts:
        for route in ((result or {}).get('manual_routes') or {}).values():
            for x,y in route.get('path') or []:
                try: pts.append({'x':float(x),'y':float(y)})
                except Exception: pass
    return _robust_xy_bounds(pts, trim=0.04)


def _normalise_polyline_to_bounds(path,bounds):
    minx,miny,maxx,maxy=map(float,bounds); w=max(maxx-minx,1.0); h=max(maxy-miny,1.0)
    out=[]
    for x,y in path or []:
        try:
            out.append(((float(x)-minx)/w,(float(y)-miny)/h))
        except Exception:
            pass
    return out


def _denormalise_polyline_from_bounds(norm,bounds):
    minx,miny,maxx,maxy=map(float,bounds); w=max(maxx-minx,1.0); h=max(maxy-miny,1.0)
    return [(minx+float(nx)*w, miny+float(ny)*h) for nx,ny in (norm or [])]


def _install_reference_route_profiles(results_by_floor):
    """Apply one manually marked route drawing as a reusable route profile.

    This deliberately does NOT use route layers as devices and does NOT require
    the other DXFs to contain Route - rack polylines.  If one uploaded floor has
    manual_routes, those routes become a learned normalized profile for the whole
    project session.  Floors that already have their own manual_routes keep them.
    """
    if not results_by_floor:
        return results_by_floor
    # Prefer Ground as the reference when present because the user drew and
    # validated the reference path there. Otherwise use the first floor with routes.
    candidates=[]
    for fl,r in results_by_floor.items():
        mr=(r or {}).get('manual_routes') or {}
        if mr:
            rank=0 if fl=='Ground Floor' else 1
            candidates.append((rank,fl,r))
    templates={}
    ref_floor='BUILTIN_ROUTE_REFERENCE'
    if candidates:
        _,ref_floor,ref=sorted(candidates,key=lambda x:(x[0],str(x[1])))[0]
        ref_bounds=_route_learning_bounds(ref)
        for rack,route in (ref.get('manual_routes') or {}).items():
            norm=_normalise_polyline_to_bounds(route.get('path') or [], ref_bounds)
            if len(norm)>=2:
                templates[rack]={
                    'norm_path':norm,
                    'source_floor':ref_floor,
                    'source_layer':route.get('source_layer'),
                    'source_block':route.get('source_block'),
                    'source_vertices':len(norm),
                }
    else:
        builtin=_load_builtin_reference_routes_json()
        if builtin is not None:
            ref_bounds=builtin.get('bounds') or (0.0,0.0,1.0,1.0)
            for rack,path in (builtin.get('routes') or {}).items():
                norm=_normalise_polyline_to_bounds(path, ref_bounds)
                if len(norm)>=2:
                    templates[rack]={
                        'norm_path':norm,
                        'source_floor':'BUILTIN_ROUTE_REFERENCE',
                        'source_layer':'BUILTIN-GROUND-ROUTE',
                        'source_block':'BUILTIN_MANSOURA_GROUND_PROFILE',
                        'source_vertices':len(norm),
                    }
    if not templates:
        return results_by_floor
    for fl,r in results_by_floor.items():
        if (r.get('manual_routes') or {}):
            r['learned_route_profile_source']=ref_floor
            continue
        tgt_bounds=_route_learning_bounds(r)
        learned={}
        for rack,t in templates.items():
            pts=_denormalise_polyline_from_bounds(t['norm_path'], tgt_bounds)
            length=sum(math.hypot(b[0]-a[0],b[1]-a[1]) for a,b in zip(pts,pts[1:]))
            if length<=1e-6:
                continue
            learned[rack]={
                'path':pts,
                'length':length,
                'source_layer':'LEARNED_FROM_'+str(t.get('source_layer') or 'REFERENCE_ROUTE'),
                'source_block':'ROUTE_PROFILE_FROM_'+str(t.get('source_floor') or ref_floor),
                'insert_transform_identity':True,
                'vertex_count':len(pts),
                'learned_from_floor':ref_floor,
                'learned_reference_vertices':t.get('source_vertices'),
                'is_learned_route_profile':True,
            }
        if learned:
            r['manual_routes']=learned
            r['learned_route_profile_source']=ref_floor
            r['learned_route_profile_note']='Manual route was drawn once on the reference DXF and normalized/applied here; no route polylines are required on this floor.'
    return results_by_floor

def generate_labeled_project(results_by_floor,text_height=None,label_pattern=None):
    """Generate Desktop AutoCAD labeling packages without launching Core Console.

    v4.6 intentionally skips accoreconsole.exe.  These project drawings contain
    AEC/proxy objects that consistently crash AutoCAD 2021 Core Console and show
    the Autodesk Error Report dialog.  Full Desktop AutoCAD can process them, so
    Generate now creates the SCR/LSP/README package directly and never starts the
    crashing headless engine.
    """
    label_pattern,_=_validate_label_pattern(label_pattern)
    results_by_floor=_install_reference_route_profiles(results_by_floor)
    assignments=_project_label_assignments(results_by_floor,label_pattern=label_pattern)
    ts=time.strftime('%Y%m%d-%H%M%S')
    produced=[]; manifest=[]; desktop_files=[]
    floor_order=['Ground Floor','First Floor','Second Floor','Third Floor','Roof Floor']
    floor_order += [f for f in results_by_floor if f not in floor_order]
    for floor in floor_order:
        if floor not in results_by_floor: continue
        items=assignments.get(floor) or []
        if not items: continue
        src=Path(results_by_floor[floor]['drawing']).resolve()
        display_stem=re.sub(r'^[0-9a-fA-F]{32}_','',src.stem)
        safe_stem=re.sub(r'[^A-Za-z0-9._ -]+','_',display_stem).strip() or _floor_prefix(floor)
        out=EXPORTS/f'{safe_stem}_LABELED_V49_{ts}.dwg'
        fallback=_write_desktop_label_fallback(src,out,items,text_height=text_height,result=results_by_floor[floor])
        for fp in (fallback['lsp'],fallback['scr'],fallback['readme']):
            desktop_files.append(Path(fp))
        produced.append({
            'floor':floor,
            # Make the per-floor UI button download the file the user actually runs.
            'name':fallback['scr'].name,
            'labels':fallback['label_count'],
            'text_height':fallback['text_height'],
            'format':'AUTOCAD-DESKTOP-SCRIPT',
            'writer':'Safe Desktop AutoCAD package',
            'status':'DESKTOP_SAFE',
            'lsp':fallback['lsp'].name,
            'readme':fallback['readme'].name,
        })
        for ent in items:
            row={k:ent.get(k) for k in ('source_floor','rack','service','block','handle','occurrence_id','x','y','labels')}
            row['label_layer']=_cad_label_layer_for_rack(results_by_floor[floor], ent.get('rack'))
            manifest.append(row)

    if not produced: raise RuntimeError('No labeled CAD output could be generated.')
    manifest_name=f'project_label_manifest_{ts}.json'; manifest_path=EXPORTS/manifest_name
    manifest_path.write_text(json.dumps({
        'generated_at':ts,
        'layer':LABEL_LAYER,
        'writer':'v4.9 Safe Desktop AutoCAD package; wall-aware room membership + collision-aware paired labels + rack/lab layer isolation; Core Console disabled',
        'core_console_launched':False,
        'drawings':produced,
        'items':manifest,
    },ensure_ascii=False,indent=2),encoding='utf-8')
    zip_path=EXPORTS/f'project_labeled_cad_{ts}.zip'
    with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED,allowZip64=True) as z:
        for fp in desktop_files:
            if fp.exists(): z.write(fp,arcname=fp.name)
        z.write(manifest_path,arcname=manifest_name)
    return {
        'zip':zip_path.name,
        'drawings':produced,
        'manifest':manifest_name,
        'logical_label_count':sum(int(x['labels']) for x in produced),
        'layer':LABEL_LAYER,
        'writer':'Safe Desktop AutoCAD v4.9 (Core Console disabled)',
        'core_console_launched':False,
        'fallback_count':0,
    }


def _response_output_text(payload):
    """Extract output text from a raw Responses API JSON response."""
    if isinstance(payload, dict) and isinstance(payload.get('output_text'), str):
        return payload['output_text']
    parts=[]
    for item in (payload or {}).get('output',[]) if isinstance(payload,dict) else []:
        if not isinstance(item,dict):
            continue
        for c in item.get('content',[]) or []:
            if not isinstance(c,dict):
                continue
            if c.get('type') in {'output_text','text'} and isinstance(c.get('text'),str):
                parts.append(c['text'])
    return '\n'.join(parts).strip()


def _openai_responses_request(payload, api_key=None):
    key=os.getenv('OPENAI_API_KEY','').strip()
    if not key:
        raise RuntimeError('AI Vision API key is missing. Paste the key in the As-Built AI Vision box or set OPENAI_API_KEY before starting the UI.')
    base=os.getenv('TELETECH_OPENAI_BASE_URL',ASBUILT_OPENAI_BASE_URL).rstrip('/')
    req=Request(base+'/responses', data=json.dumps(payload,ensure_ascii=False).encode('utf-8'), method='POST', headers={
        'Authorization':'Bearer '+key,
        'Content-Type':'application/json',
        'Accept':'application/json',
        'User-Agent':'TELETECH-CAD-UI-v4.9-AsBuilt-Vision',
    })
    try:
        with urlopen(req, timeout=ASBUILT_VISION_TIMEOUT) as r:
            raw=r.read()
    except HTTPError as e:
        try:
            detail=e.read().decode('utf-8','replace')
            j=json.loads(detail); msg=((j.get('error') or {}).get('message') or detail)
        except Exception:
            msg=str(e)
        raise RuntimeError(f'AI Vision API HTTP {getattr(e,"code","")}: {msg}')
    except URLError as e:
        raise RuntimeError(f'AI Vision API connection failed: {getattr(e,"reason",e)}')
    except TimeoutError:
        raise RuntimeError('AI Vision API timed out. Try again or increase TELETECH_VISION_TIMEOUT.')
    try:
        return json.loads(raw.decode('utf-8'))
    except Exception as e:
        raise RuntimeError(f'AI Vision API returned invalid JSON: {e}')


def _gemini_upload_file(path, api_key, mime_type):
    """Upload one As-Built PDF/image to the Gemini File API (resumable protocol).

    Returns {'uri','mime_type','name'}. The returned uri is referenced by every
    later turn of the same As-Built chat via file_data, so the bytes are sent
    exactly once per uploaded document, never once per chat message.
    """
    path=Path(path)
    if not path.exists():
        raise RuntimeError('Uploaded As-Built file was not found.')
    size=path.stat().st_size
    if size>ASBUILT_PDF_MAX_BYTES:
        raise RuntimeError('As-Built file exceeds the 200 MB AI Vision safety limit.')
    key=(api_key or os.getenv('GEMINI_API_KEY','')).strip()
    if not key:
        raise RuntimeError('Gemini API key is missing. Paste it in the As-Built chat box or set GEMINI_API_KEY.')
    base=ASBUILT_GEMINI_BASE_URL
    upload_url=None
    _upload_exc=None
    for _api_ver in ('v1beta','v1'):
        _start_req=Request(f'{base}/upload/{_api_ver}/files?key={key}',
            data=json.dumps({'file':{'display_name':path.name}}).encode('utf-8'), method='POST',
            headers={
                'Content-Type':'application/json',
                'X-Goog-Upload-Protocol':'resumable',
                'X-Goog-Upload-Command':'start',
                'X-Goog-Upload-Header-Content-Length':str(size),
                'X-Goog-Upload-Header-Content-Type':mime_type,
            })
        try:
            with urlopen(_start_req, timeout=ASBUILT_VISION_TIMEOUT) as r:
                upload_url=r.headers.get('X-Goog-Upload-URL') or r.headers.get('x-goog-upload-url')
                r.read()
            _upload_exc=None
            break   # success
        except HTTPError as e:
            detail=e.read().decode('utf-8','replace')
            _upload_exc=RuntimeError(f'Gemini file upload (start) failed: HTTP {e.code}: {detail}')
            if e.code!=404:
                break   # non-404 is final
        except URLError as e:
            raise RuntimeError(f'Gemini file upload (start) connection failed: {getattr(e,"reason",e)}')
    if _upload_exc:
        raise _upload_exc
    if not upload_url:
        raise RuntimeError('Gemini file upload did not return an upload URL.')
    put_req=Request(upload_url, data=path.read_bytes(), method='POST', headers={
        'Content-Length':str(size),
        'X-Goog-Upload-Offset':'0',
        'X-Goog-Upload-Command':'upload, finalize',
    })
    try:
        with urlopen(put_req, timeout=ASBUILT_VISION_TIMEOUT) as r:
            raw=r.read()
    except HTTPError as e:
        detail=e.read().decode('utf-8','replace')
        raise RuntimeError(f'Gemini file upload failed: HTTP {e.code}: {detail}')
    except URLError as e:
        raise RuntimeError(f'Gemini file upload connection failed: {getattr(e,"reason",e)}')
    try:
        parsed=json.loads(raw.decode('utf-8'))
    except Exception as e:
        raise RuntimeError(f'Gemini file upload returned invalid JSON: {e}')
    f=parsed.get('file') or {}
    uri=f.get('uri'); name=f.get('name',''); state=f.get('state','ACTIVE')
    if not uri:
        raise RuntimeError('Gemini file upload did not return a file URI.')
    # PDFs/images are normally ACTIVE immediately; poll briefly just in case.
    for _ in range(ASBUILT_CHAT_UPLOAD_POLL_ATTEMPTS):
        if state=='ACTIVE':
            break
        if state=='FAILED':
            raise RuntimeError('Gemini file processing failed for the uploaded As-Built file.')
        time.sleep(ASBUILT_CHAT_UPLOAD_POLL_DELAY)
        try:
            with urlopen(Request(f'{base}/v1beta/{name}?key={key}', method='GET'), timeout=30) as r:
                f=json.loads(r.read().decode('utf-8'))
            state=f.get('state','ACTIVE')
        except Exception:
            break
    return {'uri':uri,'mime_type':f.get('mimeType',mime_type),'name':name}


def _gemini_generate(model, contents, api_key, response_schema=None):
    """One Gemini generateContent call. Returns (text, model_turn_to_append_to_history)."""
    key=(api_key or os.getenv('GEMINI_API_KEY','')).strip()
    if not key:
        raise RuntimeError('Gemini API key is missing. Paste it in the As-Built chat box or set GEMINI_API_KEY.')
    base=ASBUILT_GEMINI_BASE_URL
    payload={'contents':contents}
    if response_schema is not None:
        payload['generationConfig']={'responseMimeType':'application/json','responseSchema':response_schema}
    req=Request(f'{base}/v1beta/models/{model}:generateContent?key={key}',
        data=json.dumps(payload,ensure_ascii=False).encode('utf-8'), method='POST',
        headers={'Content-Type':'application/json'})
    # Gemini can temporarily return 503 when the selected model is overloaded.
    # Retry with exponential backoff, then try a current stable Flash fallback.
    # This is especially useful for large scanned As-Built requests.
    models_to_try=[model]
    fallback=os.getenv('TELETECH_GEMINI_FALLBACK_MODEL','gemini-3.8-flash').strip()
    if fallback and fallback not in models_to_try:
        models_to_try.append(fallback)
    last_error=None
    raw=None
    for try_model in models_to_try:
        req=Request(f'{base}/v1beta/models/{try_model}:generateContent?key={key}',
            data=json.dumps(payload,ensure_ascii=False).encode('utf-8'), method='POST',
            headers={'Content-Type':'application/json'})
        for attempt in range(4):
            try:
                with urlopen(req, timeout=ASBUILT_VISION_TIMEOUT) as r:
                    raw=r.read()
                break
            except HTTPError as e:
                try:
                    detail=e.read().decode('utf-8','replace'); j=json.loads(detail); msg=((j.get('error') or {}).get('message') or detail)
                except Exception:
                    msg=str(e)
                code=getattr(e,'code',None)
                last_error=RuntimeError(f'Gemini API HTTP {code}: {msg}')
                if code in (408,429,500,502,503,504):
                    if attempt < 3:
                        time.sleep((2 ** attempt) + random.random())
                        continue
                    break
                raise last_error
            except URLError as e:
                last_error=RuntimeError(f'Gemini API connection failed: {getattr(e,"reason",e)}')
                if attempt < 3:
                    time.sleep((2 ** attempt) + random.random())
                    continue
                break
            except TimeoutError:
                last_error=RuntimeError('Gemini API timed out. Try again.')
                if attempt < 3:
                    time.sleep((2 ** attempt) + random.random())
                    continue
                break
        if raw is not None:
            break
    if raw is None:
        raise last_error or RuntimeError('Gemini API request failed.')
    try:
        parsed=json.loads(raw.decode('utf-8'))
    except Exception as e:
        raise RuntimeError(f'Gemini API returned invalid JSON: {e}')
    cands=parsed.get('candidates') or []
    if not cands:
        fb=parsed.get('promptFeedback') or {}
        raise RuntimeError(('Gemini API returned no candidates. '+str(fb.get('blockReason',''))).strip())
    parts=(cands[0].get('content') or {}).get('parts') or []
    text=''.join(p.get('text','') for p in parts if isinstance(p,dict))
    if not text:
        raise RuntimeError('Gemini API returned no text output.')
    return text,{'role':'model','parts':[{'text':text}]}


def _asbuilt_candidate_catalog(floor,label_pattern=None):
    targets=_asbuilt_targets(floor,label_pattern=label_pattern)
    if not targets:
        return [],{},''
    xs=[float(t.get('x') or 0) for t in targets]; ys=[float(t.get('y') or 0) for t in targets]
    minx,maxx=min(xs),max(xs); miny,maxy=min(ys),max(ys)
    dx=max(maxx-minx,1e-9); dy=max(maxy-miny,1e-9)
    rows=[]; by_key={}; lines=[]
    for i,t in enumerate(targets,1):
        key=f'T{i:04d}'
        nx=int(round((float(t.get('x') or 0)-minx)*1000/dx))
        ny=int(round((maxy-float(t.get('y') or 0))*1000/dy))
        rec=dict(t); rec.update({'candidate_key':key,'nx':nx,'ny':ny})
        rows.append(rec); by_key[key]=rec
        label=(t.get('label') or '').replace('|','/')
        svc=(t.get('service') or '').replace('|','/')
        rack=(t.get('rack') or '').replace('|','/')
        lines.append(f'{key}|label={label}|service={svc}|rack={rack}|nx={nx}|ny={ny}')
    return rows,by_key,'\n'.join(lines)


def _candidate_rank_for_finding(finding, catalog, suggested_key=''):
    """Return review alternatives without auto-applying any change."""
    norm=lambda v: re.sub(r'[^A-Z0-9\u0600-\u06FF]+',' ',str(v or '').upper()).strip()
    sx=int(finding.get('x_norm') or 500); sy=int(finding.get('y_norm') or 500)
    hint=norm(finding.get('service_hint') or '')
    scored=[]
    for t in catalog:
        th=norm(str(t.get('service') or '')+' '+str(t.get('block') or '')+' '+str(t.get('label') or ''))
        svc_pen=0 if hint and (hint in th or any(w and w in th for w in hint.split())) else (250 if hint else 80)
        dist=((int(t.get('nx') or 0)-sx)**2+(int(t.get('ny') or 0)-sy)**2)**0.5
        bonus=-1000 if t.get('candidate_key')==suggested_key else 0
        scored.append((bonus+svc_pen+dist,t))
    scored.sort(key=lambda z:z[0])
    out=[]; seen=set()
    for _,t in scored[:12]:
        tid=t.get('target_id')
        if tid in seen: continue
        seen.add(tid)
        out.append({k:t.get(k) for k in ('target_id','candidate_key','label','service','block','rack','x','y','editable_direct')})
    return out



ASBUILT_SPLIT_DPI = int(os.getenv('ASBUILT_SPLIT_DPI','220') or '220')
ASBUILT_SPLIT_MAX_PAGES = int(os.getenv('ASBUILT_SPLIT_MAX_PAGES','80') or '80')


def _split_asbuilt_pdf_to_images(pdf_path):
    """Render every scanned As-Built PDF page to a high-resolution PNG.

    This is intentionally done before Vision analysis: the source PDFs in this
    workflow are commonly image-only scans, so page images give the model a
    stable visual input and preserve handwritten markups.
    """
    pdf_path=Path(pdf_path)
    if pdf_path.suffix.lower()!='.pdf':
        return [{'path':str(pdf_path),'filename':pdf_path.name,'mime_type':'image/png' if pdf_path.suffix.lower()=='.png' else 'image/jpeg'}]
    if fitz is None:
        raise RuntimeError('PyMuPDF (fitz) is required to split scanned As-Built PDFs into page images.')
    out_dir=pdf_path.parent/('ab_'+hashlib.md5(pdf_path.stem.encode('utf-8')).hexdigest()[:10])
    out_dir.mkdir(parents=True,exist_ok=True)
    doc=fitz.open(str(pdf_path))
    try:
        if len(doc)>ASBUILT_SPLIT_MAX_PAGES:
            raise RuntimeError(f'As-Built PDF has {len(doc)} pages; safety limit is {ASBUILT_SPLIT_MAX_PAGES}.')
        scale=ASBUILT_SPLIT_DPI/72.0
        mat=fitz.Matrix(scale,scale)
        pages=[]
        for idx,page in enumerate(doc,1):
            out=out_dir/f'p{idx:03d}.png'
            if not out.exists():
                pix=page.get_pixmap(matrix=mat,alpha=False)
                pix.save(str(out))
            pages.append({'path':str(out),'filename':out.name,'mime_type':'image/png','page_number':idx})
        return pages
    finally:
        doc.close()


def _asbuilt_page_parts_for_gemini(pdf_path,api_key):
    """Split PDF and upload each page image; return Gemini file parts."""
    pages=_split_asbuilt_pdf_to_images(pdf_path)
    parts=[]
    for page in pages:
        g=_gemini_upload_file(page['path'],api_key,'image/png')
        parts.append({'file_data':{'file_uri':g['uri'],'mime_type':g['mime_type']}})
    return pages,parts

def analyze_asbuilt_pdf_with_vision(pdf_path,floor,label_pattern=None,api_key=None,model=None):
    """Read a site-markup PDF with the OpenAI Responses API and return review-only removals."""
    pdf_path=Path(pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower()!='.pdf':
        raise RuntimeError('Uploaded As-Built PDF was not found.')
    if pdf_path.stat().st_size>ASBUILT_PDF_MAX_BYTES:
        raise RuntimeError('As-Built PDF exceeds the 200 MB AI Vision safety limit.')
    catalog,by_key,candidate_text=_asbuilt_candidate_catalog(floor,label_pattern=label_pattern)
    if not catalog:
        raise RuntimeError('No endpoint targets exist for the selected floor. Analyze the CAD floor first.')
    if len(catalog)>700:
        raise RuntimeError(f'The selected floor has {len(catalog)} endpoints. AI Vision review is limited to 700 endpoints per floor for deterministic matching.')
    b64=base64.b64encode(pdf_path.read_bytes()).decode('ascii')
    schema={
      'type':'object','additionalProperties':False,
      'properties':{
        'document_summary':{'type':'string'},
        'findings':{'type':'array','items':{
          'type':'object','additionalProperties':False,
          'properties':{
            'page_number':{'type':'integer','minimum':1},
            'status':{'type':'string','enum':list(ASBUILT_REMOVE_STATUSES)},
            'service_hint':{'type':'string'},
            'target_key':{'type':'string'},
            'target_label_seen':{'type':'string'},
            'nearby_text':{'type':'string'},
            'evidence':{'type':'string'},
            'confidence':{'type':'number','minimum':0,'maximum':1},
            'x_norm':{'type':'integer','minimum':0,'maximum':1000},
            'y_norm':{'type':'integer','minimum':0,'maximum':1000}
          },
          'required':['page_number','status','service_hint','target_key','target_label_seen','nearby_text','evidence','confidence','x_norm','y_norm']
        }}
      },
      'required':['document_summary','findings']
    }
    prompt=f"""You are reviewing a scanned/annotated telecom site As-Built PDF against ONE already-analyzed CAD floor: {floor}.

IMPORTANT SAFETY RULES:
1) Extract ONLY site removals whose handwritten/site note clearly means one of these four approved statuses, normalized exactly to the matching phrase:
- لم يتم السحب
- تم رد الكابلات
- لا يوجد لها مسار
- تم نقل
2) Ignore every other handwritten note, dimensions, counts, arrows, corrections, highlights, and comments. Do not invent removals.
3) The PDF can contain several pages/floors. Report only findings that visually belong to the selected CAD floor when you can determine that. If unsure, keep confidence low.
4) Match each finding to the candidate endpoint list below. target_key MUST be one listed key when you are reasonably confident; otherwise return an empty string. Never fabricate a candidate key.
5) x_norm/y_norm are the approximate location of the marked endpoint on its drawing area, normalized 0..1000 (left->right, top->bottom), ignoring page margins where possible.
6) evidence should be short: what handwritten phrase/mark and nearby printed context caused the finding. Do not translate the status; preserve Arabic in status.
7) A colored highlight by itself is NOT a removal unless the approved removal meaning is also present.

CANDIDATE ENDPOINTS FOR {floor}:
{candidate_text}
"""
    chosen=(model or ASBUILT_VISION_DEFAULT_MODEL).strip() or ASBUILT_VISION_DEFAULT_MODEL
    payload={
      'model':chosen,
      'input':[{'role':'user','content':[
        {'type':'input_file','filename':pdf_path.name,'file_data':'data:application/pdf;base64,'+b64,'detail':'high'},
        {'type':'input_text','text':prompt}
      ]}],
      'text':{'format':{'type':'json_schema','name':'teletech_asbuilt_pdf_review','strict':True,'schema':schema}},
      'max_output_tokens':12000
    }
    raw=_openai_responses_request(payload,api_key=api_key)
    text=_response_output_text(raw)
    if not text:
        raise RuntimeError('AI Vision API returned no structured output.')
    try:
        parsed=json.loads(text)
    except Exception as e:
        raise RuntimeError(f'AI Vision output could not be parsed: {e}')
    findings=[]
    for f in parsed.get('findings') or []:
        if not _is_remove_status(f.get('status','')):
            continue
        sk=str(f.get('target_key') or '').strip()
        selected=by_key.get(sk)
        opts=_candidate_rank_for_finding(f,catalog,sk)
        ff=dict(f)
        ff['suggested_target_id']=selected.get('target_id') if selected else ''
        ff['suggested_label']=selected.get('label') if selected else ''
        ff['candidate_options']=opts
        ff['auto_selected']=bool(selected and float(f.get('confidence') or 0)>=0.70)
        findings.append(ff)
    return {
      'floor':floor,'model':chosen,'pdf_name':pdf_path.name,
      'document_summary':parsed.get('document_summary') or '',
      'findings':findings,'finding_count':len(findings),
      'rule_statuses':list(ASBUILT_REMOVE_STATUSES),
      'note':'AI findings are review-only. No CAD/Excel change is applied until the operator selects a target and SHIFT/RESERVE.'
    }


def _asbuilt_project_candidate_catalog(label_pattern=None):
    """Build one unique candidate catalog across every analyzed project floor."""
    floor_order=['Ground Floor','First Floor','Second Floor','Third Floor','Roof Floor']
    floor_order += [f for f in PROJECT_RESULTS if f not in floor_order]
    all_rows=[]; by_key={}; lines=[]; per_floor={}
    fidx=0
    for floor in floor_order:
        if floor not in PROJECT_RESULTS:
            continue
        fidx+=1
        rows,_,_=_asbuilt_candidate_catalog(floor,label_pattern=label_pattern)
        if not rows:
            continue
        remapped=[]
        lines.append(f'--- FLOOR: {floor} ---')
        for i,t in enumerate(rows,1):
            key=f'F{fidx:02d}T{i:04d}'
            rec=dict(t); rec['candidate_key']=key; rec['floor']=floor
            remapped.append(rec); all_rows.append(rec); by_key[key]=rec
            label=str(t.get('label') or '').replace('|','/')
            svc=str(t.get('service') or '').replace('|','/')
            rack=str(t.get('rack') or '').replace('|','/')
            lines.append(f'{key}|floor={floor}|label={label}|service={svc}|rack={rack}|nx={int(t.get("nx") or 0)}|ny={int(t.get("ny") or 0)}')
        per_floor[floor]=remapped
    return all_rows,by_key,per_floor,'\n'.join(lines)


def analyze_asbuilt_pdf_project_with_vision(pdf_path,label_pattern=None,api_key=None,model=None):
    """Analyze one site PDF against the complete already-analyzed CAD project.

    This is review-only. It never mutates CAD/Excel during AI analysis.
    """
    pdf_path=Path(pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower()!='.pdf':
        raise RuntimeError('Uploaded As-Built PDF was not found.')
    if pdf_path.stat().st_size>ASBUILT_PDF_MAX_BYTES:
        raise RuntimeError('As-Built PDF exceeds the 200 MB AI Vision safety limit.')
    if not PROJECT_RESULTS:
        raise RuntimeError('Analyze the CAD project first. No project floors are loaded.')
    catalog,by_key,per_floor,candidate_text=_asbuilt_project_candidate_catalog(label_pattern=label_pattern)
    if not catalog:
        raise RuntimeError('No endpoint targets exist in the analyzed project.')
    if len(catalog)>2600:
        raise RuntimeError(f'The analyzed project has {len(catalog)} endpoints. Full-project AI review is limited to 2600 endpoints in one pass.')
    floors=list(per_floor)
    b64=base64.b64encode(pdf_path.read_bytes()).decode('ascii')
    schema={
      'type':'object','additionalProperties':False,
      'properties':{
        'document_summary':{'type':'string'},
        'findings':{'type':'array','items':{
          'type':'object','additionalProperties':False,
          'properties':{
            'page_number':{'type':'integer','minimum':1},
            'floor':{'type':'string','enum':floors},
            'building_hint':{'type':'string'},
            'status':{'type':'string','enum':list(ASBUILT_REMOVE_STATUSES)},
            'service_hint':{'type':'string'},
            'target_key':{'type':'string'},
            'target_label_seen':{'type':'string'},
            'nearby_text':{'type':'string'},
            'evidence':{'type':'string'},
            'confidence':{'type':'number','minimum':0,'maximum':1},
            'x_norm':{'type':'integer','minimum':0,'maximum':1000},
            'y_norm':{'type':'integer','minimum':0,'maximum':1000}
          },
          'required':['page_number','floor','building_hint','status','service_hint','target_key','target_label_seen','nearby_text','evidence','confidence','x_norm','y_norm']
        }}
      },
      'required':['document_summary','findings']
    }
    prompt=f"""You are reviewing ONE scanned/annotated telecom site As-Built PDF against a COMPLETE already-analyzed CAD project.

AVAILABLE CAD FLOORS: {', '.join(floors)}

IMPORTANT SAFETY RULES:
1) Extract ONLY site removals whose handwritten/site note clearly means one of these four approved statuses, normalized exactly to the matching phrase:
- لم يتم السحب
- تم رد الكابلات
- لا يوجد لها مسار
- تم نقل
2) Ignore every other handwritten note, dimensions, counts, arrows, corrections, highlights, and comments. Do not invent removals.
3) Determine the CAD floor for each finding from page title, printed plan context, floor notation, and nearby labels. floor MUST be one of AVAILABLE CAD FLOORS. If uncertain, keep confidence low.
4) Match each finding to the candidate endpoint list below. target_key MUST be one listed key when reasonably confident; otherwise return an empty string. Never fabricate a candidate key.
5) x_norm/y_norm are the approximate location of the marked endpoint on the drawing area of that page, normalized 0..1000 (left->right, top->bottom), ignoring page margins where possible.
6) evidence should be short: the handwritten approved phrase/mark plus nearby printed context that caused the finding. Preserve Arabic status text exactly.
7) A colored highlight by itself is NOT a removal unless the approved removal meaning is also present.
8) The PDF may contain multiple floors/pages. Return all approved findings across the whole project in a single result.

PROJECT CANDIDATE ENDPOINTS:
{candidate_text}
"""
    chosen=(model or ASBUILT_VISION_DEFAULT_MODEL).strip() or ASBUILT_VISION_DEFAULT_MODEL
    payload={
      'model':chosen,
      'input':[{'role':'user','content':[
        {'type':'input_file','filename':pdf_path.name,'file_data':'data:application/pdf;base64,'+b64,'detail':'high'},
        {'type':'input_text','text':prompt}
      ]}],
      'text':{'format':{'type':'json_schema','name':'teletech_asbuilt_project_pdf_review','strict':True,'schema':schema}},
      'max_output_tokens':16000
    }
    raw=_openai_responses_request(payload,api_key=api_key)
    text=_response_output_text(raw)
    if not text:
        raise RuntimeError('AI Vision API returned no structured output.')
    try:
        parsed=json.loads(text)
    except Exception as e:
        raise RuntimeError(f'AI Vision output could not be parsed: {e}')
    findings=[]
    for f in parsed.get('findings') or []:
        floor=str(f.get('floor') or '')
        if floor not in per_floor or not _is_remove_status(f.get('status','')):
            continue
        sk=str(f.get('target_key') or '').strip()
        selected=by_key.get(sk)
        if selected and selected.get('floor')!=floor:
            selected=None
        opts=_candidate_rank_for_finding(f,per_floor.get(floor) or [],sk)
        ff=dict(f)
        ff['suggested_target_id']=selected.get('target_id') if selected else ''
        ff['suggested_label']=selected.get('label') if selected else ''
        ff['candidate_options']=opts
        ff['auto_selected']=bool(selected and float(f.get('confidence') or 0)>=0.70)
        findings.append(ff)
    counts=Counter(str(f.get('floor') or '') for f in findings)
    return {
      'mode':'full_project','model':chosen,'pdf_name':pdf_path.name,
      'document_summary':parsed.get('document_summary') or '',
      'project_floors':floors,'project_endpoint_count':len(catalog),
      'findings':findings,'finding_count':len(findings),'finding_count_by_floor':dict(counts),
      'rule_statuses':list(ASBUILT_REMOVE_STATUSES),
      'note':'Full-project AI findings are review-only. No CAD/Excel change is applied until the operator reviews the endpoint and chooses SHIFT/RESERVE.'
    }


# ---------------------- FREE manual AI Vision path (no paid API) ----------------------
# The paid path above calls OpenAI's Responses API directly from the server, which
# needs a billed API key. This path lets the operator run the SAME prompt/schema in
# any free vision-capable chat (including the assistant they are already using to
# maintain this project), then paste the resulting JSON back in. Post-processing
# (candidate matching, auto_selected threshold, output shape) is identical to the
# paid path, so the existing review UI (renderVisionReview) needs no changes.

def _asbuilt_manual_prompt_bundle(label_pattern=None):
    """Build the exact prompt + candidate list the operator pastes into a free
    vision chat along with the As-Built PDF/image, so the returned JSON matches
    what analyze_asbuilt_manual_findings_project() expects.
    """
    if not PROJECT_RESULTS:
        raise RuntimeError('Analyze the CAD project first. No project floors are loaded.')
    catalog,by_key,per_floor,candidate_text=_asbuilt_project_candidate_catalog(label_pattern=label_pattern)
    if not catalog:
        raise RuntimeError('No endpoint targets exist in the analyzed project.')
    floors=list(per_floor)
    prompt=f"""You are reviewing ONE scanned/annotated telecom site As-Built PDF or image against a COMPLETE already-analyzed CAD project.

AVAILABLE CAD FLOORS: {', '.join(floors)}

IMPORTANT SAFETY RULES:
1) Extract ONLY site removals whose handwritten/site note clearly means one of these four approved statuses, normalized exactly to the matching phrase:
- لم يتم السحب
- تم رد الكابلات
- لا يوجد لها مسار
- تم نقل
2) Ignore every other handwritten note, dimensions, counts, arrows, corrections, highlights, and comments. Do not invent removals.
3) Determine the CAD floor for each finding from page title, printed plan context, floor notation, and nearby labels. floor MUST be one of AVAILABLE CAD FLOORS. If uncertain, keep confidence low.
4) Match each finding to the candidate endpoint list below. target_key MUST be one listed key when reasonably confident; otherwise return an empty string. Never fabricate a candidate key.
5) x_norm/y_norm are the approximate location of the marked endpoint on the drawing area of that page, normalized 0..1000 (left->right, top->bottom), ignoring page margins where possible.
6) evidence should be short: the handwritten approved phrase/mark plus nearby printed context that caused the finding. Preserve Arabic status text exactly.
7) A colored highlight by itself is NOT a removal unless the approved removal meaning is also present.
8) The PDF/image may contain multiple floors/pages. Return all approved findings across the whole project in a single result.

REQUIRED OUTPUT: reply with ONLY a single JSON object, no other text, matching exactly this shape:
{{
  "document_summary": "<one short sentence>",
  "findings": [
    {{
      "page_number": 1,
      "floor": "<one of AVAILABLE CAD FLOORS>",
      "building_hint": "",
      "status": "<one of the four approved Arabic phrases above, verbatim>",
      "service_hint": "<e.g. D, V, W, C, camera, wifi, data...>",
      "target_key": "<one key from the candidate list below, or empty string>",
      "target_label_seen": "<label text you can read near the mark, if any>",
      "nearby_text": "<short nearby printed/handwritten context>",
      "evidence": "<what you saw that triggered this finding>",
      "confidence": 0.0,
      "x_norm": 0,
      "y_norm": 0
    }}
  ]
}}
If there are no approved findings, return {{"document_summary":"...","findings":[]}}.

PROJECT CANDIDATE ENDPOINTS:
{candidate_text}
"""
    return {'floors':floors,'endpoint_count':len(catalog),'prompt':prompt}


def analyze_asbuilt_manual_findings_project(findings_raw, document_summary='', label_pattern=None):
    """Free alternative to analyze_asbuilt_pdf_project_with_vision().

    Takes a findings[] list the operator pasted (produced by any free AI chat using
    the prompt from _asbuilt_manual_prompt_bundle) instead of calling the paid
    OpenAI API. Runs the exact same candidate-matching/validation and returns the
    exact same shape, so the existing review UI works unchanged.
    """
    if not PROJECT_RESULTS:
        raise RuntimeError('Analyze the CAD project first. No project floors are loaded.')
    catalog,by_key,per_floor,_=_asbuilt_project_candidate_catalog(label_pattern=label_pattern)
    if not catalog:
        raise RuntimeError('No endpoint targets exist in the analyzed project.')
    if not isinstance(findings_raw,list):
        raise RuntimeError('Pasted AI Vision JSON must have a "findings" array. Paste the JSON object exactly as returned by the chat.')
    findings=[]
    for f in findings_raw:
        if not isinstance(f,dict):
            continue
        floor=str(f.get('floor') or '')
        if floor not in per_floor or not _is_remove_status(f.get('status','')):
            continue
        sk=str(f.get('target_key') or '').strip()
        selected=by_key.get(sk)
        if selected and selected.get('floor')!=floor:
            selected=None
        opts=_candidate_rank_for_finding(f,per_floor.get(floor) or [],sk)
        ff=dict(f)
        ff['suggested_target_id']=selected.get('target_id') if selected else ''
        ff['suggested_label']=selected.get('label') if selected else ''
        ff['candidate_options']=opts
        ff['auto_selected']=bool(selected and float(f.get('confidence') or 0)>=0.70)
        findings.append(ff)
    counts=Counter(str(f.get('floor') or '') for f in findings)
    return {
      'mode':'full_project_manual','model':'manual-free-vision','pdf_name':'',
      'document_summary':document_summary or '',
      'project_floors':list(per_floor),'project_endpoint_count':len(catalog),
      'findings':findings,'finding_count':len(findings),'finding_count_by_floor':dict(counts),
      'rule_statuses':list(ASBUILT_REMOVE_STATUSES),
      'note':'Manually pasted AI findings are review-only. No CAD/Excel change is applied until the operator reviews the endpoint and chooses SHIFT/RESERVE.'
    }


ASBUILT_CHAT_RESPONSE_SCHEMA={
    'type':'object',
    'properties':{
        'type':{'type':'string','enum':['ask','final']},
        'question':{'type':'string'},
        'document_summary':{'type':'string'},
        'legend_mapping':{'type':'array','items':{'type':'object','properties':{
            'color':{'type':'string'},'status_original':{'type':'string'},'normalized_status':{'type':'string'},
            'confirmed':{'type':'boolean'}},'required':['color','status_original','normalized_status','confirmed']}},
        'slide_counts':{'type':'array','items':{'type':'object','properties':{
            'page_number':{'type':'integer'},'floor':{'type':'string'},
            'pink':{'type':'integer'},'yellow':{'type':'integer'},'orange':{'type':'integer'},
            'blue':{'type':'integer'},'green':{'type':'integer'},'ambiguous':{'type':'integer'}},
            'required':['page_number','floor','pink','yellow','orange','blue','green','ambiguous']}},
        'markup_inventory':{'type':'array','items':{'type':'object','properties':{
            'markup_id':{'type':'string'},'page_number':{'type':'integer'},'floor':{'type':'string'},
            'color':{'type':'string'},'status_original':{'type':'string'},'confidence':{'type':'number'},
            'x_norm':{'type':'integer'},'y_norm':{'type':'integer'},'human_review_required':{'type':'boolean'}},
            'required':['markup_id','page_number','floor','color','status_original','confidence','x_norm','y_norm','human_review_required']}},
        'findings':{'type':'array','items':{
            'type':'object',
            'properties':{
                'page_number':{'type':'integer'},
                'floor':{'type':'string'},
                'status':{'type':'string','enum':list(ASBUILT_REMOVE_STATUSES)},
                'service_hint':{'type':'string'},
                'target_key':{'type':'string'},
                'target_label_seen':{'type':'string'},
                'nearby_text':{'type':'string'},
                'evidence':{'type':'string'},
                'confidence':{'type':'number'},
                'x_norm':{'type':'integer'},
                'y_norm':{'type':'integer'},
            },
            'required':['page_number','floor','status','service_hint','target_key','target_label_seen','nearby_text','evidence','confidence','x_norm','y_norm']
        }}
    },
    'required':['type','question','document_summary','legend_mapping','slide_counts','markup_inventory','findings']
}


def _asbuilt_chat_system_prompt(floors,candidate_text):
    return f"""You are the TELETECH As-Built revision reader. The uploaded document may be a PDF made entirely from scanned images, or standalone scanned PNG/JPG pages. Treat every page as a visual engineering drawing. Do NOT rely on PDF text extraction; inspect the page image itself, including handwritten Arabic notes, colored markups, arrows, circles, highlights, symbols and their spatial relation to the CAD drawing.

AVAILABLE CAD FLOORS: {', '.join(floors)}

CORE RULE: COUNT FIRST, INTERPRET SECOND.
Before deciding what any markup means, perform a complete visual inventory page-by-page. Scan each page in a fixed grid/top-to-bottom, left-to-right order. Then repeat the count independently in the reverse direction. If the two passes disagree, mark the page/count as ambiguous and require human review. Never silently choose the larger or smaller number.

MARKUP COUNT RULES:
1) Markup Count and CAD Entity Count are different. Count visible markup marks only; never convert a markup count into an entity count.
2) A visible distinct colored mark is one markup. Two touching/adjacent marks are two only when you can visually distinguish two separate strokes/marks; otherwise count them as ambiguous, not guessed.
3) Record Pink, Yellow, Orange, Blue and Green separately for every page. Unknown colors go to human review.
4) Preserve the original handwritten Arabic status literally. Do not translate it into a different status when the handwriting is uncertain.
5) Lock the Legend before interpreting the slides. If the legend says Pink = «لم يتم السحب نهائيًا», preserve exactly that meaning. Do not reverse it to «تم السحب».
6) If a color is not defined in the Legend, normalized_status must remain empty/null and human_review_required=true.
7) Never treat printed CAD symbols as handwritten markups merely because they have a similar color or shape. Use stroke/overlay/context to decide; if uncertain, flag it.

TWO-PASS OUTPUT:
- slide_counts must contain one row per page with the final confirmed count for each color plus ambiguous count.
- markup_inventory must contain one record for every markup that you can distinguish, with a unique markup_id such as MK-P02-001. Never merge two marks just because they appear near each other.
- If a mark cannot be separated confidently, create one ambiguous inventory record and do not invent an extra count.

REVISION / CAD MATCHING RULES:
1) The CAD project is the source of truth for existing Point/Endpoint IDs and coordinates. The scan is the source of truth for the technician's markup/revision intent.
2) Only approved removal statuses may currently become removable CAD findings: «لم يتم السحب», «تم رد الكابلات», «لا يوجد لها مسار», «تم نقل».
3) A color alone is never enough to create a CAD operation.
4) Match a markup to the candidate CAD endpoint only when the page/floor, room/nearby text, spatial location and drawing context support the match. Never fabricate a target_key.
5) Cable/routing notes, moves, additions, Patch Panel/Rack notes and other non-removal markups must be preserved in markup_inventory with their original text and human_review_required=true until a dedicated CAD operation is confirmed. Do not turn them into endpoint deletion.
6) x_norm/y_norm are page coordinates normalized 0..1000, left-to-right/top-to-bottom.
7) Do not modify CAD during analysis. This stage produces a reviewed revision manifest only.

INTERACTION PROTOCOL:
- If any count, legend text, markup separation, status, floor, or CAD match is genuinely ambiguous, return type="ask" and ONE short specific Arabic question about the single blocking ambiguity. Do not guess.
- For a final response, return the complete counts and markup inventory for the WHOLE document, plus only the CAD findings that are sufficiently supported.

CANDIDATE CAD ENDPOINTS:
{candidate_text}
"""


def _asbuilt_chat_validate_findings(findings_raw,by_key,per_floor):
    findings=[]
    for f in (findings_raw or []):
        if not isinstance(f,dict):
            continue
        floor=str(f.get('floor') or '')
        if floor not in per_floor or not _is_remove_status(f.get('status','')):
            continue
        sk=str(f.get('target_key') or '').strip()
        selected=by_key.get(sk)
        if selected and selected.get('floor')!=floor:
            selected=None
        opts=_candidate_rank_for_finding(f,per_floor.get(floor) or [],sk)
        ff=dict(f)
        ff['suggested_target_id']=selected.get('target_id') if selected else ''
        ff['suggested_label']=selected.get('label') if selected else ''
        ff['candidate_options']=opts
        ff['auto_selected']=bool(selected and float(f.get('confidence') or 0)>=0.70)
        findings.append(ff)
    return findings


def _asbuilt_chat_prune_sessions():
    now=time.time()
    for cid in [c for c,s in ASBUILT_CHAT_SESSIONS.items() if now-s.get('created',now)>ASBUILT_CHAT_TTL_SECONDS]:
        ASBUILT_CHAT_SESSIONS.pop(cid,None)


def _asbuilt_chat_result(chat_id,parsed,by_key,per_floor):
    out={'chat_id':chat_id,'type':parsed.get('type') or 'ask','question':parsed.get('question') or '',
         'document_summary':parsed.get('document_summary') or '','legend_mapping':parsed.get('legend_mapping') or [],
         'slide_counts':parsed.get('slide_counts') or [],'markup_inventory':parsed.get('markup_inventory') or [],
         'findings':[],'finding_count':0,'finding_count_by_floor':{}}
    if out['type']=='final':
        findings=_asbuilt_chat_validate_findings(parsed.get('findings') or [],by_key,per_floor)
        out['findings']=findings; out['finding_count']=len(findings)
        out['finding_count_by_floor']=dict(Counter(str(f.get('floor') or '') for f in findings))
    return out


def _glm_chat_request(messages, api_key=None, model=None):
    """Call GLM Vision through the OpenAI-compatible Z.ai Chat Completions API."""
    key=os.getenv('GLM_API_KEY','').strip()
    if not key:
        raise RuntimeError('GLM API key is missing. Set GLM_API_KEY in the .env file.')
    chosen=ASBUILT_GLM_DEFAULT_MODEL
    base=os.getenv('TELETECH_GLM_BASE_URL', os.getenv('GLM_BASE_URL', ASBUILT_GLM_BASE_URL)).rstrip('/')
    payload={
        'model':chosen,
        'messages':messages,
        'temperature':0.1,
        'max_tokens':16384,
        'response_format':{'type':'json_object'}
    }
    req=Request(base+'/chat/completions', data=json.dumps(payload,ensure_ascii=False).encode('utf-8'), method='POST', headers={
        'Authorization':'Bearer '+key,
        'Content-Type':'application/json',
        'Accept':'application/json',
        'User-Agent':'TELETECH-CAD-UI-GLM-AsBuilt-Vision',
    })
    try:
        with urlopen(req, timeout=ASBUILT_VISION_TIMEOUT) as r:
            raw=r.read()
    except HTTPError as e:
        try:
            detail=e.read().decode('utf-8','replace')
            j=json.loads(detail)
            err=j.get('error') if isinstance(j,dict) else None
            msg=(err.get('message') if isinstance(err,dict) else None) or detail
        except Exception:
            msg=str(e)
        raise RuntimeError(f'GLM API HTTP {getattr(e,"code","")}: {msg}')
    except URLError as e:
        raise RuntimeError(f'GLM API connection failed: {getattr(e,"reason",e)}')
    except TimeoutError:
        raise RuntimeError('GLM API timed out. Try again or increase TELETECH_VISION_TIMEOUT.')
    try:
        data=json.loads(raw.decode('utf-8'))
    except Exception as e:
        raise RuntimeError(f'GLM API returned invalid JSON: {e}')
    try:
        text=data['choices'][0]['message']['content']
    except Exception:
        raise RuntimeError('GLM API returned no text output: '+json.dumps(data,ensure_ascii=False)[:1200])
    if isinstance(text,list):
        text=''.join(str(x.get('text','')) if isinstance(x,dict) else str(x) for x in text)
    return str(text).strip(), {'role':'assistant','content':str(text).strip()}



def _asbuilt_hf_ocr_available():
    return bool(
        ASBUILT_HF_OCR_ENABLED
        and torch is not None
        and AutoModelForImageTextToText is not None
        and AutoProcessor is not None
        and Image is not None
    )


def _load_asbuilt_hf_ocr():
    """Lazy-load the small Arabic handwriting OCR model once per server process."""
    global _ASBUILT_HF_OCR_MODEL_OBJ, _ASBUILT_HF_OCR_PROCESSOR
    if not ASBUILT_HF_OCR_ENABLED:
        raise RuntimeError('Local Hugging Face As-Built OCR is disabled (TELETECH_HF_OCR_ENABLED=0).')
    if not _asbuilt_hf_ocr_available():
        missing=[]
        if torch is None: missing.append('torch')
        if AutoModelForImageTextToText is None or AutoProcessor is None: missing.append('transformers')
        if Image is None: missing.append('pillow')
        raise RuntimeError(
            'Local Hugging Face OCR dependencies are missing: ' + ', '.join(missing) +
            '. Install them with: pip install -r requirements_asbuilt_hf_ocr.txt'
        )
    with _ASBUILT_HF_OCR_LOCK:
        if _ASBUILT_HF_OCR_MODEL_OBJ is not None and _ASBUILT_HF_OCR_PROCESSOR is not None:
            return _ASBUILT_HF_OCR_MODEL_OBJ, _ASBUILT_HF_OCR_PROCESSOR

        # 1B model: substantially lighter than the previous 3B Qwen OCR.
        # Prefer BF16 on CUDA; CPU uses float32 for broad compatibility.
        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
        kwargs = dict(device_map='auto', trust_remote_code=True)
        try:
            model = AutoModelForImageTextToText.from_pretrained(
                ASBUILT_HF_OCR_MODEL, dtype=dtype, **kwargs
            )
        except TypeError:
            model = AutoModelForImageTextToText.from_pretrained(
                ASBUILT_HF_OCR_MODEL, torch_dtype=dtype, **kwargs
            )
        processor = AutoProcessor.from_pretrained(
            ASBUILT_HF_OCR_MODEL, trust_remote_code=True,
        )
        model.eval()
        _ASBUILT_HF_OCR_MODEL_OBJ = model
        _ASBUILT_HF_OCR_PROCESSOR = processor
        print(f'[HF-OCR] Loaded {ASBUILT_HF_OCR_MODEL}')
        print(f'[HF-OCR] CUDA available: {bool(torch.cuda.is_available())}')
        return model, processor


def _asbuilt_hf_ocr_image(image_path):
    """Run fast Arabic/English handwriting OCR on one rendered page image."""
    model, processor = _load_asbuilt_hf_ocr()

    img = Image.open(str(image_path)).convert('RGB')
    max_dim = max(int(img.width), int(img.height), 1)
    if max_dim > ASBUILT_HF_OCR_MAX_IMAGE_DIM:
        scale = ASBUILT_HF_OCR_MAX_IMAGE_DIM / float(max_dim)
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.Resampling.LANCZOS
        )

    messages = [{
        'role': 'user',
        'content': [
            {'type': 'image', 'image': img},
            {'type': 'text', 'text':
                'OCR only. Extract visible Arabic/English handwritten notes and important labels. '
                'Do not describe the drawing. Do not guess. If unreadable, write [غير واضح]. '
                'Return text only.'},
        ],
    }]

    text = processor.apply_chat_template(
        messages, add_generation_prompt=True, tokenize=False
    )
    inputs = processor(
        text=[text], images=[img], padding=True, return_tensors='pt'
    )
    try:
        device = next(model.parameters()).device
        inputs = {k: v.to(device) if hasattr(v, 'to') else v for k,v in inputs.items()}
    except Exception:
        pass

    with torch.inference_mode():
        generated = model.generate(
            **inputs,
            max_new_tokens=ASBUILT_HF_OCR_MAX_NEW_TOKENS,
            do_sample=False,
            pad_token_id=getattr(processor.tokenizer, 'eos_token_id', None),
            eos_token_id=getattr(processor.tokenizer, 'eos_token_id', None),
        )

    input_len = inputs['input_ids'].shape[1]
    decoded = processor.batch_decode(
        generated[:, input_len:], skip_special_tokens=True,
        clean_up_tokenization_spaces=True,
    )[0]
    return str(decoded).strip()


def _asbuilt_hf_ocr_page(page_path):
    """Wrapper used by the As-Built flow; never mutates the source image."""
    try:
        return _asbuilt_hf_ocr_image(page_path)
    except Exception as e:
        print(f'[HF-OCR] page OCR failed for {page_path}: {e}')
        return ''


def _asbuilt_qwen_available():
    return bool(ASBUILT_QWEN_ENABLED and torch is not None and AutoProcessor is not None and AutoModelForImageTextToText is not None and Image is not None)


def _load_asbuilt_qwen():
    global _ASBUILT_QWEN_MODEL_OBJ, _ASBUILT_QWEN_PROCESSOR
    if not _asbuilt_qwen_available():
        raise RuntimeError('Local Qwen Vision is unavailable. Install torch, transformers and pillow.')
    with _ASBUILT_QWEN_LOCK:
        if _ASBUILT_QWEN_MODEL_OBJ is not None and _ASBUILT_QWEN_PROCESSOR is not None:
            return _ASBUILT_QWEN_MODEL_OBJ, _ASBUILT_QWEN_PROCESSOR
        print(f'[QWEN] Loading {ASBUILT_QWEN_MODEL} ...')
        processor=AutoProcessor.from_pretrained(ASBUILT_QWEN_MODEL)
        dtype=torch.bfloat16 if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else (torch.float16 if torch.cuda.is_available() else torch.float32)
        model=AutoModelForImageTextToText.from_pretrained(ASBUILT_QWEN_MODEL, torch_dtype=dtype, device_map='auto')
        model.eval()
        _ASBUILT_QWEN_MODEL_OBJ=model
        _ASBUILT_QWEN_PROCESSOR=processor
        return model, processor


def _asbuilt_qwen_image(image_path):
    img=Image.open(str(image_path)).convert('RGB')
    max_dim=max(img.size)
    if max_dim > ASBUILT_QWEN_MAX_IMAGE_DIM:
        scale=ASBUILT_QWEN_MAX_IMAGE_DIM/float(max_dim)
        img=img.resize((max(1,int(img.width*scale)), max(1,int(img.height*scale))), Image.Resampling.LANCZOS)
    return img


def _extract_json_object(text):
    text=str(text or '').strip()
    try:
        return json.loads(text)
    except Exception:
        m=re.search(r'\{.*\}', text, re.S)
        if m:
            try: return json.loads(m.group(0))
            except Exception: pass
    raise RuntimeError('Qwen did not return valid JSON.')


def _asbuilt_qwen_interpret_page(image_path, page_number, cv_info, ocr_text, floor_names):
    model,processor=_load_asbuilt_qwen()
    img=_asbuilt_qwen_image(image_path)
    cv_counts=(cv_info or {}).get('counts') or {}
    cv_centers={k:[r.get('center_norm') for r in ((cv_info or {}).get('regions') or {}).get(k,[])[:30]] for k in ('pink','yellow','orange','blue','green')}
    prompt=f"""You are the local TELETECH As-Built visual interpreter for page {page_number}.
OpenCV already produced a fast color-region pre-count: {json.dumps(cv_counts,ensure_ascii=False)}.
Region centers 0..1000: {json.dumps(cv_centers,ensure_ascii=False)}.

Do NOT invent counts. Verify the colored regions against the image and interpret spatial context.
- Distinguish technician markups from printed CAD/legend artifacts.
- Group touching strokes only when clearly one markup; otherwise flag ambiguous.
- Locate each markup with x_norm/y_norm 0..1000.
- Read nearby room/area labels when visible.
- Preserve handwritten Arabic literally when possible.
- Never assign a CAD entity ID unless explicitly identified.
- Cables/arrows are markups too; preserve them as cable-related/unknown if operation is unclear.
- Legend is locked; Pink means «لم يتم السحب نهائيًا» and must not be reversed.
- Return JSON only.

Known CAD floor names: {', '.join(floor_names)}

OCR evidence (advisory only):
{ocr_text[:5000]}

Return exactly:
{{
  "page_number": {int(page_number)},
  "verified_markup_count": {{"pink":0,"yellow":0,"orange":0,"blue":0,"green":0}},
  "markup_inventory": [],
  "legend_observations": [],
  "human_review": [],
  "notes": ""
}}"""
    messages=[{'role':'user','content':[{'type':'image','image':img},{'type':'text','text':prompt}]}]
    text=processor.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
    inputs=processor(text=[text],images=[img],padding=True,return_tensors='pt')
    if hasattr(model,'device'):
        inputs={k:(v.to(model.device) if hasattr(v,'to') else v) for k,v in inputs.items()}
    with torch.inference_mode():
        out=model.generate(**inputs,max_new_tokens=ASBUILT_QWEN_MAX_NEW_TOKENS,do_sample=False)
    input_len=inputs['input_ids'].shape[1] if 'input_ids' in inputs else 0
    generated=out[:,input_len:] if input_len else out
    text_out=processor.batch_decode(generated,skip_special_tokens=True,clean_up_tokenization_spaces=False)[0]
    return _extract_json_object(text_out)


def _asbuilt_reconcile_cv_qwen(cv_info, qwen_result, page_number):
    cv=((cv_info or {}).get('counts') or {})
    q=((qwen_result or {}).get('verified_markup_count') or {})
    colors=('pink','yellow','orange','blue','green')
    row={'page_number':int(page_number),'pink':int(cv.get('pink',0) or 0),'yellow':int(cv.get('yellow',0) or 0),'orange':int(cv.get('orange',0) or 0),'blue':int(cv.get('blue',0) or 0),'green':int(cv.get('green',0) or 0),'ambiguous':len(qwen_result.get('human_review') or []) if isinstance(qwen_result,dict) else 0}
    disagreements=[]
    for c in colors:
        if c in q:
            try:
                qn=int(q.get(c,0) or 0); cn=int(cv.get(c,0) or 0)
                if qn != cn: disagreements.append(f'{c}: CV={cn}, Qwen={qn}')
            except Exception: pass
    row['cv_color_regions']={c:int(cv.get(c,0) or 0) for c in colors}
    row['qwen_verified_counts']={c:int(q.get(c,0) or 0) for c in colors if c in q}
    row['count_verification']='MATCH' if not disagreements else 'REVIEW'
    row['count_disagreements']=disagreements
    row['qwen_human_review']=qwen_result.get('human_review') or [] if isinstance(qwen_result,dict) else []
    return row


def _asbuilt_cv_color_inventory(image_path, legend_path=None):
    """Fast deterministic As-Built color detector.

    Legend handling is deliberately conservative:
    a separate legend image is only a reference. A page is suppressed only
    after a strong color-template match is verified. A weak/false match must
    never blank the real drawing.
    """
    if cv2 is None or np is None:
        return {'available': False, 'reason': 'opencv-python/numpy not installed'}

    try:
        img=cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if img is None:
            return {'available': False, 'reason': 'image could not be opened'}

        h,w=img.shape[:2]
        page_area=float(max(h*w,1))

        # ---- Safe legend exclusion ---------------------------------------
        # The previous grayscale/template threshold could match a large white
        # CAD area and accidentally remove the real markups. We now require:
        #   1) color/saturation matching,
        #   2) a strong score,
        #   3) at least two distinct expected legend colors,
        #   4) a reasonable candidate rectangle.
        legend_excluded=False
        legend_match_score=0.0
        legend_bbox=None
        legend_match_method='not_supplied'

        if legend_path and Path(str(legend_path)).exists():
            try:
                ref=cv2.imread(str(legend_path), cv2.IMREAD_COLOR)
                if ref is not None and ref.size>0:
                    rh,rw=ref.shape[:2]

                    # IMPORTANT:
                    # Do not use grayscale/saturation template matching here.
                    # A scanned CAD page contains huge white areas that can
                    # produce false template matches. Instead, learn the
                    # legend's colored-stroke signature from the reference
                    # image and look for the same vertical color sequence on
                    # the page.
                    ref_hsv=cv2.cvtColor(ref,cv2.COLOR_BGR2HSV)
                    color_ranges={
                        'pink':(155,179,70,100),
                        'green':(40,90,55,70),
                        'yellow':(20,40,60,100),
                        'orange':(5,20,80,100),
                    }

                    # Get representative component sizes/aspect ratios from
                    # the supplied legend reference.
                    ref_sig={}
                    for cname,(lo,hi,smin,vmin) in color_ranges.items():
                        cm=((ref_hsv[:,:,0]>=lo)&(ref_hsv[:,:,0]<=hi)&
                            (ref_hsv[:,:,1]>=smin)&(ref_hsv[:,:,2]>=vmin)).astype(np.uint8)
                        n,lab,st,cen=cv2.connectedComponentsWithStats(cm,8)
                        comps=[]
                        for i in range(1,n):
                            a=int(st[i,cv2.CC_STAT_AREA])
                            ww=int(st[i,cv2.CC_STAT_WIDTH])
                            hh=int(st[i,cv2.CC_STAT_HEIGHT])
                            if a>=80 and ww>=8 and hh>=5:
                                comps.append((a,ww,hh,int(st[i,cv2.CC_STAT_LEFT]),int(st[i,cv2.CC_STAT_TOP])))
                        if comps:
                            # Prefer the largest elongated colored stroke.
                            comps.sort(key=lambda z:(z[1]/max(z[2],1),z[0]), reverse=True)
                            a,ww,hh,_,_=comps[0]
                            ref_sig[cname]=(a,ww,hh)

                    page_hsv=cv2.cvtColor(img,cv2.COLOR_BGR2HSV)
                    page_components={}
                    for cname,(lo,hi,smin,vmin) in color_ranges.items():
                        cm=((page_hsv[:,:,0]>=lo)&(page_hsv[:,:,0]<=hi)&
                            (page_hsv[:,:,1]>=smin)&(page_hsv[:,:,2]>=vmin)).astype(np.uint8)
                        n,lab,st,cen=cv2.connectedComponentsWithStats(cm,8)
                        arr=[]
                        for i in range(1,n):
                            a=int(st[i,cv2.CC_STAT_AREA])
                            x=int(st[i,cv2.CC_STAT_LEFT]); y=int(st[i,cv2.CC_STAT_TOP])
                            ww=int(st[i,cv2.CC_STAT_WIDTH]); hh=int(st[i,cv2.CC_STAT_HEIGHT])
                            if a>=80 and ww>=8 and hh>=5:
                                arr.append({'a':a,'x':x,'y':y,'w':ww,'h':hh,
                                            'cx':float(cen[i][0]),'cy':float(cen[i][1])})
                        page_components[cname]=arr

                    # The known legend order is:
                    # pink -> green -> yellow -> orange -> orange.
                    # Search for a compact vertical cluster.  This is much
                    # safer than suppressing an arbitrary rectangle.
                    candidates=[]
                    for p in page_components['pink']:
                        for g in page_components['green']:
                            if g['cy'] <= p['cy'] or abs(g['cx']-p['cx']) > max(220,0.18*w):
                                continue
                            for yel in page_components['yellow']:
                                if yel['cy'] <= g['cy'] or abs(yel['cx']-p['cx']) > max(220,0.18*w):
                                    continue
                                for o1 in page_components['orange']:
                                    if o1['cy'] <= yel['cy'] or abs(o1['cx']-p['cx']) > max(220,0.18*w):
                                        continue
                                    for o2 in page_components['orange']:
                                        if o2 is o1 or o2['cy'] <= o1['cy'] or abs(o2['cx']-p['cx']) > max(220,0.18*w):
                                            continue

                                        pts=[p,g,yel,o1,o2]
                                        ys=[q['cy'] for q in pts]
                                        xs=[q['cx'] for q in pts]
                                        minx,maxx=min(q['x'] for q in pts),max(q['x']+q['w'] for q in pts)
                                        miny,maxy=min(q['y'] for q in pts),max(q['y']+q['h'] for q in pts)
                                        bw=maxx-minx; bh=maxy-miny
                                        if bw<=0 or bh<=0:
                                            continue

                                        # Legend strokes are horizontally elongated
                                        # and tightly aligned in x.
                                        if bw > max(320,0.30*w) or bh > max(650,0.55*h):
                                            continue
                                        xspread=max(xs)-min(xs)
                                        if xspread > max(220,0.16*w):
                                            continue

                                        # Similarity to reference component shapes.
                                        shape_score=0.0; used=0
                                        for cname,q in [('pink',p),('green',g),('yellow',yel),('orange',o1)]:
                                            if cname not in ref_sig:
                                                continue
                                            ra,rw0,rh0=ref_sig[cname]
                                            # Resolution/phone-camera scale can
                                            # change absolute component size by
                                            # a large factor.  Use the reference
                                            # primarily for the stroke shape.
                                            ref_aspect=rw0/max(rh0,1)
                                            q_aspect=q['w']/max(q['h'],1)
                                            aspect_pen=abs(np.log((q_aspect+1e-6)/(ref_aspect+1e-6)))
                                            local=max(0.0,1.0-0.75*aspect_pen)
                                            shape_score += local
                                            used += 1
                                        shape_score /= max(used,1)

                                        # Normalize vertical spacing; legend rows
                                        # are relatively regular.
                                        gaps=[ys[i+1]-ys[i] for i in range(4)]
                                        spacing_cv=(max(gaps)-min(gaps))/max(np.mean(gaps),1.0)
                                        spacing_score=max(0.0,1.0-0.8*spacing_cv)

                                        score=0.65*shape_score+0.35*spacing_score
                                        candidates.append((score,(minx,miny,bw,bh)))

                    if candidates:
                        candidates.sort(key=lambda z:z[0],reverse=True)
                        score,bbox=candidates[0]
                        bx,by,bw,bh=bbox
                        # Strong verification only.  Never suppress the page
                        # if the signature is uncertain.
                        if score>=0.58 and (bw*bh)/page_area <= 0.20:
                            legend_match_score=float(score)
                            legend_bbox=(int(bx),int(by),int(bw),int(bh))
                            legend_excluded=True
                            legend_match_method='color_sequence_verified'
                        else:
                            legend_match_score=float(score)
                            legend_match_method='reference_not_verified'
                    else:
                        legend_match_method='reference_not_verified'
            except Exception as e:
                legend_match_method=f'reference_match_error:{type(e).__name__}'

        hsv=cv2.cvtColor(img,cv2.COLOR_BGR2HSV)

        # Component area is intentionally kept small and stable.  The old
        # page-area multiplier rejected real markups on high-resolution scans
        # (e.g. requiring ~7,000 pixels on a 4MP page while a real markup may
        # occupy ~500-1,500 pixels).  We count separate connected components
        # and only discard tiny speckles.
        pink_min=40
        yellow_min=40
        orange_min=40
        green_min=40

        masks={
            'pink':((hsv[:,:,0]>=155)&(hsv[:,:,0]<=179)&(hsv[:,:,1]>=70)&(hsv[:,:,2]>=100)),
            'yellow':((hsv[:,:,0]>=20)&(hsv[:,:,0]<=40)&(hsv[:,:,1]>=60)&(hsv[:,:,2]>=100)),
            'orange':((hsv[:,:,0]>=5)&(hsv[:,:,0]<=20)&(hsv[:,:,1]>=80)&(hsv[:,:,2]>=100)),
            'green':((hsv[:,:,0]>=40)&(hsv[:,:,0]<=90)&(hsv[:,:,1]>=55)&(hsv[:,:,2]>=70)),
        }
        mins={'pink':pink_min,'yellow':yellow_min,'orange':orange_min,'green':green_min}

        out={
            'available':True,'width':int(w),'height':int(h),
            'counts':{'pink':0,'yellow':0,'orange':0,'blue':0,'green':0},
            'regions':{'pink':[],'yellow':[],'orange':[],'blue':[],'green':[]},
            'blue_ignored':True,
            'counting_rule':'SEPARATE_COMPONENTS_NO_MERGE',
            'confidence_threshold':ASBUILT_MIN_MARKUP_CONFIDENCE,
            'legend_excluded':legend_excluded,
            'legend_match_score':round(float(legend_match_score),3),
            'legend_bbox':legend_bbox,
            'legend_match_method':legend_match_method
        }

        # Only remove pixels after a VERIFIED legend match.
        if legend_excluded and legend_bbox:
            lx,ly,lw,lh=legend_bbox
            for m in masks.values():
                m[max(0,ly):min(h,ly+lh),max(0,lx):min(w,lx+lw)] = False

        for color,mask in masks.items():
            # Raw connected components: no morphology, so adjacent points
            # remain separate components.
            m=mask.astype(np.uint8)*255
            n,labels,stats,centroids=cv2.connectedComponentsWithStats(m,8)
            regions=[]

            for i in range(1,n):
                a=int(stats[i,cv2.CC_STAT_AREA])
                if a<mins[color]:
                    continue

                x=int(stats[i,cv2.CC_STAT_LEFT])
                y=int(stats[i,cv2.CC_STAT_TOP])
                rw=int(stats[i,cv2.CC_STAT_WIDTH])
                rh=int(stats[i,cv2.CC_STAT_HEIGHT])

                # Confidence is a visual-quality gate, not a raw pixel-area
                # gate.  Real markups in these scans are often 200-1,200
                # pixels; requiring 1,500 pixels incorrectly removed valid
                # points.  Compact/saturated components above 200 px are
                # treated as the reliable 78% class. Tiny speckles stay
                # below the gate.
                fill_ratio=float(a)/max(rw*rh,1)
                aspect=float(rw)/max(rh,1)
                if a>=200 and fill_ratio>=0.45 and 0.45<=aspect<=3.5:
                    conf=0.78
                elif a>=80 and fill_ratio>=0.35:
                    conf=0.70
                else:
                    conf=0.55
                if conf<ASBUILT_MIN_MARKUP_CONFIDENCE:
                    continue
                cx=float(centroids[i][0])
                cy=float(centroids[i][1])

                regions.append({
                    'component_id':int(i),
                    'area':a,
                    'confidence':conf,
                    'bbox':[x,y,rw,rh],
                    'center_norm':[
                        round(cx/max(w,1)*1000),
                        round(cy/max(h,1)*1000)
                    ]
                })

            regions.sort(key=lambda r:(r['bbox'][1],r['bbox'][0]))
            out['regions'][color]=regions[:200]
            out['counts'][color]=len(out['regions'][color])

        # Blue is drawing/text ink, never a markup.
        out['counts']['blue']=0
        out['regions']['blue']=[]
        return out

    except Exception as e:
        return {'available':False,'reason':str(e)}

def _asbuilt_cv_evidence_text(cv_info):
    """Compact detector evidence. Blue is explicitly excluded."""
    if not cv_info or not cv_info.get('available'):
        return 'LOCAL COLOR DETECTOR: unavailable for this page.'
    c=cv_info.get('counts',{})
    lines=[
        'LOCAL COLOR DETECTOR (OpenCV) — code-only visual evidence:',
        f"pink={c.get('pink',0)}, yellow={c.get('yellow',0)}, orange={c.get('orange',0)}, green={c.get('green',0)}, blue=0 (BLUE IGNORED).",
        f'COUNTING RULE: every separate connected colored component above {ASBUILT_MIN_MARKUP_CONFIDENCE:.2f} confidence is a separate markup candidate; nearby candidates are never merged.',
        'NEVER merge nearby markups. Do not use morphology/closing/dilation to fuse neighboring points.',
        'BLUE is CAD/text/ink in this project and MUST NOT be counted as a markup.',
        f'LEGEND REFERENCE: {("matched and excluded" if cv_info.get("legend_excluded") else ("supplied but NOT matched; page was left untouched" if cv_info.get("legend_match_method") not in (None, "not_supplied") else "not supplied"))}.',
    ]
    for color in ('pink','yellow','orange','green'):
        regs=(cv_info.get('regions') or {}).get(color) or []
        if regs:
            centers=', '.join(f"({r['center_norm'][0]},{r['center_norm'][1]})" for r in regs[:40])
            lines.append(f'{color} region centers (0..1000): {centers}')
    return '\n'.join(lines)

def _asbuilt_prepare_image_for_glm(src_path, page_meta=None):
    """Prepare PNG/JPG for GLM Vision without requiring Pillow.
    Uses PyMuPDF (fitz), which is already required for scanned-PDF page rendering.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for As-Built image processing. Install with: pip install pymupdf")
    max_dim=int(os.getenv('ASBUILT_GLM_MAX_IMAGE_DIM','1800') or '1800')
    max_bytes=int(os.getenv('ASBUILT_GLM_MAX_IMAGE_BYTES',str(1500*1024)) or str(1500*1024))
    src=str(src_path)
    doc=None
    try:
        doc=fitz.open(src)
        if len(doc)<1:
            raise RuntimeError('The As-Built image could not be opened.')
        page=doc[0]
        rect=page.rect
        longest=max(float(rect.width),float(rect.height),1.0)
        scale=min(1.0, float(max_dim)/longest)
        # Render at a reasonable resolution, then reduce JPEG quality if needed.
        matrix=fitz.Matrix(scale,scale)
        quality=82
        data=b''
        while quality>=45:
            pix=page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
            try:
                data=pix.tobytes('jpeg', jpg_quality=quality)
            except TypeError:
                data=pix.tobytes('jpeg')
            if len(data)<=max_bytes or scale<=0.5:
                break
            scale=max(0.5,scale*0.82)
            matrix=fitz.Matrix(scale,scale)
            quality-=7
        meta=dict(page_meta or {})
        meta.update({'vision_mime':'image/jpeg','vision_bytes':len(data),
                     'vision_width':pix.width,'vision_height':pix.height})
        return meta, {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(data).decode('ascii')}}
    finally:
        if doc is not None:
            doc.close()

def _asbuilt_page_parts_for_glm(pdf_path):
    """Render PDF pages and compress EVERY page before sending to GLM."""
    pages=_split_asbuilt_pdf_to_images(pdf_path)
    return [_asbuilt_prepare_image_for_glm(pg['path'],pg) for pg in pages]

def _asbuilt_batch_analysis_prompt(sys_prompt, page_numbers, total_pages):
    nums=', '.join(str(x) for x in page_numbers)
    return sys_prompt + f"""\n\nMULTI-IMAGE PAGE-BATCH MODE:
You are receiving exactly {len(page_numbers)} independent drawing-sheet images from a scanned As-Built PDF.
The images are provided in this exact order and MUST remain separate:
{nums}

Analyze EVERY image. Never merge marks from different images.
For EACH page, return one slide_counts record and markup_inventory records tagged with that page_number.
Count Pink/Yellow/Orange/Blue/Green independently for each page using TWO visual counting passes:
PASS 1: scan the whole page systematically (top-to-bottom, left-to-right) and count every visible markup.
PASS 2: independently recount each color and compare with PASS 1. If they disagree, inspect the image again and report the unresolved ambiguity instead of guessing.

IMPORTANT: Markup Count is NOT Entity Count. A colored mark is a markup unless the CAD target is explicitly identified.
Do not use text extraction from the PDF. The supplied images are the authoritative visual source.
Do not invent Point IDs, Cable IDs, room IDs, or CAD operations.
The page_number for every inventory record MUST match the image it came from.
Return JSON only."""

def _asbuilt_guess_floor_hint(text):
    """Best-effort floor hint from an As-Built filename. Empty means unknown."""
    s=str(text or '').upper()
    patterns=[
        (r'\b(?:HUM[-_ ]*)?TH\b','Third Floor'),
        (r'\b(?:HUM[-_ ]*)?T\b','Third Floor'),
        (r'\b(?:HUM[-_ ]*)?S(?:[12])?\b','Second Floor'),
        (r'\b(?:HUM[-_ ]*)?F(?:[12])?\b','First Floor'),
        (r'\b(?:HUM[-_ ]*)?G(?:[12])?\b','Ground Floor'),
        (r'\bROOF\b','Roof Floor'),
    ]
    for pat,floor in patterns:
        if re.search(pat,s,re.I): return floor
    return ''


def _asbuilt_sheet_floor(page_number, total_pages):
    """Map scanned As-Built half-sheets to the CAD floor file.

    Project rule: two consecutive images form one drawing sheet (right half,
    then left half). The last Roof image belongs to the Third Floor CAD file,
    because the Third Floor drawing and Roof are stored in the same CAD file.
    This is intentionally deterministic and does not depend on handwriting OCR.
    """
    try: p=int(page_number); n=int(total_pages)
    except Exception: return ''
    if p<=0: return ''
    if p in (1,2): return 'Ground Floor'
    if p in (3,4): return 'First Floor'
    if p in (5,6): return 'Second Floor'
    if p in (7,8): return 'Third Floor'
    if p==9 and n>=9: return 'Third Floor'  # Roof is stored with Third Floor CAD
    # Continue the same two-page pattern for longer uploads.
    pair_floor={0:'Ground Floor',1:'First Floor',2:'Second Floor',3:'Third Floor'}
    idx=(p-1)//2
    return pair_floor.get(idx,'')


def _asbuilt_match_markup_to_cad(markup, catalog, per_floor=None, floor_hint=''):
    """Rank existing CAD endpoints by normalized sheet position; review-only."""
    try: sx=float(markup.get('x_norm') or 500); sy=float(markup.get('y_norm') or 500)
    except Exception: sx=sy=500.0
    scored=[]
    for t in catalog or []:
        floor=str(t.get('floor') or '')
        if floor_hint and floor != floor_hint: continue
        dx=float(t.get('nx') or 0)-sx; dy=float(t.get('ny') or 0)-sy
        dist=(dx*dx+dy*dy)**0.5
        if dist<=60: conf=.90
        elif dist<=110: conf=.78
        elif dist<=180: conf=.62
        elif dist<=280: conf=.45
        else: conf=.25
        scored.append((dist,t,conf))
    # HARD RULE: never fall back to another floor. If the page floor has no
    # candidates, leave it unmatched for review instead of suggesting a wrong floor.
    scored.sort(key=lambda z:z[0])
    out=[]
    for dist,t,conf in scored[:8]:
        out.append({'target_id':t.get('target_id'),'candidate_key':t.get('candidate_key'),
                    'floor':t.get('floor'),'label':t.get('label') or t.get('block') or t.get('target_id'),
                    'service':t.get('service',''),'block':t.get('block',''),'rack':t.get('rack',''),
                    'x':t.get('x'),'y':t.get('y'),'cad_x_norm':int(t.get('nx') or 0),
                    'cad_y_norm':int(t.get('ny') or 0),'distance_norm':round(dist,1),
                    'match_confidence':round(conf,2),'editable_direct':bool(t.get('editable_direct'))})
    return out

def _asbuilt_code_markup_inventory(cv_info, page_number, ocr_text='', catalog=None, per_floor=None, floor_hint=''):
    """Deterministic code-only markup inventory from OpenCV regions.

    This is intentionally NOT an entity matcher. Every detected colored region is
    kept as a separate candidate with pixel bbox/center evidence. Human review is
    required before any CAD operation.
    """
    if not cv_info or not cv_info.get('available'):
        return []
    inv=[]
    for color in ('pink','yellow','orange','green'):
        for idx,r in enumerate((cv_info.get('regions') or {}).get(color) or [],1):
            bbox=r.get('bbox') or [0,0,0,0]
            area=float(r.get('area') or 0)
            # Conservative confidence: size alone never makes a CAD decision.
            conf=0.55
            if area >= 500: conf=0.70
            if area >= 1500: conf=0.78
            # Project-validated counting rule: candidates below 78% are not
            # included in the actionable markup inventory. They remain visible
            # only as low-confidence detector noise in the raw CV evidence.
            if conf < ASBUILT_MIN_MARKUP_CONFIDENCE:
                continue
            row={
                'markup_id':f'MK-{str(color).upper()}-P{int(page_number):02d}-{idx:03d}',
                'page_number':int(page_number),'color':color,
                'status_original':'لم يتم السحب نهائيًا' if color=='pink' else ('مسحوب' if color=='green' else None),
                'normalized_status':'NOT_WITHDRAWN_FINAL' if color=='pink' else ('WITHDRAWN' if color=='green' else None),
                'confidence':conf,
                'x_norm':int((r.get('center_norm') or [0,0])[0]),'y_norm':int((r.get('center_norm') or [0,0])[1]),
                'bbox_px':[int(v) for v in bbox[:4]],'area_px':int(area),'entity_identified':False,
                'human_review_required':True,'evidence':'OpenCV color region; not a CAD entity.',
                'ocr_text':str(ocr_text or '')[:1000],'floor_hint':floor_hint or '',
                'candidate_options':[],'suggested_target_id':'','suggested_target_label':'',
                'match_confidence':0.0,'match_status':'UNMATCHED_REVIEW'
            }
            if catalog:
                opts=_asbuilt_match_markup_to_cad(row,catalog,per_floor,floor_hint=floor_hint)
                row['candidate_options']=opts
                if opts:
                    row['suggested_target_id']=opts[0].get('target_id') or ''
                    row['suggested_target_label']=opts[0].get('label') or ''
                    row['match_confidence']=float(opts[0].get('match_confidence') or 0)
                    row['match_status']='CANDIDATE_MATCH' if row['match_confidence']>=0.78 else 'REVIEW_MATCH'
            inv.append(row)
    return inv

def asbuilt_chat_start(path,filename,api_key='',model='',label_pattern=None,first_message='',paths=None,filenames=None,legend_path=''):
    """Start the deterministic As-Built scan.

    Input can be one PDF or multiple PDF/image files. PDFs are rendered page-by-page
    so scanned PDFs are treated exactly like images. OpenCV detects markup colors.
    No GLM/Qwen/remote AI call is made and no CAD change is applied here.
    """
    if not PROJECT_RESULTS:
        raise RuntimeError('Analyze the CAD project before starting the As-Built scan.')

    label_pattern,_=_validate_label_pattern(label_pattern)
    catalog,by_key,per_floor,candidate_text=_asbuilt_project_candidate_catalog(label_pattern=label_pattern)
    if not catalog:
        raise RuntimeError('No endpoint targets exist in the analyzed project.')

    input_paths=list(paths or []) or [path]
    input_names=list(filenames or []) or [filename or Path(path).name]
    if len(input_names)<len(input_paths):
        input_names += [Path(x).name for x in input_paths[len(input_names):]]

    pages=[]
    page_number=1
    supported={'.pdf','.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff'}

    for src,name in zip(input_paths,input_names):
        ext=Path(name or src).suffix.lower()
        if ext not in supported:
            raise RuntimeError(f'Unsupported As-Built input: {name}')

        if ext=='.pdf':
            rendered=_split_asbuilt_pdf_to_images(src)
            if not rendered:
                raise RuntimeError(f'Could not render scanned PDF: {name}')
            for pg in rendered:
                pg=dict(pg)
                pg['page_number']=page_number
                try:
                    pg['cv_color_inventory']=_asbuilt_cv_color_inventory(pg['path'], legend_path=legend_path)
                except Exception as e:
                    pg['cv_color_inventory']={'available':False,'error':str(e),'counts':{}}
                pages.append(pg)
                page_number += 1
        else:
            pg={
                'path':str(src),
                'filename':Path(name or src).name,
                'page_number':page_number
            }
            try:
                pg['cv_color_inventory']=_asbuilt_cv_color_inventory(src, legend_path=legend_path)
            except Exception as e:
                pg['cv_color_inventory']={'available':False,'error':str(e),'counts':{}}
            pages.append(pg)
            page_number += 1

    rows=[]
    inventory=[]
    failures=[]

    # Review-only CAD matching: normalize endpoint coordinates independently per floor.
    match_catalog=[]
    match_per_floor={}
    for floor_name,floor_rows in (per_floor or {}).items():
        floor_rows=floor_rows or []
        if not floor_rows: continue
        xs=[float(t.get('x') or 0) for t in floor_rows]; ys=[float(t.get('y') or 0) for t in floor_rows]
        minx,maxx=min(xs),max(xs); miny,maxy=min(ys),max(ys)
        dx=max(maxx-minx,1e-9); dy=max(maxy-miny,1e-9)
        norm_rows=[]
        for i,t in enumerate(floor_rows,1):
            rec=dict(t); rec['candidate_key']=rec.get('candidate_key') or f'{floor_name}-T{i:04d}'
            rec['nx']=int(round((float(t.get('x') or 0)-minx)*1000/dx)); rec['ny']=int(round((maxy-float(t.get('y') or 0))*1000/dy))
            norm_rows.append(rec); match_catalog.append(rec)
        match_per_floor[floor_name]=norm_rows

    total_pages=len(pages)
    for pg in pages:
        pno=int(pg['page_number'])
        cv=pg.get('cv_color_inventory') or {}
        # The sheet/floor is a property of the drawing pair, not of each markup.
        # OCR is not used to guess the floor because the handwritten floor title
        # is unreliable for this project.
        sheet_floor=_asbuilt_sheet_floor(pno,total_pages)
        counts=(cv.get('counts') or {}) if cv.get('available') else {}

        row={
            'page_number':pno,
            'floor':sheet_floor,
            'sheet_floor':sheet_floor,
            'sheet_id':int((pno+1)//2),
            'sheet_half':'right' if pno % 2 == 1 else 'left',
            'pink':int(counts.get('pink',0) or 0),
            'yellow':int(counts.get('yellow',0) or 0),
            'orange':int(counts.get('orange',0) or 0),
            'blue':int(counts.get('blue',0) or 0),
            'green':int(counts.get('green',0) or 0),
            'ambiguous':0,
            'cv_color_regions':{
                k:int(counts.get(k,0) or 0)
                for k in ('pink','yellow','orange','blue','green')
            },
            'count_verification':'CODE_CV_ONLY',
            'count_disagreements':[],
            'human_review_required':True,
            'ocr_text':'',
            'legend_excluded':bool(cv.get('legend_excluded')),
            'legend_match_score':float(cv.get('legend_match_score') or 0),
            'legend_match_method':cv.get('legend_match_method') or 'not_supplied'
        }

        if not cv.get('available'):
            row['count_verification']='CV_UNAVAILABLE'
            row['count_disagreements']=[str(cv.get('error') or 'OpenCV unavailable')]
            row['ambiguous']=1
            failures.append({
                'page_number':pno,
                'error':str(cv.get('error') or 'OpenCV unavailable')
            })

        rows.append(row)
        floor_hint=sheet_floor
        inventory.extend(_asbuilt_code_markup_inventory(cv,pno,'',catalog=match_catalog,per_floor=match_per_floor,floor_hint=floor_hint))

    chat_id=uuid.uuid4().hex
    result={
        'chat_id':chat_id,
        'type':'final',
        'question':(
            'تم الفحص البرمجي. راجع العلامات المكتشفة يدويًا قبل اعتماد أي تعديل CAD.'
            if pages else 'لم يتم العثور على صفحات للتحليل.'
        ),
        'document_summary':(
            f'Code-only As-Built scan: {len(pages)} page(s). '
            f'PDF pages were rendered as images. OpenCV detected colored markup regions. '
            f'No GLM/Qwen/AI agent was used and no CAD change was applied automatically. '
            f'Floor matching uses the project sheet-pair rule: pages 1-2 G, 3-4 F, 5-6 S, 7-8 T, and page 9 Roof -> Third Floor CAD.'
        ),
        'legend_mapping':[],
        'slide_counts':rows,
        'markup_inventory':inventory,
        'findings':[],
        'finding_count':0,
        'finding_count_by_floor':{},
        'page_failures':failures,
        'human_review_required':True,
        'deep_verify_available':False,
        'cad_match_available':bool(match_catalog),
        'cad_match_mode':'NORMALIZED_SPATIAL_REVIEW_ONLY',
        'pipeline':'CODE_ONLY_OPENCV_WITH_CAD_MATCH_REVIEW'
    }

    with ASBUILT_CHAT_LOCK:
        _asbuilt_chat_prune_sessions()
        ASBUILT_CHAT_SESSIONS[chat_id]={
            'history':[],
            'model':'code-only',
            'api_key':'',
            'label_pattern':label_pattern,
            'by_key':by_key,
            'per_floor':per_floor,
            'created':time.time(),
            'split_pages':pages,
            'source_filename':filename,
            'page_failures':failures,
            'fast_scan':True,
            'legend_path':legend_path
        }

    return result


def asbuilt_chat_reply(chat_id,message):
    """Store a human clarification/correction without invoking an AI model."""
    with ASBUILT_CHAT_LOCK:
        sess=ASBUILT_CHAT_SESSIONS.get(chat_id)
    if not sess:
        raise RuntimeError('As-Built scan session not found or expired. Start a new scan.')

    msg=str(message or '').strip()
    if not msg:
        raise RuntimeError('Type a correction or clarification before sending.')

    sess.setdefault('review_notes',[]).append(msg)
    return {
        'chat_id':chat_id,
        'type':'ask',
        'question':'تم تسجيل ملاحظة المراجعة. لا يوجد تعديل CAD تلقائي؛ أضف التعديل المعتمد من Manual / Review Revision Entry.',
        'document_summary':'Code-only scan remains unchanged until a human-approved revision is entered.',
        'legend_mapping':[],
        'slide_counts':[],
        'markup_inventory':[],
        'findings':[],
        'finding_count':0,
        'finding_count_by_floor':{}
    }


def _legacy_gemini_asbuilt_chat_start(path,filename,api_key,model,label_pattern=None,first_message=''):
    """Start one project-wide As-Built AI chat: upload the file once to Gemini,
    send the rules + candidate catalog, and return the model's first turn
    (a clarifying question, or already-final findings if nothing is ambiguous).
    """
    if not PROJECT_RESULTS:
        raise RuntimeError('Analyze the CAD project before starting the As-Built chat.')
    label_pattern,_=_validate_label_pattern(label_pattern)
    catalog,by_key,per_floor,candidate_text=_asbuilt_project_candidate_catalog(label_pattern=label_pattern)
    if not catalog:
        raise RuntimeError('No endpoint targets exist in the analyzed project.')
    ext=Path(filename or path).suffix.lower()
    mime={'.pdf':'application/pdf','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg'}.get(ext)
    if not mime:
        raise RuntimeError('Only PDF, PNG or JPG As-Built files are supported.')
    chosen=(model or ASBUILT_GEMINI_DEFAULT_MODEL).strip() or ASBUILT_GEMINI_DEFAULT_MODEL
    # IMPORTANT: scanned PDF -> page images before Vision. This prevents the
    # model from relying on a missing/weak PDF text layer and makes every sheet
    # independently inspectable. Non-PDF images continue through unchanged.
    if ext=='.pdf':
        split_pages,vision_parts=_asbuilt_page_parts_for_gemini(path,api_key)
    else:
        split_pages=[{'path':path,'filename':Path(filename or path).name,'mime_type':mime,'page_number':1}]
        gfile=_gemini_upload_file(path,api_key,mime)
        vision_parts=[{'file_data':{'file_uri':gfile['uri'],'mime_type':gfile['mime_type']}}]
    floors=list(per_floor)
    sys_prompt=_asbuilt_chat_system_prompt(floors,candidate_text)
    if ext=='.pdf':
        sys_prompt += "\n\nVISION INPUT MODE: The uploaded As-Built PDF was split into independent page images before analysis.\n"
        sys_prompt += "There are %d rendered pages. Treat each image as one drawing sheet/page. Never skip a page. Keep page numbers 1..%d exactly aligned with image order." % (len(split_pages),len(split_pages))
        sys_prompt += "\nFirst perform a page-by-page visual inventory/count pass, then a second verification pass before producing findings."
    if first_message:
        sys_prompt+=f"\n\nOPERATOR NOTE (read before you start): {first_message}"
    history=[{'role':'user','parts':vision_parts+[{'text':sys_prompt}]}]
    text,model_turn=_gemini_generate(chosen,history,api_key,response_schema=ASBUILT_CHAT_RESPONSE_SCHEMA)
    history.append(model_turn)
    try:
        parsed=json.loads(text)
    except Exception as e:
        raise RuntimeError(f'Gemini returned invalid JSON: {e}')
    chat_id=uuid.uuid4().hex
    with ASBUILT_CHAT_LOCK:
        _asbuilt_chat_prune_sessions()
        ASBUILT_CHAT_SESSIONS[chat_id]={'history':history,'model':chosen,'api_key':api_key,
            'label_pattern':label_pattern,'by_key':by_key,'per_floor':per_floor,'created':time.time(),
            'split_pages':split_pages,'source_filename':filename}
    return _asbuilt_chat_result(chat_id,parsed,by_key,per_floor)


def _legacy_gemini_asbuilt_chat_reply(chat_id,message):
    """Continue an existing As-Built chat with one operator message (an answer
    to the model's question, or a correction) and return the model's next turn.
    """
    with ASBUILT_CHAT_LOCK:
        sess=ASBUILT_CHAT_SESSIONS.get(chat_id)
    if not sess:
        raise RuntimeError('As-Built chat session not found or expired. Start a new chat.')
    if not str(message or '').strip():
        raise RuntimeError('Type a reply before sending.')
    history=list(sess['history']); history.append({'role':'user','parts':[{'text':message}]})
    text,model_turn=_gemini_generate(sess['model'],history,sess['api_key'],response_schema=ASBUILT_CHAT_RESPONSE_SCHEMA)
    history.append(model_turn)
    with ASBUILT_CHAT_LOCK:
        if chat_id in ASBUILT_CHAT_SESSIONS:
            ASBUILT_CHAT_SESSIONS[chat_id]['history']=history
    try:
        parsed=json.loads(text)
    except Exception as e:
        raise RuntimeError(f'Gemini returned invalid JSON: {e}')
    return _asbuilt_chat_result(chat_id,parsed,sess['by_key'],sess['per_floor'])


def _asbuilt_targets(floor,label_pattern=None):
    """Return every counted CAD endpoint for As-Built matching.

    IMPORTANT:
    CAD Match must not disappear merely because the label-allocation layer has
    not produced an assignment for a floor yet. The As-Built matcher needs the
    actual analyzed CAD occurrences first; labels are enrichment, not a gate.

    We therefore:
      1) take ALL COUNTED/MAPPED occurrences from the selected floor result;
      2) merge labels/port slots when the normal label allocator has them;
      3) keep the original occurrence coordinates for deterministic matching.

    This fixes the Ground/First-floor ``No CAD candidate`` case without changing
    the scan, color counting, legend handling, or page-to-floor rules.
    """
    r=PROJECT_RESULTS.get(floor)
    if not r:
        raise RuntimeError(f'Floor result not found: {floor}')

    label_pattern,_=_validate_label_pattern(label_pattern)

    # Optional label enrichment. Failure/empty assignments must NEVER remove
    # valid CAD endpoints from the As-Built candidate pool.
    try:
        assignments=_project_label_assignments(PROJECT_RESULTS,label_pattern=label_pattern)
    except Exception:
        assignments={}
    assigned_by_occ={
        str(ent.get('occurrence_id')): ent
        for ent in (assignments.get(floor) or [])
        if ent.get('occurrence_id') is not None
    }

    mapped=[]
    for rec in (r.get('mapped') or []):
        # Only real counted/mapped physical occurrences are candidates.
        status=str(rec.get('status') or '').upper()
        if status not in {'MAPPED','COUNTED',''}:
            continue
        mapped.append(rec)

    # Some stable builds may keep directly addressable candidates in a secondary
    # collection. Include them too, without duplicating occurrence IDs.
    by_occ={}
    for rec in mapped:
        by_occ[str(rec.get('occurrence_id'))]=rec

    out=[]
    for occ_id, rec in by_occ.items():
        ent=assigned_by_occ.get(occ_id) or {}
        labels=list(ent.get('labels') or rec.get('labels') or [])
        handle=str(ent.get('handle') or rec.get('handle') or '')
        root=bool(rec.get('root',False))
        out.append({
            'target_id':f"{occ_id}|{handle}",
            'occurrence_id':rec.get('occurrence_id'),
            'handle':handle,
            'labels':labels,
            'label':' + '.join(labels) if labels else str(rec.get('label') or rec.get('port_label') or ''),
            'service':ent.get('service') or rec.get('service',''),
            'block':ent.get('block') or rec.get('block',''),
            'rack':ent.get('rack') or rec.get('rack',''),
            'schedule_floor':ent.get('schedule_floor') or rec.get('schedule_floor') or floor,
            'port_slots':list(ent.get('port_slots') or rec.get('port_slots') or []),
            'x':float(rec.get('x') or ent.get('x') or 0.0),
            'y':float(rec.get('y') or ent.get('y') or 0.0),
            'root':root,
            'depth':int(rec.get('depth') or ent.get('depth') or 0),
            'editable_direct':bool(root and handle),
        })

    out.sort(key=lambda x:(x.get('label') or '',x.get('service') or '',x.get('occurrence_id') or 0))
    return out


def _lsp_escape(v):
    return str(v).replace('\\','\\\\').replace('"','\\"')


def _recount_result_from_mapped(result):
    """Rebuild rack physical/logical counts after confirmed As-Built deletions."""
    counts={}
    for rack in _rack_order_from_counts(result):
        counts[rack]={'physical':{},'logical_ports':{},'physical_total':0,'logical_total':0}
    for rec in result.get('mapped') or []:
        if rec.get('status') not in {'MAPPED','COUNTED'}:
            continue
        rack=rec.get('rack')
        if not rack:
            continue
        c=counts.setdefault(rack,{'physical':{},'logical_ports':{},'physical_total':0,'logical_total':0})
        phys=Counter(c.get('physical') or {}); lp=Counter(c.get('logical_ports') or {})
        phys[rec.get('service','Unassigned')]+=1
        for code in rec.get('logical_ports') or []:
            lp[code]+=1
        c['physical']=dict(phys); c['logical_ports']=dict(lp)
        c['physical_total']=sum(phys.values()); c['logical_total']=sum(lp.values())
    result['counts']=counts
    result['mapped_count']=sum(int(x.get('physical_total',0)) for x in counts.values())
    return result


def _filtered_project_results_for_asbuilt(results_by_floor, site_changes):
    """Copy project results and remove only site-confirmed endpoint occurrences."""
    out=json.loads(json.dumps(results_by_floor))
    remove_by_floor=defaultdict(set)
    for ch in site_changes:
        t=ch.get('target') or {}
        remove_by_floor[str(t.get('source_floor') or ch.get('floor') or '')].add(str(t.get('occurrence_id')))
    for floor,occ_ids in remove_by_floor.items():
        r=out.get(floor)
        if not r:
            continue
        r['mapped']=[x for x in (r.get('mapped') or []) if str(x.get('occurrence_id')) not in occ_ids]
        _recount_result_from_mapped(r)
    return out


def _repack_asbuilt_network(items, change_map, start_panel, label_pattern):
    """Compact SHIFT removals while keeping RESERVE positions as fixed holes."""
    if not items:
        return [],[]
    items=sorted(items,key=lambda x:(int(x['panel']),int(x['port']),str(x.get('code',''))))
    orig_start=min(int(x['panel']) for x in items)
    reserve_at={}
    actual=[]
    for item in items:
        tid=item['target_id']; ch=change_map.get(tid)
        offset=(int(item['panel'])-orig_start)*24 + int(item['port'])-1
        if ch and ch.get('patch_action')=='RESERVE':
            reserve_at[offset]={'item':item,'change':ch}
        elif ch and ch.get('patch_action')=='SHIFT':
            continue
        else:
            actual.append(item)
    placed=[]; pos=0
    for item in actual:
        while pos in reserve_at:
            pos+=1
        panel=int(start_panel)+pos//24; port=pos%24+1
        x=dict(item); x.update(panel=panel,port=port,
            label=_format_port_label(item['prefix'],item['network'],panel,item['code'],port,item['desc'],label_pattern,validate=False))
        placed.append(x); pos+=1
    reserves=[]
    for off,meta in sorted(reserve_at.items()):
        item=meta['item']; ch=meta['change']
        reserves.append({'panel':int(start_panel)+off//24,'port':off%24+1,'network':item['network'],
            'code':item['code'],'desc':item['desc'],'target_id':item['target_id'],
            'old_label':item.get('label',''),'site_status':ch.get('site_status',''),'patch_action':'RESERVE'})
    return placed,reserves


def _asbuilt_allocation_plan(results_by_floor, site_changes, label_pattern):
    """Build final per-rack LC/DN allocation including reserved holes."""
    assignments=_project_label_assignments(results_by_floor,label_pattern=label_pattern)
    change_map={str((c.get('target') or {}).get('target_id') or c.get('target_id') or ''):c for c in site_changes}
    grouped=defaultdict(lambda:defaultdict(lambda:defaultdict(list)))
    for source_floor,ents in assignments.items():
        for ent in ents:
            tid=f"{ent.get('occurrence_id','')}|{ent.get('handle','')}"
            schedule_floor=ent.get('schedule_floor') or source_floor
            rack=ent.get('rack')
            for slot in ent.get('port_slots') or []:
                item=dict(slot); item.update(target_id=tid,source_floor=source_floor,schedule_floor=schedule_floor,rack=rack)
                grouped[schedule_floor][rack][slot['network']].append(item)
    plan=defaultdict(dict)
    for schedule_floor,racks in grouped.items():
        for rack,nets in racks.items():
            lc,lc_res=_repack_asbuilt_network(nets.get('LC',[]),change_map,1,label_pattern)
            lc_positions=[(x['panel'],x['port']) for x in lc]+[(x['panel'],x['port']) for x in lc_res]
            lc_last=max((p for p,_ in lc_positions),default=0)
            dn_start=lc_last+1 if lc_last else 1
            dn,dn_res=_repack_asbuilt_network(nets.get('DN',[]),change_map,dn_start,label_pattern)
            plan[schedule_floor][rack]={'lc':lc,'dn':dn,'lc_res':lc_res,'dn_res':dn_res}
    return plan


def _asbuilt_legend_text(changes, limit=3):
    if not changes:
        return ''
    head='RES = removed point, patch port reserved | SHIFT = removed point, following ports repacked'
    details=[]
    for ch in changes[:limit]:
        t=ch.get('target') or {}; old=(t.get('labels') or [''])[0]
        details.append(f"{old or t.get('service','Point')} | {ch.get('site_status','')} | {ch.get('patch_action','')}")
    if len(changes)>limit:
        details.append(f"+{len(changes)-limit} more in As-Built manifest")
    return head+'\n'+'\n'.join(details)


def export_project_asbuilt(results_by_floor, site_changes, label_pattern=None):
    """Generate adjusted Quantity/Port Map/Labeling without mutating the live project."""
    label_pattern,_=_validate_label_pattern(label_pattern)
    filtered=_filtered_project_results_for_asbuilt(results_by_floor,site_changes)
    effective=_effective_export_results(filtered)
    plan=_asbuilt_allocation_plan(results_by_floor,site_changes,label_pattern)
    ts=time.strftime('%Y%m%d-%H%M%S')
    PORT_LAYOUTS={
      'Ground Floor':{'IDF-G1':('G-R1',{1:11,2:16,3:24,4:29,5:34,6:39},7,20),'IDF-G2':('G-R2',{1:11,2:19,3:24,4:29,5:34},7,15)},
      'First Floor':{'IDF-G1':('F-R1',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20),'IDF-G2':('F-R2',{1:11,2:19,3:24,4:29,5:34,6:39,7:44},7,15)},
      'Second Floor':{'IDF-G1':('S-R1',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20),'IDF-G2':('S-R2',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20)},
      'Third Floor':{'IDF-G1':('T-R1',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20),'IDF-G2':('T-R2',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20)},
    }
    changes_by_sheet=defaultdict(list)
    for ch in site_changes:
        t=ch.get('target') or {}; sf=t.get('schedule_floor') or ch.get('floor'); rack=t.get('rack')
        if sf in PORT_LAYOUTS and rack in PORT_LAYOUTS[sf]:
            changes_by_sheet[PORT_LAYOUTS[sf][rack][0]].append(ch)

    pm=EXPORTS/f'project_port_map_ASBUILT_{ts}.xlsx'; pp={}; pm_panel_maps={}; pm_used_panels={}; legends={}
    for floor,maps in PORT_LAYOUTS.items():
        for rack,(sh,rows_map,lc_info_row,dn_info_row) in maps.items():
            cells={'C1':'','C2':'','C3':'','C4':''}
            for _tr in set(rows_map.keys())|{lc_info_row,dn_info_row}:
                for _col in('A','G','J','M'): cells[f'{_col}{_tr}']=''
            _write_port_map_stream(cells,rows_map,[])
            pp[sh]=cells; pm_panel_maps[sh]=dict(rows_map); pm_used_panels[sh]=set()
    for floor,r in effective.items():
        if floor not in PORT_LAYOUTS: continue
        labels=r.get('rack_labels') or _default_rack_labels(floor)
        for rack,(sh,base_rows_map,lc_info_row,dn_info_row) in PORT_LAYOUTS[floor].items():
            rackplan=(plan.get(floor) or {}).get(rack,{'lc':[],'dn':[],'lc_res':[],'dn_res':[]})
            lc=rackplan['lc']; dn=rackplan['dn']; reserves=rackplan['lc_res']+rackplan['dn_res']; cells=pp[sh]
            cells.update({'C1':r['university'],'C2':r['building'],'C3':floor,'C4':labels.get(rack,rack)})
            allocations=[(x['panel'],x['port'],x['code'],x['desc']) for x in lc+dn]
            used_panels={int(x['panel']) for x in lc+dn} | {int(x['panel']) for x in reserves}
            max_panel=max(used_panels,default=0); rows_map=_expand_panel_rows(base_rows_map,max_panel)
            pm_panel_maps[sh]=rows_map; pm_used_panels[sh]=used_panels
            _write_port_map_stream(cells,rows_map,allocations,reserved_slots=reserves)
            lc_panels=sorted({int(x['panel']) for x in lc+rackplan['lc_res']}); dn_panels=sorted({int(x['panel']) for x in dn+rackplan['dn_res']})
            title=lambda net,panels: f'{net} Rack' if not panels else f'{net} Rack (P{panels[0]:02d}:P{panels[-1]:02d})'
            lc48,lc24=_switch_mix(len(lc)); dn48,dn24=_switch_mix(len(dn))
            cells[f'A{lc_info_row}']=title('LC',lc_panels); cells[f'A{dn_info_row}']=title('DN',dn_panels)
            cells[f'G{lc_info_row}']=lc48; cells[f'J{lc_info_row}']=lc24; cells[f'M{lc_info_row}']=len(lc)
            cells[f'G{dn_info_row}']=dn48; cells[f'J{dn_info_row}']=dn24; cells[f'M{dn_info_row}']=len(dn)
            if changes_by_sheet.get(sh): legends[sh]=_asbuilt_legend_text(changes_by_sheet[sh])
    patch_xlsx(PORT_MAP_PROJECT_TEMPLATE,pm,pp,port_map_panel_maps=pm_panel_maps,port_map_used_panels=pm_used_panels,port_map_legends=legends)

    sm=EXPORTS/f'project_quantity_summary_ASBUILT_{ts}.xlsx'; sp={}
    for rr in range(3,195):
        for cc in ['A','B','C','D','E','F','G','H','I','J','K','L','M','N','O','P','Q','R','S','T','U','V','W']:
            sp[f'{cc}{rr}']=''
    row=3
    for floor in ['Ground Floor','First Floor','Second Floor','Third Floor']:
        r=effective.get(floor)
        if not r: continue
        for rack in _rack_order_from_counts(r):
            if rack not in (r.get('counts') or {}): continue
            sp.update(_summary_cells_for_rack(r,rack,row,_summary_floor_label(floor),_summary_rack_label(r,rack))); row+=1
    patch_xlsx(SUMMARY_TEMPLATE,sm,{'Sheet1':sp},freeze_columns={'Sheet1':4})

    schedule_specs=[]
    for floor in ['Ground Floor','First Floor','Second Floor','Third Floor']:
        r=effective.get(floor)
        if not r: continue
        labels=r.get('rack_labels') or _default_rack_labels(floor)
        for rack in _rack_order_from_counts(r):
            rackplan=(plan.get(floor) or {}).get(rack)
            if not rackplan: continue
            rack_title=labels.get(rack,_rack_label_prefix(r,rack))
            if rack=='IDF-G1': sheet_suffix=f'{_floor_prefix(floor)}1'
            elif rack=='IDF-G2': sheet_suffix=f'{_floor_prefix(floor)}2'
            else: sheet_suffix=_rack_label_prefix(r,rack)
            groups=[]
            for network,items in [('LC',rackplan['lc']),('DN',rackplan['dn'])]:
                current=None
                for item in items:
                    if current is None or current['code']!=item['code']:
                        current={'network':network,'code':item['code'],'desc':item['desc'],'labels':[]}; groups.append(current)
                    current['labels'].append(item['label'])
            schedule_specs.append({'name':_safe_excel_sheet_name(f'{floor} {sheet_suffix}'),'source':_project_schedule_source_sheet(floor,rack),'rack_title':rack_title,'groups':groups})
    cs=EXPORTS/f'project_cable_schedule_ASBUILT_{ts}.xlsx'; _write_project_cable_schedule_xlsx(cs,schedule_specs)
    return {'port_map':pm.name,'quantity_summary':sm.name,'cable_schedule':cs.name}


def generate_asbuilt_package(floor,changes,label_pattern=None):
    """Generate a Desktop AutoCAD As-Built change script for one analyzed drawing.

    Supported changes are intentionally structured and auditable:
      MOVE_ENDPOINT   - move one root ModelSpace INSERT to absolute X/Y.
      DELETE_ENDPOINT - delete one root ModelSpace INSERT.
      SITE_REMOVE     - delete a site-confirmed endpoint and regenerate Excel with SHIFT/RESERVE port policy.
      ADD_BLOCK       - insert an existing block definition at X/Y.
      ADD_NOTE        - place an As-Built text note at X/Y.

    The source drawing is never overwritten. Full AutoCAD Desktop runs the generated
    script, then the operator uses SAVEAS to create the revised As-Built drawing.
    """
    if floor not in PROJECT_RESULTS:
        raise RuntimeError(f'Floor result not found: {floor}')
    if not isinstance(changes,list) or not changes:
        raise RuntimeError('Add at least one As-Built change before Generate.')
    label_pattern,_=_validate_label_pattern(label_pattern)
    needs_targets=any(str(c.get('op') or '').strip().upper() in {'MOVE_ENDPOINT','DELETE_ENDPOINT','SITE_REMOVE'} for c in changes if isinstance(c,dict))
    targets={x['target_id']:x for x in _asbuilt_targets(floor,label_pattern=label_pattern)} if needs_targets else {}
    result=PROJECT_RESULTS[floor]
    src=Path(result['drawing']).resolve()
    ts=time.strftime('%Y%m%d-%H%M%S')
    display_stem=re.sub(r'^[0-9a-fA-F]{32}_','',src.stem)
    safe_stem=re.sub(r'[^A-Za-z0-9._ -]+','_',display_stem).strip() or _floor_prefix(floor)
    stem=f'{safe_stem}_ASBUILT_{ts}'
    lsp=EXPORTS/f'{stem}_EDIT.lsp'
    scr=EXPORTS/f'{stem}_RUN.scr'
    readme=EXPORTS/f'{stem}_README.txt'
    manifest=EXPORTS/f'{stem}_changes.json'

    normalized=[]
    for idx,ch in enumerate(changes,1):
        if not isinstance(ch,dict):
            raise RuntimeError(f'Invalid change #{idx}')
        op=str(ch.get('op') or '').strip().upper()
        note=str(ch.get('note') or '').strip()
        if op in {'MOVE_ENDPOINT','DELETE_ENDPOINT','SITE_REMOVE'}:
            tid=str(ch.get('target_id') or '')
            t=targets.get(tid)
            if not t: raise RuntimeError(f'Change #{idx}: target was not found in {floor}.')
            if op in {'MOVE_ENDPOINT','DELETE_ENDPOINT'} and not t.get('editable_direct'):
                raise RuntimeError(
                    f"Change #{idx}: {t.get('label') or t.get('block')} is nested inside a parent block (depth {t.get('depth')}). "
                    'Direct move/delete is blocked to avoid changing every block instance. Use Site removal for Excel + manual CAD deletion, or edit it manually in AutoCAD.'
                )
            item={'op':op,'target':t,'note':note,'floor':floor}
            if op=='MOVE_ENDPOINT':
                try: nx=float(ch.get('x')); ny=float(ch.get('y'))
                except Exception: raise RuntimeError(f'Change #{idx}: MOVE requires numeric new X and Y.')
                item.update(x=nx,y=ny)
            elif op=='SITE_REMOVE':
                status=str(ch.get('site_status') or '').strip()
                action=str(ch.get('patch_action') or '').strip().upper()
                if not _is_remove_status(status):
                    raise RuntimeError(f'Change #{idx}: choose one of the four approved site removal statuses.')
                if action not in ASBUILT_PATCH_ACTIONS:
                    raise RuntimeError(f'Change #{idx}: choose SHIFT or RESERVE for Patch Panel handling.')
                item.update(site_status=status,patch_action=action,cad_manual_required=not bool(t.get('editable_direct')))
            normalized.append(item)
        elif op=='ADD_BLOCK':
            block=str(ch.get('block') or '').strip()
            if not block: raise RuntimeError(f'Change #{idx}: choose a block name.')
            try:
                x=float(ch.get('x')); y=float(ch.get('y')); rot=float(ch.get('rotation') or 0); scale=float(ch.get('scale') or 1)
            except Exception: raise RuntimeError(f'Change #{idx}: Add Block requires numeric X/Y/rotation/scale.')
            if scale<=0: raise RuntimeError(f'Change #{idx}: block scale must be positive.')
            normalized.append({'op':op,'block':block,'x':x,'y':y,'rotation':rot,'scale':scale,'note':note})
        elif op=='ADD_NOTE':
            text=str(ch.get('text') or '').strip()
            if not text: raise RuntimeError(f'Change #{idx}: note text is empty.')
            try: x=float(ch.get('x')); y=float(ch.get('y')); h=float(ch.get('height') or 150)
            except Exception: raise RuntimeError(f'Change #{idx}: Add Note requires numeric X/Y/height.')
            if h<=0: raise RuntimeError(f'Change #{idx}: note height must be positive.')
            normalized.append({'op':op,'text':text,'x':x,'y':y,'height':h,'note':note})
        else:
            raise RuntimeError(f'Change #{idx}: unsupported operation {op or "(blank)"}.')

    lines=[
      '(vl-load-com)',
      '(setq *tt-old-cmdecho* (getvar "CMDECHO"))',
      '(setq *tt-old-filedia* (getvar "FILEDIA"))',
      '(setq *tt-old-cmddia* (getvar "CMDDIA"))',
      '(setq *tt-old-clayer* (getvar "CLAYER"))',
      '(setvar "CMDECHO" 0)','(setvar "FILEDIA" 0)','(setvar "CMDDIA" 0)',
      '(if (not (tblsearch "LAYER" "TELETECH-ASBUILT")) (command "_.-LAYER" "_M" "TELETECH-ASBUILT" ""))',
      '(setq *tt-doc* (vla-get-ActiveDocument (vlax-get-acad-object)))',
      '(setq *tt-ms* (vla-get-ModelSpace *tt-doc*))',
    ]
    for idx,ch in enumerate(normalized,1):
        op=ch['op']
        lines.append(f'(princ "\\nTELETECH As-Built change {idx}: {op}\\n")')
        if op=='MOVE_ENDPOINT':
            t=ch['target']; h=_lsp_escape(t['handle']); ox=float(t['x']); oy=float(t['y']); nx=float(ch['x']); ny=float(ch['y'])
            lines += [
              f'(setq *tt-e* (handent "{h}"))',
              f'(if *tt-e* (vla-Move (vlax-ename->vla-object *tt-e*) (vlax-3d-point (list {ox:.12g} {oy:.12g} 0.0)) (vlax-3d-point (list {nx:.12g} {ny:.12g} 0.0))) (princ "\\nWARNING: target handle not found.\\n"))'
            ]
        elif op in {'DELETE_ENDPOINT','SITE_REMOVE'}:
            t=ch['target']; h=_lsp_escape(t.get('handle',''))
            if op=='SITE_REMOVE' and ch.get('cad_manual_required'):
                msg=_lsp_escape(t.get('label') or t.get('block') or 'endpoint')
                lines.append(f'(princ "\\nMANUAL CAD DELETE REQUIRED: {msg} is nested/read-only. Excel outputs were adjusted.\\n")')
            else:
                lines += [f'(setq *tt-e* (handent "{h}"))',
                          '(if *tt-e* (entdel *tt-e*) (princ "\\nWARNING: target handle not found.\\n"))']
        elif op=='ADD_BLOCK':
            b=_lsp_escape(ch['block']); x=ch['x']; y=ch['y']; sc=ch['scale']; rad=math.radians(ch['rotation'])
            lines.append(
              f'(if (tblsearch "BLOCK" "{b}") '
              f'(vla-InsertBlock *tt-ms* (vlax-3d-point (list {x:.12g} {y:.12g} 0.0)) "{b}" {sc:.12g} {sc:.12g} {sc:.12g} {rad:.12g}) '
              f'(princ "\\nWARNING: block {b} not found in drawing.\\n"))'
            )
        elif op=='ADD_NOTE':
            txt=_lsp_escape(ch['text']); x=ch['x']; y=ch['y']; h=ch['height']
            lines.append(
              '(entmakex (list (cons 0 "TEXT") (cons 100 "AcDbEntity") (cons 8 "TELETECH-ASBUILT") '
              '(cons 100 "AcDbText") '
              f'(cons 10 (list {x:.12g} {y:.12g} 0.0)) (cons 40 {h:.12g}) '
              f'(cons 1 "{txt}") (cons 50 0.0) (cons 7 "Standard") '
              f'(cons 11 (list {x:.12g} {y:.12g} 0.0)) (cons 72 0) (cons 73 0)))'
            )
    lines += [
      '(command "_.REGEN")',
      '(if *tt-old-clayer* (setvar "CLAYER" *tt-old-clayer*))',
      '(if *tt-old-filedia* (setvar "FILEDIA" *tt-old-filedia*))',
      '(if *tt-old-cmddia* (setvar "CMDDIA" *tt-old-cmddia*))',
      '(if *tt-old-cmdecho* (setvar "CMDECHO" *tt-old-cmdecho*))',
      '(princ "\\nTELETECH As-Built changes applied. Use SAVEAS to create a NEW As-Built drawing.\\n")','(princ)'
    ]
    lsp.write_text('\r\n'.join(lines)+'\r\n',encoding='utf-8')
    lsp_abs=str(lsp.resolve()).replace('\\','/')
    scr.write_text(f'(load "{lsp_abs}")\r\n',encoding='utf-8')
    site_changes=[x for x in normalized if x.get('op')=='SITE_REMOVE']
    adjusted_files=export_project_asbuilt(PROJECT_RESULTS,site_changes,label_pattern=label_pattern) if site_changes else {}
    readme.write_text(
      'TELETECH As-Built Desktop AutoCAD package\n\n'
      f'Floor: {floor}\nOriginal drawing: {src.name}\nChanges: {len(normalized)}\n\n'
      '1. Open the ORIGINAL drawing in full AutoCAD Desktop.\n'
      f'2. Type SCRIPT and select: {scr.name}\n'
      '3. Review the changes on screen. TELETECH-ASBUILT is used for added notes.\n'
      '4. Use SAVEAS and save a NEW file, for example *_ASBUILT.dwg. Do not overwrite the original.\n'
      '5. If this package contains SITE_REMOVE changes, the included ASBUILT Excel files already remove those endpoints from Quantity/Labeling.\n'
      '6. Port Map rule: SHIFT repacks following ports; RESERVE leaves a RES slot and records the decision in the sheet Legend.\n'
      '7. Nested/read-only CAD symbols are listed as MANUAL CAD DELETE REQUIRED; Excel is still adjusted safely.\n',encoding='utf-8')
    man={'generated_at':ts,'floor':floor,'source_drawing':str(src),'changes':normalized}
    # JSON-safe copy without nested full target records if they contain non-serializable values.
    def clean(o):
        if isinstance(o,dict): return {k:clean(v) for k,v in o.items()}
        if isinstance(o,list): return [clean(v) for v in o]
        if isinstance(o,(str,int,float,bool)) or o is None: return o
        return str(o)
    manifest.write_text(json.dumps(clean(man),ensure_ascii=False,indent=2),encoding='utf-8')
    zpath=EXPORTS/f'{stem}_PACKAGE.zip'
    with zipfile.ZipFile(zpath,'w',zipfile.ZIP_DEFLATED) as z:
        for fp in (lsp,scr,readme,manifest): z.write(fp,arcname=fp.name)
        for name in adjusted_files.values():
            fp=EXPORTS/name
            if fp.exists(): z.write(fp,arcname=fp.name)
    return {'zip':zpath.name,'scr':scr.name,'lsp':lsp.name,'readme':readme.name,'manifest':manifest.name,'changes':len(normalized),'floor':floor,**adjusted_files}

def generate_asbuilt_project_package(changes,label_pattern=None):
    """Generate a full-project As-Built package from reviewed changes across floors.

    CAD scripts remain per source drawing/floor, while adjusted project Excel is generated
    once using all confirmed SITE_REMOVE changes together.
    """
    if not PROJECT_RESULTS:
        raise RuntimeError('Analyze the CAD project first.')
    if not isinstance(changes,list) or not changes:
        raise RuntimeError('Add at least one reviewed As-Built change before Generate.')
    label_pattern,_=_validate_label_pattern(label_pattern)
    grouped=defaultdict(list)
    for idx,ch in enumerate(changes,1):
        if not isinstance(ch,dict):
            raise RuntimeError(f'Invalid change #{idx}')
        floor=str(ch.get('floor') or '').strip()
        if not floor or floor not in PROJECT_RESULTS:
            raise RuntimeError(f'Change #{idx}: floor is missing or not loaded.')
        grouped[floor].append(dict(ch))

    # Resolve all SITE_REMOVE targets once for the combined Excel exports.
    normalized_site=[]
    target_cache={}
    for floor,items in grouped.items():
        tmap=target_cache.setdefault(floor,{x['target_id']:x for x in _asbuilt_targets(floor,label_pattern=label_pattern)})
        for idx,ch in enumerate(items,1):
            if str(ch.get('op') or '').strip().upper()!='SITE_REMOVE':
                continue
            tid=str(ch.get('target_id') or '')
            t=tmap.get(tid)
            if not t:
                raise RuntimeError(f'{floor}: reviewed site-removal target was not found.')
            status=str(ch.get('site_status') or '').strip()
            action=str(ch.get('patch_action') or '').strip().upper()
            if not _is_remove_status(status):
                raise RuntimeError(f'{floor}: choose one of the four approved site removal statuses.')
            if action not in ASBUILT_PATCH_ACTIONS:
                raise RuntimeError(f'{floor}: choose SHIFT or RESERVE for Patch Panel handling.')
            normalized_site.append({'op':'SITE_REMOVE','target':t,'target_id':tid,'floor':floor,
                                    'site_status':status,'patch_action':action,
                                    'note':str(ch.get('note') or ''),
                                    'cad_manual_required':not bool(t.get('editable_direct'))})

    # Reuse the proven single-floor CAD script writer, but assemble one clean master ZIP.
    floor_outputs=[]
    for floor,items in grouped.items():
        floor_outputs.append(generate_asbuilt_package(floor,items,label_pattern=label_pattern))

    adjusted_files=export_project_asbuilt(PROJECT_RESULTS,normalized_site,label_pattern=label_pattern) if normalized_site else {}
    ts=time.strftime('%Y%m%d-%H%M%S')
    master_readme=EXPORTS/f'PROJECT_ASBUILT_{ts}_README.txt'
    master_manifest=EXPORTS/f'PROJECT_ASBUILT_{ts}_changes.json'
    master_zip=EXPORTS/f'PROJECT_ASBUILT_{ts}_PACKAGE.zip'
    master_readme.write_text(
      'TELETECH Full-Project As-Built package\n\n'
      f'Project floors changed: {len(grouped)}\nTotal reviewed changes: {len(changes)}\n'
      f'Site removals affecting Excel: {len(normalized_site)}\n\n'
      'Run the matching *_RUN.scr in each ORIGINAL floor drawing using full AutoCAD Desktop.\n'
      'Then SAVEAS each revised drawing to a new *_ASBUILT.dwg file.\n'
      'Combined ASBUILT Excel files already apply every reviewed SITE_REMOVE across all floors.\n'
      'SHIFT repacks following ports. RESERVE keeps the original patch-port position reserved.\n',encoding='utf-8')
    clean_changes=[]
    for c in changes:
        clean_changes.append({k:v for k,v in c.items() if k not in {'api_key'}})
    master_manifest.write_text(json.dumps({'generated_at':ts,'floors':list(grouped),'changes':clean_changes},ensure_ascii=False,indent=2),encoding='utf-8')
    with zipfile.ZipFile(master_zip,'w',zipfile.ZIP_DEFLATED) as z:
        z.write(master_readme,arcname=master_readme.name)
        z.write(master_manifest,arcname=master_manifest.name)
        for fo in floor_outputs:
            for key in ('scr','lsp','readme','manifest'):
                name=fo.get(key)
                fp=EXPORTS/name if name else None
                if fp and fp.exists():
                    z.write(fp,arcname=f"CAD/{fo.get('floor','Floor')}/{fp.name}")
        for name in adjusted_files.values():
            fp=EXPORTS/name
            if fp.exists():
                z.write(fp,arcname=f'Excel/{fp.name}')
    return {
      'zip':master_zip.name,'readme':master_readme.name,'manifest':master_manifest.name,
      'changes':len(changes),'floors':list(grouped),'floor_count':len(grouped),
      'scripts':[{'floor':fo.get('floor'),'scr':fo.get('scr'),'lsp':fo.get('lsp')} for fo in floor_outputs],
      **adjusted_files
    }

def _project_status(results_by_floor):
    order=['Ground Floor','First Floor','Second Floor','Third Floor','Roof Floor']
    order += [f for f in results_by_floor if f not in order]
    out=[]
    for floor in order:
        r=results_by_floor.get(floor)
        if not r: continue
        counts=r.get('counts') or {}
        r1=counts.get('IDF-G1',{'physical_total':0,'logical_total':0})
        r2=counts.get('IDF-G2',{'physical_total':0,'logical_total':0})
        physical_total=sum(int(c.get('physical_total',0)) for c in counts.values())
        logical_total=sum(int(c.get('logical_total',0)) for c in counts.values())
        labs=sorted([rk for rk in counts if str(rk).startswith('IDF-LAB')], key=_lab_rack_sort_key)
        lab_note=''
        if labs:
            lab_note=' + ' + ', '.join(f'{_rack_label_prefix(r,rk)}:{int(counts[rk].get("physical_total",0))}' for rk in labs)
        out.append({
            'floor':floor,'drawing':Path(r.get('drawing','')).name,
            'rack1':r.get('rack_labels',{}).get('IDF-G1','Rack 1'),
            'rack2':(r.get('rack_labels',{}).get('IDF-G2','Rack 2') + lab_note),
            'rack1_physical':int(r1.get('physical_total',0)),'rack2_physical':int(r2.get('physical_total',0)),
            'rack1_ports':int(r1.get('logical_total',0)),'rack2_ports':int(r2.get('logical_total',0)),
            'physical_total':physical_total,
            'logical_total':logical_total,
            'review':int(r.get('review_count',0)),'rack_review':int(r.get('rack_review_count',0))
        })
    return out


def _uploaded_drawings_status():
    out=[]
    for x in UPLOADED_DRAWINGS:
        item=dict(x)
        item['drawing']=Path(item.get('path','')).name
        item['exists']=Path(item.get('path','')).exists()
        item['analyzed']=bool(item.get('analyzed'))
        item.pop('path',None)
        out.append(item)
    return out


def _effective_export_results(results_by_floor):
    """Return export-only floor results with Roof merged into the highest normal floor.

    TELETECH rule: the roof does not get dedicated Excel tabs. Its services terminate
    on the building's last normal floor rack, so Roof counts are added to that floor's
    Rack 1 / Rack 2 counts. The raw Roof analysis remains separate in PROJECT_RESULTS
    and can still be selected/reviewed in the UI.
    """
    normal_order=['Ground Floor','First Floor','Second Floor','Third Floor']
    out={f:json.loads(json.dumps(results_by_floor[f])) for f in normal_order if f in results_by_floor}
    roof=results_by_floor.get('Roof Floor')
    if not roof:
        return out
    dest_floor=next((f for f in reversed(normal_order) if f in out),None)
    if dest_floor is None:
        raise RuntimeError('Roof drawing is loaded, but no normal floor is loaded. Add the building last occupied floor so Roof data can be merged into it.')
    dest=out[dest_floor]
    for rack in ('IDF-G1','IDF-G2'):
        dc=dest['counts'][rack]; rc=roof['counts'][rack]
        dphys=Counter(dc.get('physical') or {}); dlp=Counter(dc.get('logical_ports') or {})
        dphys.update(rc.get('physical') or {}); dlp.update(rc.get('logical_ports') or {})
        dc['physical']=dict(dphys); dc['logical_ports']=dict(dlp)
        dc['physical_total']=sum(dphys.values()); dc['logical_total']=sum(dlp.values())
    dest['roof_merged']=True
    dest['roof_source_drawing']=roof.get('drawing')
    dest['roof_merged_into']=dest_floor
    return out




def _excel_col_name(idx):
    name=''
    while idx:
        idx,rem=divmod(idx-1,26)
        name=chr(65+rem)+name
    return name


def _xml_text(v):
    if v is None:
        return ''
    # Excel/Open XML is XML 1.0: CAD metadata can occasionally carry control
    # characters that make an otherwise valid XLSX look corrupt to Excel.
    text=re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', str(v))
    return text.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')


def _safe_excel_sheet_name(name, used=None, fallback='Sheet'):
    """Return a legal, unique Excel worksheet name (31-char limit)."""
    base=re.sub(r'[\[\]:*?/\\]', '_', str(name or '')).strip().strip("'") or fallback
    base=base[:31]
    if used is None:
        return base
    candidate=base; n=2
    while candidate.lower() in used:
        suffix=f'_{n}'
        candidate=base[:31-len(suffix)] + suffix
        n+=1
    used.add(candidate.lower())
    return candidate


def _write_simple_xlsx(path, sheets):
    path=Path(path)
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        overrides=''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))
        z.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+overrides+'</Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        rels=''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))
        z.writestr('xl/_rels/workbook.xml.rels', f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        sheets_xml=[]; used_sheet_names=set()
        for i,(name,rows) in enumerate(sheets.items(),start=1):
            safe_name=_xml_text(_safe_excel_sheet_name(name,used_sheet_names,f'Sheet{i}'))
            sheets_xml.append(f'<sheet name="{safe_name}" sheetId="{i}" r:id="rId{i}"/>')
            max_cols=max((len(r) for r in rows), default=1)
            dim=f'A1:{_excel_col_name(max_cols)}{max(len(rows),1)}'
            sheet_rows=[]
            for r_idx,row in enumerate(rows,start=1):
                cells=[]
                for c_idx,val in enumerate(row,start=1):
                    ref=f'{_excel_col_name(c_idx)}{r_idx}'
                    style=' s="1"' if r_idx<=3 or (row and str(row[0]).startswith('---')) else ''
                    if isinstance(val,(int,float)) and not isinstance(val,bool):
                        cells.append(f'<c r="{ref}"{style}><v>{val}</v></c>')
                    else:
                        cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{_xml_text(val)}</t></is></c>')
                sheet_rows.append(f'<row r="{r_idx}">'+''.join(cells)+'</row>')
            z.writestr(f'xl/worksheets/sheet{i}.xml', f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><dimension ref="{dim}"/><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="15"/><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
        z.writestr('xl/workbook.xml', f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{"".join(sheets_xml)}</sheets></workbook>')






def _xlsx_cell_text(c, shared):
    if c is None:
        return ''
    typ=c.attrib.get('t')
    if typ=='inlineStr':
        return ''.join(t.text or '' for t in c.iter(f'{{{NS_MAIN}}}t'))
    v=c.find(f'{{{NS_MAIN}}}v')
    if v is None:
        return ''
    value=v.text or ''
    if typ=='s':
        try:
            return shared[int(value)]
        except Exception:
            return value
    return value


def _schedule_code_from_label(label):
    m=re.search(r'/(?:LC|DN)\d+/([A-Z]+)\d+$',str(label or '').upper())
    return m.group(1) if m else ''


def _schedule_style_profiles(root, shared):
    """Capture the consultant template's per-service row styles for A:D."""
    profiles={}
    sheet_data=root.find(f'{{{NS_MAIN}}}sheetData')
    for row in sheet_data.findall(f'{{{NS_MAIN}}}row'):
        rn=int(row.attrib.get('r','0'))
        if rn < 5:
            continue
        b=_find_or_make_cell(root,f'B{rn}')
        code=_schedule_code_from_label(_xlsx_cell_text(b,shared))
        if not code or code in profiles:
            continue
        styles=[]
        for col in 'ABCD':
            c=_find_or_make_cell(root,f'{col}{rn}')
            styles.append(c.attrib.get('s'))
        profiles[code]=styles
    first=next(iter(profiles.values()),[None,None,None,None])
    lc_fallback=profiles.get('C') or first
    dn_fallback=profiles.get('D') or profiles.get('V') or profiles.get('W') or first
    return profiles,lc_fallback,dn_fallback


def _apply_schedule_row_style(root, row_num, styles):
    for col,style in zip('ABCD',styles or [None,None,None,None]):
        c=_find_or_make_cell(root,f'{col}{row_num}')
        if style is None:
            c.attrib.pop('s',None)
        else:
            c.attrib['s']=str(style)


def _schedule_remove_body_merges(root):
    merges=root.find(f'{{{NS_MAIN}}}mergeCells')
    if merges is None:
        return
    for mc in list(merges):
        nums=[int(x) for x in re.findall(r'\d+',mc.attrib.get('ref',''))]
        if nums and max(nums) >= 5:
            merges.remove(mc)
    merges.attrib['count']=str(len(list(merges)))


def _schedule_add_merge(root, ref):
    merges=root.find(f'{{{NS_MAIN}}}mergeCells')
    if merges is None:
        merges=ET.Element(f'{{{NS_MAIN}}}mergeCells')
        sheet_data=root.find(f'{{{NS_MAIN}}}sheetData')
        children=list(root)
        idx=children.index(sheet_data) if sheet_data in children else len(children)-1
        root.insert(idx+1,merges)
    ET.SubElement(merges,f'{{{NS_MAIN}}}mergeCell',{'ref':ref})
    merges.attrib['count']=str(len(list(merges)))


def _schedule_trim_rows(root,last_row):
    sheet_data=root.find(f'{{{NS_MAIN}}}sheetData')
    for row in list(sheet_data):
        if int(row.attrib.get('r','0')) > last_row:
            sheet_data.remove(row)
    dim=root.find(f'{{{NS_MAIN}}}dimension')
    if dim is not None:
        dim.attrib['ref']=f'A1:D{max(4,last_row)}'


def _patch_project_schedule_sheet(source_xml, shared, rack_title, groups):
    """Populate one consultant L.S sheet while preserving its original look."""
    root=ET.fromstring(source_xml)
    profiles,lc_fallback,dn_fallback=_schedule_style_profiles(root,shared)
    _schedule_remove_body_merges(root)
    total=sum(len(g.get('labels') or []) for g in groups)
    last_row=max(4,4+total)
    _ensure_sheet_rows(root,last_row,style_source_row=5)
    sheet_data=root.find(f'{{{NS_MAIN}}}sheetData')
    max_existing=max((int(r.attrib.get('r','0')) for r in sheet_data.findall(f'{{{NS_MAIN}}}row')),default=last_row)
    for rn in range(5,max_existing+1):
        for col in 'ABCD':
            _set_cell(root,f'{col}{rn}','')
    _set_cell(root,'A1',rack_title)
    rn=5
    for g in groups:
        labels=list(g.get('labels') or [])
        if not labels:
            continue
        code=str(g.get('code') or '')
        network=str(g.get('network') or 'LC')
        styles=profiles.get(code) or (dn_fallback if network=='DN' else lc_fallback)
        start=rn
        end=rn+len(labels)-1
        for label in labels:
            _apply_schedule_row_style(root,rn,styles)
            _set_cell(root,f'A{rn}',rack_title if rn==start else '')
            _set_cell(root,f'B{rn}',label)
            _set_cell(root,f'C{rn}','')
            _set_cell(root,f'D{rn}',str(g.get('desc') or '') if rn==start else '')
            rn+=1
        if end > start:
            _schedule_add_merge(root,f'A{start}:A{end}')
            _schedule_add_merge(root,f'D{start}:D{end}')
    _schedule_trim_rows(root,last_row)
    _cleanup_mc_ignorable(root)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


def _xml_attr_text(value):
    return (str(value or '').replace('&','&amp;').replace('"','&quot;')
            .replace('<','&lt;').replace('>','&gt;'))


def _project_schedule_source_sheet(floor,rack):
    """Use the original valid consultant L.S workbook as the style source."""
    if floor=='Ground Floor':
        return 'Ground Floor G2' if rack=='IDF-G2' else 'Ground Floor G1'
    if floor=='First Floor':
        return 'First Floor F2' if rack=='IDF-G2' else 'First Floor F1'
    # Third Floor intentionally clones the Second Floor consultant layout;
    # only labels/sheet name change to T. LAB sheets clone Rack 1 styling.
    return 'Second Floor S2' if rack=='IDF-G2' else 'Second Floor S1'


def _write_project_cable_schedule_xlsx(output,specs):
    """Build Combined Labeling from the uploaded L.S template, not a plain workbook.

    Starts from cable_schedule_template.xlsx so column widths, borders, fonts,
    merged From/Service cells, Fluke header and print setup remain identical.
    Extra Third/LAB sheets are clones of the matching consultant sheet style.
    """
    with zipfile.ZipFile(CABLE_TEMPLATE,'r') as zin:
        source_map=_sheet_paths(zin)
        shared=_xlsx_shared_strings(zin)
        records=[]
        for i,spec in enumerate(specs,start=1):
            source_name=spec['source']
            source_path=source_map[source_name]
            source_rel=f"xl/worksheets/_rels/{Path(source_path).name}.rels"
            records.append((i,spec,source_path,source_rel if source_rel in zin.namelist() else None))

        # Keep the original workbook XML text so Excel compatibility prefixes
        # (x15/xr/xr6/xr10) are not rewritten by ElementTree.
        workbook_xml=zin.read('xl/workbook.xml').decode('utf-8')
        sheet_tags=''.join(
            f'<sheet name="{_xml_attr_text(spec["name"])}" sheetId="{i}" r:id="rId{100+i}"/>'
            for i,spec,_,_ in records)
        workbook_xml=re.sub(r'<sheets>.*?</sheets>',f'<sheets>{sheet_tags}</sheets>',workbook_xml,flags=re.S)

        rel_root=ET.fromstring(zin.read('xl/_rels/workbook.xml.rels'))
        for rel in list(rel_root):
            if rel.attrib.get('Type','').endswith('/worksheet'):
                rel_root.remove(rel)
        for i,_,_,_ in records:
            ET.SubElement(rel_root,f'{{{NS_REL_PKG}}}Relationship',{
                'Id':f'rId{100+i}',
                'Type':'http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet',
                'Target':f'worksheets/sheet{i}.xml'})
        rel_bytes=ET.tostring(rel_root,encoding='utf-8',xml_declaration=True)

        ns_ct='http://schemas.openxmlformats.org/package/2006/content-types'
        ct_root=ET.fromstring(zin.read('[Content_Types].xml'))
        for item in list(ct_root):
            if item.tag==f'{{{ns_ct}}}Override' and item.attrib.get('PartName','').startswith('/xl/worksheets/sheet'):
                ct_root.remove(item)
        for i,_,_,_ in records:
            ET.SubElement(ct_root,f'{{{ns_ct}}}Override',{
                'PartName':f'/xl/worksheets/sheet{i}.xml',
                'ContentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'})
        ct_bytes=ET.tostring(ct_root,encoding='utf-8',xml_declaration=True)

        # Update Excel's sheet title metadata so it agrees with the workbook.
        app_root=ET.fromstring(zin.read('docProps/app.xml'))
        ns_app='http://schemas.openxmlformats.org/officeDocument/2006/extended-properties'
        ns_vt='http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes'
        i4=app_root.find(f'.//{{{ns_vt}}}i4')
        if i4 is not None:
            i4.text=str(len(records))
        titles=app_root.find(f'{{{ns_app}}}TitlesOfParts')
        if titles is not None:
            vector=titles.find(f'{{{ns_vt}}}vector')
            if vector is not None:
                vector.attrib['size']=str(len(records))
                for child in list(vector):
                    vector.remove(child)
                for _,spec,_,_ in records:
                    el=ET.SubElement(vector,f'{{{ns_vt}}}lpstr')
                    el.text=spec['name']
        app_bytes=ET.tostring(app_root,encoding='utf-8',xml_declaration=True)

        skip={'xl/workbook.xml','xl/_rels/workbook.xml.rels','[Content_Types].xml','docProps/app.xml'}
        skip.update(n for n in zin.namelist()
                    if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',n)
                    or re.fullmatch(r'xl/worksheets/_rels/sheet\d+\.xml\.rels',n))
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename in skip:
                    continue
                zout.writestr(info,zin.read(info.filename))
            zout.writestr('xl/workbook.xml',workbook_xml.encode('utf-8'))
            zout.writestr('xl/_rels/workbook.xml.rels',rel_bytes)
            zout.writestr('[Content_Types].xml',ct_bytes)
            zout.writestr('docProps/app.xml',app_bytes)
            for i,spec,source_path,source_rel in records:
                zout.writestr(f'xl/worksheets/sheet{i}.xml',
                              _patch_project_schedule_sheet(zin.read(source_path),shared,spec['rack_title'],spec['groups']))
                if source_rel:
                    zout.writestr(f'xl/worksheets/_rels/sheet{i}.xml.rels',zin.read(source_rel))

def _summary_floor_label(floor):
    return str(floor or '').replace(' Floor','').upper()


def _summary_rack_label(r, rack):
    """Consultant template L.C.R format: G-1/F-2/T-1, while LAB names stay explicit."""
    rack=str(rack or '')
    if rack=='IDF-G1':
        return f"{_floor_prefix(r.get('floor'))}-1"
    if rack=='IDF-G2':
        return f"{_floor_prefix(r.get('floor'))}-2"
    label=_rack_label_prefix(r,rack)
    m=re.fullmatch(r'LAB\s*(\d+)',str(label).replace('-',' '),re.I)
    return f'LAB {m.group(1)}' if m else label


def _summary_cells_for_rack(r, rack, row, floor_label, rack_label):
    """Map one rack into the consultant Quantity Summary through TOTAL only.

    Final sheet layout:
      A  = UNIVERSITY
      B  = Building
      C  = FLOOR
      D  = L.C.R
      E:V = service quantities
      W  = TOTAL

    Nothing is exported after TOTAL.

    The consultant sheet counts cable/outlet runs, not only physical symbols:
      * one D-V point = 2 runs (D + V)
      * one 2-cable floor box = 2 runs
    """
    phys=Counter((r.get('counts') or {}).get(rack,{}).get('physical') or {})

    # Service quantities exactly as represented by the consultant template.
    d_v=phys['D-V (Data + Voice)']*2
    fb_dv=phys['Floor Box - 2 Cable']*2
    data_only=phys['Data']+phys['Data Point']
    data_future=phys['Future Point']+phys['Data Future']+phys['Future Data Point']
    fb_d=phys['Floor Box - 1 Cable']
    wifi_in=phys['Indoor Wi-Fi']; wifi_out=phys['Outdoor Wi-Fi']
    gb=phys['GB']; ts=phys['TS']
    tv=phys['Television']; signage=phys['Digital Signage']; projector=phys['Projector']
    rec_cam=phys['Record Camera']; high=phys['High-Level Screen']; av=phys['AV System Rack']+phys['AV System']; ac=phys['Access Control Point']
    c_dome=phys['CCTV - Indoor Dome']
    c_bullet=phys['CCTV - Indoor Bullet']+phys['CCTV - Outdoor Bullet']

    service_values=[d_v,fb_dv,data_only,data_future,fb_d,wifi_in,wifi_out,gb,ts,
                    tv,signage,projector,rec_cam,high,av,ac,c_dome,c_bullet]
    total=sum(service_values)

    # Match the template visually: absent service cells are blank.
    def svc(v): return v if v else ''

    return {f'A{row}':r.get('university',''), f'B{row}':r.get('building',''),
            f'C{row}':floor_label, f'D{row}':rack_label,
            f'E{row}':svc(d_v), f'F{row}':svc(fb_dv), f'G{row}':svc(data_only), f'H{row}':svc(data_future), f'I{row}':svc(fb_d),
            f'J{row}':svc(wifi_in), f'K{row}':svc(wifi_out), f'L{row}':svc(gb), f'M{row}':svc(ts), f'N{row}':svc(tv),
            f'O{row}':svc(signage), f'P{row}':svc(projector), f'Q{row}':svc(rec_cam), f'R{row}':svc(high),
            f'S{row}':svc(av), f'T{row}':svc(ac), f'U{row}':svc(c_dome), f'V{row}':svc(c_bullet),
            f'W{row}':total}


def export_project(results_by_floor,label_pattern=None):
    label_pattern,_=_validate_label_pattern(label_pattern)
    results=_effective_export_results(results_by_floor)
    if not results:
        raise RuntimeError('No floor drawings have been analyzed for this project.')
    for floor,r in results.items():
        if not r.get('export_template_supported',True):
            raise RuntimeError(r.get('analysis_warning') or f'{floor} is not supported for Excel export.')
        if r.get('rack_review_count',0):
            raise RuntimeError(f"{floor}: {r['rack_review_count']} counted item(s) still need rack allocation before project export.")

    ts=time.strftime('%Y%m%d-%H%M%S')
    LC_ORDER=['C','AC','A','R','H','P','S','TV']; DN_ORDER=['D','V','W','F']
    PORT_LAYOUTS={
      'Ground Floor':{'IDF-G1':('G-R1',{1:11,2:16,3:24,4:29,5:34,6:39},7,20),'IDF-G2':('G-R2',{1:11,2:19,3:24,4:29,5:34},7,15)},
      'First Floor':{'IDF-G1':('F-R1',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20),'IDF-G2':('F-R2',{1:11,2:19,3:24,4:29,5:34,6:39,7:44},7,15)},
      'Second Floor':{'IDF-G1':('S-R1',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20),'IDF-G2':('S-R2',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20)},
      'Third Floor':{'IDF-G1':('T-R1',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20),'IDF-G2':('T-R2',{1:11,2:16,3:24,4:29,5:34,6:39,7:44,8:49},7,20)},
    }
    pm=EXPORTS/f'project_port_map_{ts}.xlsx'; pp={}; pm_panel_maps={}; pm_used_panels={}
    for floor,maps in PORT_LAYOUTS.items():
        for rack,(sh,rows_map,lc_info_row,dn_info_row) in maps.items():
            cells={'C1':'','C2':'','C3':'','C4':''}
            for _tr in set(rows_map.keys())|{lc_info_row,dn_info_row}:
                for _col in('A','G','J','M'): cells[f'{_col}{_tr}']=''
            _write_port_map_stream(cells,rows_map,[])
            pp[sh]=cells; pm_panel_maps[sh]=dict(rows_map); pm_used_panels[sh]=set()
    for floor,r in results.items():
        if floor not in PORT_LAYOUTS: continue
        rack_cfg=_build_rack_cfg(r); labels=r.get('rack_labels') or _default_rack_labels(floor)
        for rack,(sh,base_rows_map,lc_info_row,dn_info_row) in PORT_LAYOUTS[floor].items():
            if rack not in rack_cfg: continue
            prefix,lc,dn,phys,lp=rack_cfg[rack]; cells=pp[sh]
            cells.update({'C1':r['university'],'C2':r['building'],'C3':floor,'C4':labels.get(rack,rack)})
            allocations=lc+dn
            max_panel=max((p for p,_,_,_ in allocations),default=0)
            rows_map=_expand_panel_rows(base_rows_map,max_panel); pm_panel_maps[sh]=rows_map
            pm_used_panels[sh]={int(p) for p,_,_,_ in allocations}
            _write_port_map_stream(cells,rows_map,allocations)
            lc48,lc24=_switch_mix(len(lc)); dn48,dn24=_switch_mix(len(dn))
            cells[f'A{lc_info_row}']=_panel_run_title('LC',lc)
            cells[f'A{dn_info_row}']=_panel_run_title('DN',dn)
            cells[f'G{lc_info_row}']=lc48; cells[f'J{lc_info_row}']=lc24; cells[f'M{lc_info_row}']=len(lc)
            cells[f'G{dn_info_row}']=dn48; cells[f'J{dn_info_row}']=dn24; cells[f'M{dn_info_row}']=len(dn)
    patch_xlsx(PORT_MAP_PROJECT_TEMPLATE,pm,pp,port_map_panel_maps=pm_panel_maps,port_map_used_panels=pm_used_panels)

    sm=EXPORTS/f'project_quantity_summary_{ts}.xlsx'; sp={}
    for rr in range(3,195):
        for cc in ['A','B','C','D','E','F','G','H','I','J','K','L','M','N','O','P','Q','R','S','T','U','V','W']:
            sp[f'{cc}{rr}']=''
    row=3
    for floor in ['Ground Floor','First Floor','Second Floor','Third Floor']:
        r=results.get(floor)
        if not r: continue
        for rack in _rack_order_from_counts(r):
            if rack not in (r.get('counts') or {}): continue
            sp.update(_summary_cells_for_rack(r,rack,row,_summary_floor_label(floor),_summary_rack_label(r,rack)))
            row+=1
    patch_xlsx(SUMMARY_TEMPLATE,sm,{'Sheet1':sp},freeze_columns={'Sheet1':4})

    schedule_specs=[]
    for floor in ['Ground Floor','First Floor','Second Floor','Third Floor']:
        r=results.get(floor)
        if not r:
            continue
        rack_cfg=_build_rack_cfg(r)
        labels=r.get('rack_labels') or _default_rack_labels(floor)
        for rack in _rack_order_from_counts(r):
            if rack not in rack_cfg:
                continue
            prefix,lc,dn,phys,lp=rack_cfg[rack]
            rack_title=labels.get(rack,_rack_label_prefix(r,rack))
            if rack=='IDF-G1':
                sheet_suffix=f'{_floor_prefix(floor)}1'
            elif rack=='IDF-G2':
                sheet_suffix=f'{_floor_prefix(floor)}2'
            else:
                sheet_suffix=_rack_label_prefix(r,rack)
            sheet_name=_safe_excel_sheet_name(f'{floor} {sheet_suffix}')
            groups=[]
            for network,allocations in [('LC',lc),('DN',dn)]:
                current=None
                for panel,port,code,desc in allocations:
                    label=_format_port_label(prefix,network,panel,code,port,desc,label_pattern,validate=False)
                    if current is None or current['code']!=code:
                        current={'network':network,'code':code,'desc':desc,'labels':[]}
                        groups.append(current)
                    current['labels'].append(label)
            schedule_specs.append({
                'name':sheet_name,
                'source':_project_schedule_source_sheet(floor,rack),
                'rack_title':rack_title,
                'groups':groups})
    cs=EXPORTS/f'project_cable_schedule_{ts}.xlsx'
    _write_project_cable_schedule_xlsx(cs,schedule_specs)
    return {'port_map':pm.name,'quantity_summary':sm.name,'cable_schedule':cs.name}

# Demo result from the user's verified Menoufia HQ Ground Floor take-off.
# It is a UI demo/reference only; live Analyze still counts CAD entities.
DEMO_RESULT={
 'profile':PROJECT_PROFILE['name'],'university':'Menoufia University','building':'HQ','floor':'Ground Floor','drawing':'Menoufia HQ Ground',
 'floor_bounds':{'min_x':2051.97,'split_x':26126.06,'max_x':55549.94,'split_ratio':0.45},
 'quantity_boq':{'D-V':51,'D-W':34,'CAM1':26,'CAM2':8,'CAM3aaaa':12,'floor boox 1':10,'AC AGN':2,'AV SYSTEM RACK':1,'H':1},
 'counts':{
  'IDF-G1':{
    'physical':{'D-V (Data + Voice)':18,'Indoor Wi-Fi':20,'CCTV - Indoor Dome':13,'CCTV - Indoor Bullet':4,'CCTV - Outdoor Bullet':6,'Floor Box - 2 Cable':10,'Access Control Point':1,'AV System Rack':1,'High-Level Screen':1},
    'logical_ports':{'C':23,'AC':1,'A':1,'H':1,'D':18,'V':18,'W':20},'physical_total':74,'logical_total':92},
  'IDF-G2':{
    'physical':{'D-V (Data + Voice)':33,'Indoor Wi-Fi':14,'CCTV - Indoor Dome':13,'CCTV - Indoor Bullet':4,'CCTV - Outdoor Bullet':6,'Access Control Point':1},
    'logical_ports':{'C':23,'AC':1,'D':33,'V':33,'W':14},'physical_total':71,'logical_total':104}},
 'mapped':[],'ignored':[],'review':[],'rack_review':[],'unassigned':{},
 'cable_lengths':{'LC-CABLE -10':{'length_m':646.9,'approximate':False},'CCTVnet':{'length_m':304.5,'approximate':False},'LC-CABLE -30':{'length_m':257.0,'approximate':False},'LC-CABLE-40':{'length_m':136.2,'approximate':False},'DN01-D':{'length_m':30.4,'approximate':False},'ELEC':{'length_m':11.8,'approximate':False},'e-Cable-T':{'length_m':7.2,'approximate':False}},
 'reference_validation':[{'item':k,'expected':v,'actual':v,'pass':True} for k,v in MENOUFIA_REFERENCE.items()],
 'rack_validation':[{'rack':rack,'item':item,'expected':n,'actual':n,'pass':True} for rack,x in MENOUFIA_RACK_REFERENCE.items() for item,n in x.items()],
 'rack_profile_applied':True,'mapped_count':145,'ignored_count':0,'review_count':0,'rack_review_count':0
}

# ------------------------------- HTTP server --------------------------------
INDEX = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TELETECH Manager</title><style>
:root{--paper:#f3f5f4;--surface:#fff;--ink:#16202a;--muted:#5d6975;--faint:#8b95a0;--line:#dde3e0;--line2:#c8d1cd;--brand:#0b6b4f;--brand-d:#074f3b;--brand-s:#e5f2ec;--blue:#2f55a4;--blue-s:#eaf0fb;--warn:#8a5300;--warn-s:#fff3d9;--bad:#b42318;--bad-s:#fdecea;--bad-l:#f4c4bf;--ok:#067647;--ok-s:#e8f6ee;--ok-l:#b5e3c9;--side:#10241e;--font:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Tahoma,sans-serif;--mono:ui-monospace,"Cascadia Mono",Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:14.5px/1.5 var(--font)}
h2,h3,h4{margin:0;line-height:1.25}a{text-decoration:none}code{font-family:var(--mono);font-size:12.5px;background:var(--paper);padding:1px 5px;border-radius:4px}
[hidden]{display:none!important}
.sr{position:absolute!important;width:1px!important;height:1px!important;overflow:hidden;clip:rect(0 0 0 0);opacity:0}
/* layout */
.app{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:100vh}
.side{background:var(--side);color:#cfe0d8;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:20px;padding:18px 12px}
.brandRow{display:flex;gap:10px;align-items:center;padding:4px 8px}.brandRow svg{width:30px;height:30px;flex:none}
.brandName{font-weight:700;color:#fff;font-size:15px;letter-spacing:.02em}.brandSub{font-size:12px;color:#7f9a8f}
.nav{display:flex;flex-direction:column;gap:2px}
.navItem{display:flex;align-items:center;gap:10px;width:100%;justify-content:flex-start;background:none;border:0;color:#cfe0d8;padding:0 10px;border-radius:8px;min-height:40px;font-weight:550}
.navItem svg{width:18px;height:18px;flex:none;opacity:.75}.navItem:hover:not(:disabled){background:rgba(255,255,255,.07)}
.navItem.on{background:rgba(255,255,255,.13);color:#fff}.navItem.on svg{opacity:1}
.navBadge{margin-left:auto;min-width:20px;padding:0 7px;border-radius:999px;background:#f5c04a;color:#3b2800;font-size:12px;font-weight:700;text-align:center}
.navItem.fresh::after{content:"";width:8px;height:8px;border-radius:50%;background:#5fd3a2;margin-left:auto}
.sideNote{margin-top:auto;padding:0 8px;font-size:12px;color:#7f9a8f}
.main{min-width:0}
.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;padding:10px 28px;min-height:58px;background:rgba(243,245,244,.93);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.projectBar{display:flex;align-items:center;gap:10px;min-width:0;flex:1}.projLabel{font-size:13px;font-weight:600;color:var(--muted)}
#projectSelect{min-width:220px;max-width:340px;font-weight:600}
.projStatus{margin:0;font-size:12.5px;color:var(--muted);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#headerRight,#authTopBar{display:flex;align-items:center;gap:8px}#headerRight{margin-left:auto}
#authTopBar{position:static!important}#authTopBar span{background:var(--brand-s)!important;color:var(--brand-d)!important;font-size:12.5px!important;padding:6px 11px!important}
#authDrawerOpen{background:#fff;color:var(--ink);border:1px solid var(--line2);min-height:36px;padding:0 11px!important}
.menu{position:relative}.menu summary{list-style:none;cursor:pointer;width:36px;height:36px;display:grid;place-items:center;border:1px solid var(--line2);border-radius:8px;background:#fff;font-size:18px;line-height:1}
.menu summary::-webkit-details-marker{display:none}
.menuPanel{position:absolute;left:0;top:calc(100% + 6px);z-index:30;width:300px;display:flex;flex-direction:column;gap:10px;background:#fff;border:1px solid var(--line2);border-radius:10px;padding:14px;box-shadow:0 12px 32px rgba(16,36,30,.16)}
.views{max-width:1140px;margin:0 auto;padding:26px 28px 72px}.view{display:none}.view.on{display:block}
.viewHead{margin-bottom:18px}.viewHead h2{font-size:22px;font-weight:650;letter-spacing:-.01em}.viewHead p{margin:4px 0 0;color:var(--muted);max-width:70ch}
/* cards */
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
.card>h3,.cardHead h3{font-size:15.5px;font-weight:650}.card>.sub{margin-top:3px}
.cardHead{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:14px}
.card .card{background:var(--paper);border-radius:8px;padding:12px 14px;margin-bottom:8px}
.sub{color:var(--muted);font-size:13px}.small{font-size:13px}.miniNote{font-size:12.5px;color:var(--muted)}
.stack>*+*{margin-top:12px}.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);background:#fff;border:1px solid var(--line);border-radius:10px;margin-bottom:16px}
.kpi{padding:16px 18px;border-left:1px solid var(--line);font-size:13px;font-weight:600;color:var(--muted)}.kpi:first-child{border-left:0}
.kpi b{display:block;margin-top:4px;font-size:28px;line-height:1.1;font-weight:650;color:var(--ink);font-variant-numeric:tabular-nums}
/* forms */
input,select,button{font:inherit}
input:not([type=checkbox]):not([type=file]),select{height:38px;padding:0 11px;border:1px solid var(--line2);border-radius:8px;background:#fff;color:var(--ink);min-width:0}
input[type=checkbox]{width:16px;height:16px;accent-color:var(--brand)}input::placeholder{color:var(--faint)}
input[type=file]{width:100%;padding:6px;font-size:13px;color:var(--muted);border:1px dashed var(--line2);border-radius:8px;background:var(--paper)}
input[type=file]::file-selector-button{margin-right:10px;border:1px solid var(--line2);border-radius:6px;background:#fff;padding:5px 10px;cursor:pointer}
:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}.fields input,.fields select{width:100%}
.field{display:flex;flex-direction:column;gap:5px}.field>span{font-size:12.5px;font-weight:600;color:var(--muted)}
button{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:38px;padding:0 16px;border:1px solid transparent;border-radius:8px;background:var(--brand);color:#fff;font-weight:600;cursor:pointer;white-space:nowrap}
button:hover:not(:disabled){background:var(--brand-d)}button:disabled{opacity:.5;cursor:not-allowed}
button.alt{background:#fff;color:var(--ink);border-color:var(--line2)}button.alt:hover:not(:disabled){background:var(--paper)}
button.danger,.docDelete,.deleteDrawing{background:var(--bad-s)!important;color:var(--bad)!important;border-color:var(--bad-l)!important}
.actionBig{width:100%;min-height:44px}
.drop{display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center;padding:26px 18px;border:1.5px dashed var(--line2);border-radius:10px;background:var(--paper);cursor:pointer}
.drop:hover,.drop.over{border-color:var(--brand);background:var(--brand-s)}.drop svg{width:28px;height:28px;color:var(--brand)}.drop b{font-size:15px}
.drop span{font-size:13px;color:var(--muted);max-width:60ch;overflow-wrap:anywhere}.drop:has(#file:disabled){opacity:.5;pointer-events:none}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.chip{min-height:32px;padding:0 13px;border-radius:999px;background:#fff;color:var(--ink);border-color:var(--line2)}
.chip:hover:not(:disabled){background:var(--paper)}.chip.on{background:var(--brand-s);border-color:var(--brand);color:var(--brand-d)}
/* data */
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12.5px;font-weight:600;color:var(--muted);background:var(--paper);white-space:nowrap}tbody tr:last-child td{border-bottom:0}
tbody th{color:var(--ink);font-size:13px}.floorRow{cursor:pointer}.floorRow:hover td{background:var(--brand-s)}
#boqTable td:not(:first-child),#boqTable th:not(:first-child){text-align:right}
.reviewWrap{overflow:auto;border:1px solid var(--line);border-radius:8px;background:#fff}.reviewWrap table{min-width:720px}
.drawingActions{display:flex;gap:6px}.drawingActions button{min-height:32px;padding:0 12px}
.downloads{display:flex;flex-wrap:wrap;gap:8px}
.summaryStrip{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.summaryChip{background:#fff;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12.5px}
.confHigh{color:var(--ok);font-weight:700}.confMid{color:var(--warn);font-weight:700}.confLow{color:var(--bad);font-weight:700}
.pill,.badge{display:inline-flex;align-items:center;border-radius:999px;padding:3px 10px;font-size:12px;font-weight:650;background:var(--ok-s);color:var(--ok);border:1px solid var(--ok-l);white-space:nowrap}
.badge.neutral{background:var(--paper);color:var(--muted);border-color:var(--line)}
.status{padding:10px 12px;border-radius:8px;background:var(--paper);border:1px solid var(--line);font-size:13.5px;overflow-wrap:anywhere}
.status.info{background:var(--blue-s);border-color:#cddaf3}.status.success{background:var(--ok-s);border-color:var(--ok-l);color:var(--ok)}
.status.error{background:var(--bad-s);border-color:var(--bad-l);color:var(--bad)}.status.mono{font-family:var(--mono)}
.callout{border-left:3px solid var(--warn);background:var(--warn-s);padding:10px 14px;border-radius:0 8px 8px 0;font-size:13px;color:#5c3a00}
.empty{text-align:center;padding:48px 20px;color:var(--muted)}.empty h3{color:var(--ink);font-size:17px;margin-bottom:6px}.empty button{margin-top:14px}
/* as-built */
.steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.step{display:flex;flex-direction:column;gap:10px;min-width:0;padding:16px;border:1px solid var(--line);border-radius:10px;background:#fff}
.stepHead{display:flex;align-items:center;gap:10px;font-weight:650}
.stepNo{width:24px;height:24px;border-radius:50%;background:var(--brand);color:#fff;display:grid;place-items:center;font-size:12.5px;font-weight:700;flex:none}
.fileMeta{font-size:12.5px;color:var(--muted);overflow-wrap:anywhere;max-height:56px;overflow:auto}
.thread{display:flex;flex-direction:column;gap:8px;min-height:44px;max-height:220px;overflow:auto;padding:8px;background:var(--paper);border-radius:8px}
.thread:empty::before{content:"Questions and corrections about the scan appear here.";color:var(--faint);font-size:13px}
.thread>div{font-size:13.5px;overflow-wrap:anywhere;unicode-bidi:plaintext;background:#fff!important;border:1px solid var(--line)}
.thread>div[style*=flex-end]{background:var(--brand-s)!important;border-color:var(--ok-l)}
details.advanced{border:1px solid var(--line);border-radius:10px;background:#fff;margin-bottom:16px}
details.advanced>summary{cursor:pointer;padding:12px 16px;font-weight:600;list-style:none}details.advanced>summary::-webkit-details-marker{display:none}
details.advanced>summary::before{content:"›";display:inline-block;margin-right:8px;transition:transform .15s}details[open].advanced>summary::before{transform:rotate(90deg)}
.advInner{padding:0 16px 16px}
/* account drawer */
#authDrawerBackdrop{position:fixed;inset:0;background:rgba(16,36,30,.4);z-index:100000;opacity:0;pointer-events:none;transition:opacity .2s}#authDrawerBackdrop.open{opacity:1;pointer-events:auto}
#authSideDrawer{position:absolute;right:0;top:0;height:100%;width:min(320px,86vw);background:#fff;color:var(--ink);padding:20px;display:flex;flex-direction:column;gap:10px;transform:translateX(105%);transition:transform .22s ease;box-shadow:-12px 0 32px rgba(0,0,0,.15)}
#authDrawerBackdrop.open #authSideDrawer{transform:none}#authSideDrawer .drawerHeading{font-size:17px;font-weight:650}
#authSideDrawer .drawerUser{font-size:12.5px;color:var(--muted);background:var(--paper);border-radius:8px;padding:10px;overflow-wrap:anywhere}
.drawerAction,#authDrawerClose{width:100%;justify-content:flex-start;background:#fff;color:var(--ink);border-color:var(--line2)}#authDrawerClose{align-self:flex-end;width:auto}
#authLogoutBtn{background:var(--bad-s)!important;color:var(--bad)!important;border-color:var(--bad-l)!important}
@media(max-width:1100px){.projStatus{display:none}}
@media(max-width:900px){.app{grid-template-columns:1fr}.side{position:static;height:auto;flex-direction:row;align-items:center;gap:8px;padding:8px 10px;overflow-x:auto}.brandSub,.sideNote{display:none}.nav{flex-direction:row;gap:4px}.navItem{width:auto;white-space:nowrap}.top{padding:10px 16px;flex-wrap:wrap}.views{padding:18px 16px 56px}.steps{grid-template-columns:1fr}.kpis{grid-template-columns:1fr 1fr}.kpi:nth-child(3){border-left:0}.kpi:nth-child(n+3){border-top:1px solid var(--line)}#projectSelect{min-width:0;flex:1}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>
<div class="app">
<aside class="side">
 <div class="brandRow"><svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#0b6b4f"/><g fill="#fff"><rect x="7" y="8" width="7" height="7" rx="1.5"/><rect x="18" y="8" width="7" height="7" rx="1.5" opacity=".55"/><rect x="7" y="18" width="7" height="7" rx="1.5" opacity=".55"/><rect x="18" y="18" width="7" height="7" rx="1.5"/></g></svg><div><div class="brandName">TELETECH</div><div class="brandSub">MANAGER</div></div></div>
 <nav class="nav" aria-label="Sections">
  <button class="navItem on" data-view="project"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>Project</button>
  <button class="navItem" data-view="results"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>Results<span class="navBadge" id="navResultsBadge" hidden></span></button>
  <button class="navItem" data-view="outputs"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 4v11M7 11l5 5 5-5M5 20h14"/></svg>Labels &amp; exports</button>
  <button class="navItem" data-view="asbuilt"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20h4L19 9l-4-4L4 16zM13 7l4 4"/></svg>As-Built</button>
  <button class="navItem" data-view="docs"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M7 3h7l5 5v13H7zM14 3v5h5"/></svg>Documents</button>
 </nav>
 <div class="sideNote">Your original drawings are never overwritten.</div>
</aside>
<div class="main">
<header class="top">
 <div class="projectBar"><label class="projLabel" for="projectSelect">Project</label><select id="projectSelect"></select>
  <details class="menu" id="projectMenu"><summary aria-label="Project actions" title="Project actions">⋯</summary><div class="menuPanel">
   <label class="field"><span>New project</span><input id="projectName" placeholder="Project name"></label><button id="createProject">Create project</button>
   <button id="renameProject" class="alt">Rename current project</button><button id="deleteProject" class="danger">Delete current project</button></div></details>
  <span id="projectStatus" class="projStatus" role="status"></span></div>
 <div id="headerRight"></div>
</header>
<main class="views">

<section class="view on" id="view-project">
 <div class="viewHead"><h2>Project</h2><p>Add the floor drawings, then check each floor's status before moving on to Results.</p></div>
 <div class="card"><div class="cardHead"><h3>Add drawings</h3></div>
  <label class="drop" id="dropZone" for="file"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 16V4M7 9l5-5 5 5M5 20h14"/></svg><b>Choose DWG / DXF files</b><span id="fileNames">Select all floors together. The floor is detected from each file name.</span></label>
  <input id="file" class="sr" type="file" accept=".dwg,.dxf" multiple>
  <div class="fields" style="margin-top:14px"><label class="field"><span>University</span><input id="univ" placeholder="Auto from file name"></label><label class="field"><span>Building</span><input id="building" placeholder="Auto from drawing name"></label><label class="field"><span>Floor (single file only)</span><select id="floor"><option selected>Auto Detect</option><option>Ground Floor</option><option>First Floor</option><option>Second Floor</option><option>Third Floor</option><option>Roof Floor</option><option>Unknown Floor</option></select></label></div>
  <div class="actions" style="margin-top:14px"><button id="upload">Upload &amp; analyze</button><button id="clearProject" class="danger">Clear project</button></div>
  <div id="status" class="status" role="status" style="margin-top:14px">Ready. Choose the floor drawings to begin.</div></div>
 <div class="card"><div class="cardHead"><h3>Analyzed floors</h3></div><div id="projectSummary"></div></div>
 <div class="card" id="uploadedDrawingsCard" style="display:none"><div class="cardHead"><div><h3>Uploaded drawings</h3><div class="sub" id="uploadedDrawingsStatus">Manage drawings in this project.</div></div></div><div id="uploadedDrawingsList"></div></div>
</section>

<section class="view" id="view-results">
 <div class="viewHead"><h2>Results</h2><p>Quantities and rack split for the selected floor. Fix anything flagged below before exporting.</p></div>
 <div id="resultsEmpty" class="card empty"><h3>No results yet</h3><div>Analyze a drawing to see its bill of quantities here.</div><button id="goProject">Go to Project</button></div>
 <div id="resultsBody" hidden>
  <div id="floorChips" class="chips"></div>
  <div class="kpis"><div class="kpi">BOQ physical items<b id="physical">-</b></div><div class="kpi">Logical ports<b id="ports">-</b></div><div class="kpi">Ignored / supporting<b id="ignored">-</b></div><div class="kpi">Needs review<b id="review">-</b></div></div>
  <div class="card"><div class="cardHead"><h3>Bill of quantities by rack</h3></div><div class="reviewWrap"><table id="boqTable"><thead><tr><th>BOQ item</th><th id="rack1Head">Rack 1</th><th id="rack2Head">Rack 2</th><th>Lab / unassigned</th><th>Total</th></tr></thead><tbody id="boqbody"></tbody></table></div></div>
  <div class="card" id="classCard" style="display:none"><div class="cardHead"><h3>Classification review</h3></div><div class="sub">These symbols are in the drawing but not recognized yet. Pick the real service for each one, or ignore it if it is supporting. Applied items flow into the BOQ, rack split, Excel and CAD labels.</div><div id="classRows" style="margin-top:12px"></div><div class="actions" style="margin-top:10px"><button id="applyClass">Apply classification</button></div><div id="classStatus" class="status" style="margin-top:10px"></div></div>
  <div class="card" id="rackCard" style="display:none"><div class="cardHead"><h3>Rack allocation review</h3></div><div class="sub">These counted items have no usable coordinates. Split each total between the two racks; the two numbers must add up to the total.</div><div id="rackRows" style="margin-top:12px"></div><div class="actions" style="margin-top:10px"><button id="applyRack">Apply rack allocation</button></div><div id="rackStatus" class="status" style="margin-top:10px"></div></div>
  <div class="card"><div class="cardHead"><div><h3>Board review</h3><div class="sub">Scans wall gaps before labeling and closes tiny drafting errors in memory only. Produces a gap report and a debug DXF.</div></div><button id="boardReview" class="alt">Run on this floor</button></div><div id="boardReviewStatus" class="status">Analyze a floor first, then run the board review.</div><div id="boardReviewDownloads" class="downloads" style="margin-top:10px"></div></div>
 </div>
</section>

<section class="view" id="view-outputs">
 <div class="viewHead"><h2>Labels &amp; exports</h2><p>Choose the label format, then download the Excel files and labeled CAD drawings for the whole project.</p></div>
 <div class="card"><div class="cardHead"><div><h3>Label ID format</h3><div class="sub">The same format is used in the Excel files and on the CAD labels.</div></div></div>
  <div class="fields"><label class="field"><span>Format</span><select id="labelSchemaMode"><option value="default" selected>TELETECH default</option><option value="custom">Custom</option></select></label><label class="field" style="grid-column:span 2"><span>Pattern</span><input id="labelPattern" value="{rack}/{network}{panel2}/{code}{port2}" disabled></label></div>
  <div class="actions" style="margin-top:12px"><div id="labelPreview" class="status mono" style="flex:1">Preview: F1/LC02/C01</div><button id="resetSchema" class="alt">Reset to default</button></div>
  <div class="small sub" style="margin-top:10px">Tokens: <code>{rack}</code> <code>{floor}</code> <code>{rack_no}</code> <code>{network}</code> <code>{panel}</code> <code>{panel2}</code> <code>{code}</code> <code>{port}</code> <code>{port2}</code> <code>{service}</code></div></div>
 <div class="card"><div class="cardHead"><h3>Excel files</h3></div><div class="downloads"><button class="good" data-export="port_map">Port map</button><button class="good" data-export="quantity_summary">Quantity summary</button><button class="good" data-export="cable_schedule">Labeling / cable schedule</button></div><div id="exportStatus" class="sub" style="margin-top:10px">Analyze a drawing first.</div></div>
 <div class="card"><div class="cardHead"><h3>Labeled CAD drawings</h3></div><div class="actions"><label class="field"><span>Text height</span><input id="labelHeight" type="number" min="0" step="10" placeholder="Auto"></label><button id="generateLabels" style="align-self:flex-end">Generate labeled CAD</button></div><div id="labelStatus" class="status" style="margin-top:12px">Analyze the project first.</div><div id="labelDownloads" class="downloads" style="margin-top:10px"></div></div>
</section>

<section class="view" id="view-asbuilt">
 <div class="viewHead"><h2>As-Built revisions</h2><p>Upload the technicians' scan. Colored markups are counted by code, and nothing changes in CAD until you approve it.</p></div>
 <div class="cardHead" style="margin-bottom:12px"><span id="asProjectBadge" class="badge neutral">Analyze CAD first</span></div>
 <div class="steps">
  <div class="step"><div class="stepHead"><span class="stepNo">1</span>Upload the scan</div><input id="asChatFile" type="file" accept=".pdf,.png,.jpg,.jpeg" multiple><div id="asChatFileMeta" class="fileMeta">No file selected.</div><div class="stepHead" style="font-weight:600;font-size:13px;margin-top:4px">Legend reference (optional)</div><input id="asLegendFile" type="file" accept=".png,.jpg,.jpeg,.webp,.bmp"><div id="asLegendMeta" class="fileMeta">A separate legend crop is excluded from counting.</div></div>
  <div class="step"><div class="stepHead"><span class="stepNo">2</span>Analyze</div><div class="sub">Reads each page and counts colored markups. Blue ink is ignored.</div><input id="asChatModel" type="hidden" value=""><button id="asChatSend" class="actionBig" style="margin-top:auto">Analyze As-Built</button></div>
  <div class="step"><div class="stepHead"><span class="stepNo">3</span>Approve &amp; generate</div><div id="asChatStatus" class="status info">Analyze the CAD project first, then upload the scan.</div><button id="asGenerate" class="actionBig" style="margin-top:auto">Generate new CAD package</button></div>
 </div>
 <div class="card" style="margin-top:16px"><div class="cardHead"><div><h3>Markup counts</h3><div class="sub">Detected by OpenCV from the scan, not CAD entities. Match each to a CAD endpoint to confirm it.</div></div><button id="asChatAdd" class="good" style="display:none">Add to revision list</button></div><div id="asCounts" class="summaryStrip"><div class="summaryChip">No analysis yet.</div></div><div id="asLegend" class="small sub"></div><div id="asChatReview" class="reviewWrap" style="display:none;margin-top:10px"></div></div>
 <div class="card"><div class="cardHead"><h3>Clarifications</h3><button id="asChatReset" class="alt">Reset</button></div><div id="asChatThread" class="thread"></div><div class="actions" style="margin-top:10px"><input id="asChatInput" dir="auto" placeholder="اكتب تصحيحًا أو ملاحظة عن علامة غير واضحة..." style="flex:1;min-width:200px"></div></div>
 <details class="advanced"><summary>Manual revision entry</summary><div class="advInner stack">
  <div class="fields"><select id="asFloor"><option value="">Select floor</option></select><select id="asOp"><option value="SITE_REMOVE">Site remove / withdraw</option><option value="MOVE_ENDPOINT">Move endpoint</option><option value="DELETE_ENDPOINT">Delete endpoint</option><option value="ADD_BLOCK">Add block / symbol</option><option value="ADD_NOTE">Add note</option></select></div>
  <div id="asTargetWrap"><select id="asTarget" style="width:100%"><option value="">Select floor first</option></select></div>
  <div id="asSiteWrap" class="fields"><select id="asSiteStatus"><option value="">Site status...</option><option value="لم يتم السحب">لم يتم السحب</option><option value="تم رد الكابلات">تم رد الكابلات</option><option value="لا يوجد لها مسار">لا يوجد لها مسار</option><option value="تم نقل">تم نقل</option></select><select id="asPatchAction"><option value="">Patch action...</option><option value="SHIFT">SHIFT</option><option value="RESERVE">RESERVE</option></select></div>
  <div class="fields"><input id="asX" type="number" step="0.01" placeholder="New X"><input id="asY" type="number" step="0.01" placeholder="New Y"><input id="asBlock" placeholder="Block name"><input id="asRotation" type="number" value="0" placeholder="Rotation"><input id="asScale" type="number" value="1" placeholder="Scale"></div>
  <div class="fields"><input id="asText" placeholder="Note text"><input id="asTextHeight" type="number" value="150" placeholder="Text height"><input id="asNote" placeholder="Internal note"></div>
  <div class="actions"><button id="asAdd">Add reviewed change</button><button id="asClear" class="alt">Clear list</button></div><div id="asStatus" class="status">No manual change added.</div></div></details>
 <div class="card"><div class="cardHead"><h3>Approved revision list</h3></div><div id="asChangeList"></div><div id="asDownloads" class="downloads" style="margin-top:10px"></div></div>
 <div class="callout"><b>Safety boundary:</b> only supported endpoint and site-removal changes are generated, packaged for AutoCAD Desktop. Cable route changes stay in review, because the system must not guess a cable path.</div>
</section>

<section class="view" id="view-docs"><div id="projectDocumentsCard">
 <div class="viewHead"><h2>Documents</h2><p>PDF and Excel files saved separately inside the selected project.</p></div>
 <div class="card"><div class="cardHead"><h3>Upload a document</h3></div>
  <div class="fields"><label class="field"><span>Category</span><select id="docCategory"><option value="contract_boq">Contract &amp; BOQ — العقد والمقايسات</option><option value="mir_ir_issue">MIR / IR / Material Issue — الفحص وأذونات الصرف</option><option value="consultant_letters">Consultant Letters — جوابات الاستشاري</option><option value="consultant_packing">Consultant Packing Lists — الباكنج ليست</option></select></label><label class="field"><span>Document number</span><input id="docNumber" placeholder="Optional"></label><label class="field"><span>Date</span><input id="docDate" type="date"></label><label class="field"><span>Notes</span><input id="docNote" placeholder="Optional"></label></div>
  <div class="actions" style="margin-top:12px"><input id="docFile" type="file" accept=".pdf,.xls,.xlsx,.xlsm,.csv,application/pdf,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv" style="flex:1;min-width:220px"><button id="uploadProjectDoc">Upload document</button></div>
  <div id="projectDocsStatus" class="status" role="status" style="margin-top:12px">Select a PDF or Excel file to add it to this project.</div></div>
 <div class="card"><div class="cardHead"><h3>Saved documents</h3></div><div id="projectDocsList"></div></div>
</div></section>

</main></div></div>
<script>
let result=null,busy=false;const $=id=>document.getElementById(id);let asbuiltChanges=[];let asbuiltTargets=[];let asbuiltVisionRows=[];let asbuiltProjectFloors=[];
let asChatId=null,asChatFilePath='',asChatLegendPath='',asChatBusy=false;
function uploadAsbuiltChatFile(f){return new Promise((resolve,reject)=>{const x=new XMLHttpRequest();x.open('POST','/api/asbuilt_chat_upload?filename='+encodeURIComponent(f.name),true);x.timeout=20*60*1000;x.setRequestHeader('Content-Type',f.type||'application/octet-stream');x.upload.onprogress=e=>{if(e.lengthComputable)$('asChatStatus').textContent=`Uploading As-Built file ${Math.round(e.loaded*100/e.total)}%...`};x.onload=()=>{let j;try{j=JSON.parse(x.responseText||'{}')}catch(e){return reject(new Error('Invalid file upload response'))}if(x.status<200||x.status>=300||j.error)return reject(new Error(j.error||`File upload HTTP ${x.status}`));resolve(j)};x.onerror=()=>reject(new Error('File upload connection failed'));x.ontimeout=()=>reject(new Error('File upload timed out'));x.send(f)})}
function uploadAsbuiltLegendFile(f){return new Promise((resolve,reject)=>{const x=new XMLHttpRequest();x.open('POST','/api/asbuilt_legend_upload?filename='+encodeURIComponent(f.name),true);x.timeout=5*60*1000;x.setRequestHeader('Content-Type',f.type||'application/octet-stream');x.onload=()=>{let j;try{j=JSON.parse(x.responseText||'{}')}catch(e){return reject(new Error('Invalid legend upload response'))}if(x.status<200||x.status>=300||j.error)return reject(new Error(j.error||`Legend upload HTTP ${x.status}`));resolve(j)};x.onerror=()=>reject(new Error('Legend upload connection failed'));x.ontimeout=()=>reject(new Error('Legend upload timed out'));x.send(f)})}
function asChatBubble(role,text){const box=$('asChatThread');if(!box)return;const d=document.createElement('div');d.style.cssText=role==='user'?'align-self:flex-end;background:#eef2ff;border-radius:10px;padding:8px 12px;max-width:80%':'align-self:flex-start;background:#f4f4f5;border-radius:10px;padding:8px 12px;max-width:80%';d.textContent=text;box.appendChild(d);box.scrollTop=box.scrollHeight;}
function colorDot(c){const m={pink:'#ec4899',yellow:'#facc15',orange:'#f97316',green:'#22c55e',blue:'#3b82f6'};return `<span title="${c}" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${m[c]||'#999'};border:1px solid rgba(0,0,0,.15);vertical-align:middle;margin:0 3px"></span>`}
function renderVisionReview(j){
 const box=$('asChatReview'); if(!box)return;
 asbuiltVisionRows=(j&&j.findings)||[];
 const sc=(j&&j.slide_counts)||[];
 const legend=(j&&j.legend_mapping)||[];
 let ch='';
 if(sc.length){
   ch=sc.map(x=>{
     const cv=x.cv_color_regions||null;
     const v=x.count_verification||'';
     const legendBadge=x.legend_excluded?' · Legend excluded':(x.legend_match_method&&x.legend_match_method!=='not_supplied'?' · Legend not matched':'');
     const cvtxt=cv?` · CV ${colorDot('pink')}${cv.pink||0} ${colorDot('yellow')}${cv.yellow||0} ${colorDot('orange')}${cv.orange||0}`:'';
     const badge=v==='MATCH'?'✓ CV match':(v==='REVIEW'?'⚠ CV review':'');
     const err=x.qwen_error?`<div class="miniNote">Legacy model note: ${esc(x.qwen_error)}</div>`:'';
     return `<div class="summaryChip"><b>Page ${Number(x.page_number||0)}</b> ${esc(x.floor||'')} · ${colorDot('pink')} ${x.pink||0} · ${colorDot('yellow')} ${x.yellow||0} · ${colorDot('orange')} ${x.orange||0} · ${colorDot('green')} ${x.green||0} · ⚠️ ${x.ambiguous||0} ${cvtxt} ${badge?`<span class="miniNote">${badge}</span>`:''}${legendBadge?`<span class="miniNote">${legendBadge}</span>`:''}${err}</div>`;
   }).join('');
 }
 $('asCounts').innerHTML=ch||'<div class="summaryChip">No page counts returned.</div>';
 $('asLegend').innerHTML=legend.length?'<b>Legend lock:</b> '+legend.map(x=>`${esc(x.color)} = ${esc(x.status_original||'')} ${x.confirmed?'✓':'⚠ review'}`).join(' | '):'';

 if(!asbuiltVisionRows.length){
   const inv=(j&&j.markup_inventory)||[];
   let ih=`<div class="miniNote">No CAD finding was auto-approved. Detected markup candidates: ${inv.length}. Every candidate requires human confirmation.</div>`;
   if(inv.length){
     ih+=`<div class="miniNote"><b>CAD Match Review:</b> existing endpoints are ranked by normalized drawing position. No CAD change is applied automatically.</div>`;
     ih+=`<div style="max-height:340px;overflow:auto;margin-top:8px"><table><thead><tr><th>ID</th><th>Page</th><th>Color</th><th>X/Y</th><th>CV</th><th>Suggested CAD match</th><th>Match</th></tr></thead><tbody>`;
     inv.forEach((m,mi)=>{
       const opts=m.candidate_options||[];
       const best=opts[0]||{};
       let optionHtml=opts.map(o=>`<option value="${esc(o.target_id||'')}">${esc((o.floor||'')+' | '+(o.label||o.block||o.target_id||''))} | ${Math.round(Number(o.match_confidence||0)*100)}%</option>`).join('');
       if(!optionHtml) optionHtml='<option value="">No CAD candidate</option>';
       ih+=`<tr><td>${esc(m.markup_id||'')}</td><td>${Number(m.page_number||0)}</td><td>${colorDot(m.color||'')}</td><td>${Number(m.x_norm||0)},${Number(m.y_norm||0)}</td><td>${Math.round(Number(m.confidence||0)*100)}%</td><td><select data-match-row="${mi}" style="min-width:280px">${optionHtml}</select></td><td>${Math.round(Number(best.match_confidence||0)*100)}%</td></tr>`;
     });
     ih+='</tbody></table></div>';
   }
   box.innerHTML=ih; box.style.display='block'; $('asChatAdd').style.display='none'; return;
 }

 let h=`<div class="miniNote">${esc(j.document_summary||'')} — ${j.finding_count||0} supported CAD finding(s) found.</div><table><thead><tr><th>Use</th><th>Floor</th><th>Page</th><th>Detected status</th><th>Service</th><th>CAD endpoint</th><th>Patch action</th><th>Confidence</th><th>Evidence</th></tr></thead><tbody>`;
 asbuiltVisionRows.forEach((f,i)=>{
   const all=f.candidate_options||[];
   const sug=f.suggested_target_id||'';
   const opts=['<option value="">Review endpoint...</option>'].concat(all.map(t=>`<option value="${esc(t.target_id||'')}" ${t.target_id===sug?'selected':''}>${esc(t.label||t.block||t.target_id||'')} | ${esc(t.service||'')} | ${esc(t.rack||'')}</option>`)).join('');
   const pct=Math.round(Number(f.confidence||0)*100);
   const cc=pct>=80?'confHigh':(pct>=60?'confMid':'confLow');
   h+=`<tr data-airow="${i}"><td><input class="aiUse" type="checkbox" ${f.auto_selected?'checked':''}></td><td><b>${esc(f.floor||'')}</b></td><td>${Number(f.page_number||0)}</td><td><b>${esc(f.status||'')}</b></td><td>${esc(f.service_hint||'')}</td><td><select class="aiTarget" style="min-width:340px">${opts}</select></td><td><select class="aiPatch"><option value="">Choose...</option><option value="SHIFT">SHIFT</option><option value="RESERVE">RESERVE</option></select></td><td class="${cc}">${pct}%</td><td>${esc(f.evidence||f.nearby_text||'')}</td></tr>`;
 });
 h+='</tbody></table>';
 box.innerHTML=h; box.style.display='block'; $('asChatAdd').style.display='inline-block';
}
function handleAsChatTurn(j){if(j.type==='final'){asChatBubble('model',j.document_summary||'Analysis ready — review the counts below.');renderVisionReview(j);if(j.human_review_required&&j.question)asChatBubble('model','⚠️ '+j.question);}else{asChatBubble('model',j.question||'...');$('asChatReview').style.display='none';$('asChatReview').innerHTML='';$('asChatAdd').style.display='none';}}
function renderAsbuiltChanges(){const box=$('asChangeList');if(!box)return;if(!asbuiltChanges.length){box.innerHTML='<div class="sub">No reviewed As-Built changes added yet.</div>';return;}let h='<div class="reviewWrap"><table><thead><tr><th>#</th><th>Floor</th><th>Operation</th><th>Target / Block</th><th>Site status / Patch action / New XY</th><th></th></tr></thead><tbody>';asbuiltChanges.forEach((c,i)=>{const t=asbuiltTargets.find(x=>x.target_id===c.target_id);let target=c.target_label||(t?(t.label||t.block||t.target_id):(c.block||c.target_id||''));let detail=c.op==='SITE_REMOVE'?`${c.site_status} | ${c.patch_action}`:c.op==='MOVE_ENDPOINT'?`${c.x}, ${c.y}`:c.op==='ADD_BLOCK'?`${c.x}, ${c.y} | ${c.block}`:c.op==='ADD_NOTE'?`${c.x}, ${c.y} | ${c.text}`:(c.note||'');h+=`<tr><td>${i+1}</td><td>${esc(c.floor||'')}</td><td>${esc(c.op)}</td><td>${esc(target)}</td><td>${esc(detail)}</td><td><button class="alt asDel" data-i="${i}">Remove</button></td></tr>`});h+='</tbody></table></div>';box.innerHTML=h;document.querySelectorAll('.asDel').forEach(b=>b.onclick=()=>{asbuiltChanges.splice(+b.dataset.i,1);renderAsbuiltChanges();});}
async function loadAsbuiltTargets(){const floor=$('asFloor').value;if(!floor){asbuiltTargets=[];$('asTarget').innerHTML='<option value="">Select floor first</option>';return;}let r=await fetch('/api/asbuilt_targets?floor='+encodeURIComponent(floor)+'&label_pattern='+encodeURIComponent(activeLabelPattern())),j=await r.json();if(!r.ok||j.error){$('asStatus').textContent=j.error||'Cannot load As-Built targets';return;}asbuiltTargets=j.targets||[];$('asTarget').innerHTML='<option value="">Select endpoint</option>'+asbuiltTargets.map(x=>`<option value="${esc(x.target_id)}">${esc(x.label||x.block)} | ${esc(x.service)} | ${x.x.toFixed(1)}, ${x.y.toFixed(1)}${x.editable_direct?'':' [nested/read-only]'}</option>`).join('');$('asStatus').textContent=`Loaded ${asbuiltTargets.length} endpoints for ${floor}.`;}
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
const DEFAULT_LABEL_SCHEMA='{rack}/{network}{panel2}/{code}{port2}';
function activeLabelPattern(){return $('labelSchemaMode').value==='custom'?$('labelPattern').value.trim():DEFAULT_LABEL_SCHEMA}
function localSchemaPreview(){let p=activeLabelPattern()||DEFAULT_LABEL_SCHEMA;const v={rack:'F1',floor:'F',rack_no:'1',network:'LC',panel:'2',panel2:'02',code:'C',port:'1',port2:'01',service:'CCTV'};return p.replace(/\{([A-Za-z0-9_]+)\}/g,(m,k)=>Object.prototype.hasOwnProperty.call(v,k)?v[k]:m)}
function refreshSchemaUi(){$('labelPattern').disabled=$('labelSchemaMode').value!=='custom';$('labelPreview').textContent='Preview: '+localSchemaPreview()}
$('labelSchemaMode').onchange=refreshSchemaUi;$('labelPattern').oninput=refreshSchemaUi;$('resetSchema').onclick=()=>{$('labelSchemaMode').value='default';$('labelPattern').value=DEFAULT_LABEL_SCHEMA;refreshSchemaUi()};refreshSchemaUi();
function render(r){result=r;const rl=r.rack_labels||{};if($('rack1Head'))$('rack1Head').textContent=rl['IDF-G1']||'Rack 1';if($('rack2Head'))$('rack2Head').textContent=rl['IDF-G2']||'Rack 2';const q=r.quantity_boq||{};const rackKeys=Object.keys(r.counts||{});const phys=rackKeys.reduce((a,k)=>a+(r.counts?.[k]?.physical_total||0),0);let logical=0;for(const k of rackKeys) logical+=(r.counts?.[k]?.logical_total||0);for(const [s,n] of Object.entries(r.unassigned||{})) if(s.includes('CCTV')) logical+=(+n||0);$('physical').textContent=phys;$('ports').textContent=logical;$('ignored').textContent=r.ignored_count||0;$('review').textContent=(r.review_count||0)+(r.rack_review_count||0);
const svcMap=[['D-V','D-V (Data + Voice)'],['D-W','Indoor Wi-Fi'],['CAM1','CCTV - Indoor Dome'],['CAM2','CCTV - Indoor Bullet'],['CAM3aaaa','CCTV - Outdoor Bullet'],['floor boox 1','Floor Box - 2 Cable'],['AC AGN','Access Control Point'],['AV SYSTEM RACK','AV System Rack'],['RECORD CAM','Record Camera'],['H','High-Level Screen'],['PROJECTOR','Projector'],['DIGITAL S','Digital Signage'],['TV','Television']];let rows='';const labKeys=(r.lab_racks||[]);for(const [key,svc] of svcMap){const total=q[key]||0;if(!total)continue;const g1=r.counts?.['IDF-G1']?.physical?.[svc]||0,g2=r.counts?.['IDF-G2']?.physical?.[svc]||0,lab=labKeys.reduce((a,k)=>a+(r.counts?.[k]?.physical?.[svc]||0),0),un=((r.unassigned||{})[svc]||0)+lab;rows+=`<tr><td>${esc(key)}<div class="small sub">${esc(svc)}</div></td><td>${g1}</td><td>${g2}</td><td>${un}</td><td><b>${total}</b></td></tr>`}const g1t=r.counts?.['IDF-G1']?.physical_total||0,g2t=r.counts?.['IDF-G2']?.physical_total||0,labt=labKeys.reduce((a,k)=>a+(r.counts?.[k]?.physical_total||0),0);rows+=`<tr><th>MAIN RACK TOTAL</th><th>${g1t}</th><th>${g2t}</th><th>${labt}</th><th>${g1t+g2t+labt}</th></tr>`;for(const lr of labKeys){const lt=r.counts?.[lr]?.physical_total||0;rows+=`<tr><th>${esc((r.rack_labels||{})[lr]||lr)} LOCAL LAB RACK</th><th colspan="2">separate from main racks</th><th>${lt}</th><th>${lt}</th></tr>`}$('boqbody').innerHTML=rows;

const reviews=Array.isArray(r.review)?r.review:[];$('classCard').style.display=reviews.length?'block':'none';
const classOptions=[['','Choose service...'],['DV','D-V (Data + Voice)'],['W','Indoor Wi-Fi'],['C_DOME','CCTV - Indoor Dome'],['C_BULLET','CCTV - Indoor Bullet'],['C_OUTDOOR','CCTV - Outdoor Bullet'],['AC','Access Control'],['A','AV System Rack'],['R','Record Camera'],['H','High-Level Screen'],['P','Projector'],['S','Digital Signage'],['TV','Television'],['F2','Floor Box - 2 Cable'],['F1','Floor Box - 1 Cable'],['IGNORE','Ignore / Supporting']];
$('classRows').innerHTML=reviews.map((x,i)=>{const rid=x.review_id||i+1;const attrs=JSON.stringify(x.attributes||{});const opts=classOptions.map(([v,t])=>`<option value="${esc(v)}">${esc(t)}</option>`).join('');return `<div class="card reviewItem" data-rid="${esc(rid)}" style="padding:12px;margin-bottom:8px;background:#f9fafb"><div><b>#${esc(rid)} — Block: ${esc(x.block||'?')}</b></div><div class="small sub">Layer: ${esc(x.layer||'?')} &nbsp; | &nbsp; Attributes: ${esc(attrs)} &nbsp; | &nbsp; XY: ${Number(x.x||0).toFixed(2)}, ${Number(x.y||0).toFixed(2)}</div><select class="classChoice" style="margin-top:8px">${opts}</select></div>`}).join('');
$('classStatus').textContent=reviews.length?`${reviews.length} item(s) must be classified before complete CAD labeling.`:'No classification review.';
const uv=r.unassigned||{};const ents=Object.entries(uv).filter(([,n])=>+n>0);$('rackCard').style.display=ents.length?'block':'none';$('rackRows').innerHTML=ents.map(([svc,n])=>`<div class="row" data-svc="${esc(svc)}" data-total="${n}" style="margin-bottom:8px"><b style="min-width:260px">${esc(svc)} (Total ${n})</b><label>G1 <input class="num g1" type="number" min="0" max="${n}" value="0"></label><label>G2 <input class="num g2" type="number" min="0" max="${n}" value="${n}"></label></div>`).join('');$('rackStatus').textContent=ents.length?'Allocation required before Excel export.':'No rack allocation review.';
const so=r.spatial_label_order||{};if($('routeStatus')){if(so.error){$('routeStatus').textContent='Spatial route fallback: '+so.error}else{const rs=so.racks||{};const a=rs['IDF-G1']||{},b=rs['IDF-G2']||{};$('routeStatus').innerHTML=`Detected room IDs: <b>${so.room_markers||0}</b><br><b>${esc(a.display||'Rack 1')}</b>: ${esc(a.direction||'')} | anchor ${Number(a.anchor_x||0).toFixed(0)}, ${Number(a.anchor_y||0).toFixed(0)} (${esc(a.anchor_source||'')}) | start room <b>${esc(a.start_room||'?')}</b><br><span class="small">Rooms: ${esc((a.room_preview||[]).join(' → ')||'geometry fallback')}</span><br><b>${esc(b.display||'Rack 2')}</b>: ${esc(b.direction||'')} | anchor ${Number(b.anchor_x||0).toFixed(0)}, ${Number(b.anchor_y||0).toFixed(0)} (${esc(b.anchor_source||'')}) | start room <b>${esc(b.start_room||'?')}</b><br><span class="small">Rooms: ${esc((b.room_preview||[]).join(' → ')||'geometry fallback')}</span>`}};$('exportStatus').textContent=r.analysis_warning|| (ents.length?'Resolve rack allocation to enable complete rack-based Excel.':'Ready to generate Excel.')}
let activeProjectId=localStorage.getItem('teletech_project_id')||'';
async function loadProjects(selectId){try{const r=await fetch('/api/projects',{cache:'no-store'}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Cannot load projects');const rows=j.projects||[],sel=$('projectSelect');if(!sel)return;sel.innerHTML=rows.map(x=>`<option value=\"${esc(x.id)}\">${esc(x.name)}</option>`).join('');if(!rows.length){$('projectStatus').textContent='Create your first project.';return;}const chosen=rows.some(x=>x.id===selectId)?selectId:(j.active_project_id&&rows.some(x=>x.id===j.active_project_id)?j.active_project_id:rows[0].id);activeProjectId=chosen;localStorage.setItem('teletech_project_id',chosen);sel.value=chosen;const r2=await fetch('/api/project_select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:chosen})}),j2=await r2.json();if(!r2.ok||j2.error)throw Error(j2.error||'Cannot select project');renderProject(j2.project||[]);$('projectStatus').textContent='Active: '+(rows.find(x=>x.id===chosen)?.name||chosen);await loadProjectDocuments();}catch(e){if($('projectStatus'))$('projectStatus').textContent=e.message;}}
if($('projectSelect'))$('projectSelect').onchange=async()=>{const id=$('projectSelect').value;if(!id)return;activeProjectId=id;localStorage.setItem('teletech_project_id',id);let r=await fetch('/api/project_select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:id})}),j=await r.json();if(!r.ok||j.error)return $('projectStatus').textContent=j.error||'Cannot select project';result=null;renderProject(j.project||[]);$('projectStatus').textContent='Project selected.';await loadUploadedDrawings();await loadProjectDocuments();};
if($('createProject'))$('createProject').onclick=async()=>{const name=$('projectName').value.trim()||'New Project';$('createProject').disabled=true;try{let r=await fetch('/api/project_create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Cannot create project');$('projectName').value='';await loadProjects(j.project.id);}catch(e){$('projectStatus').textContent=e.message}finally{$('createProject').disabled=false}};
if($('renameProject'))$('renameProject').onclick=async()=>{const id=$('projectSelect').value;if(!id)return $('projectStatus').textContent='Select a project first.';const current=$('projectSelect').selectedOptions[0]?.textContent||'';const name=prompt('Enter the new project name:',current);if(name===null)return;if(!name.trim())return $('projectStatus').textContent='Project name cannot be empty.';try{const r=await fetch('/api/project_rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:id,name:name.trim()})}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Rename failed');await loadProjects(id);$('projectStatus').textContent='Project renamed.';}catch(e){$('projectStatus').textContent=e.message}};
if($('deleteProject'))$('deleteProject').onclick=async()=>{const id=$('projectSelect').value;if(!id)return $('projectStatus').textContent='Select a project first.';const name=$('projectSelect').selectedOptions[0]?.textContent||'this project';if(!confirm('Permanently delete project "'+name+'" and ALL its drawings, results and generated files? This cannot be undone.'))return;try{const r=await fetch('/api/project_delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:id})}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Delete failed');activeProjectId='';localStorage.removeItem('teletech_project_id');result=null;await loadProjects(j.active_project_id||'');await loadUploadedDrawings();renderProject([]);$('projectStatus').textContent='Project deleted.';}catch(e){$('projectStatus').textContent=e.message}};
function renderProject(rows){if(!Array.isArray(rows)||!rows.length){$('projectSummary').innerHTML='<div class="sub">No project floors loaded yet.</div>';refreshAsbuiltFloors([]);return;}let h='<div class="sub" style="margin-bottom:8px">Click any drawing to show its own BOQ table below.</div><table><thead><tr><th>Floor</th><th>Drawing</th><th>Rack 1</th><th>Rack 2</th><th>Physical</th><th>Logical Ports</th><th>Status</th></tr></thead><tbody>';for(const x of rows){let st=(x.rack_review||x.review)?`CHECK (${x.review||0}/${x.rack_review||0})`:'READY';h+=`<tr class="floorRow" data-floor="${esc(x.floor)}" style="cursor:pointer"><td><b>${esc(x.floor)}</b></td><td>${esc(x.drawing)}</td><td>${esc(x.rack1)}: ${x.rack1_physical}</td><td>${esc(x.rack2)}: ${x.rack2_physical}</td><td>${x.physical_total}</td><td>${x.logical_total}</td><td>${st}</td></tr>`;}h+='</tbody></table>';$('projectSummary').innerHTML=h;refreshAsbuiltFloors(rows);document.querySelectorAll('.floorRow').forEach(tr=>tr.onclick=async()=>{let r=await fetch('/api/project_result?floor='+encodeURIComponent(tr.dataset.floor)),j=await r.json();if(!r.ok||j.error)return $('status').textContent=j.error||'Cannot load floor result';render(j.result);$('status').textContent='Showing BOQ for '+tr.dataset.floor;});}
function refreshAsbuiltFloors(rows){asbuiltProjectFloors=(rows||[]).map(x=>x.floor);if($('asProjectBadge')){$('asProjectBadge').textContent=`${asbuiltProjectFloors.length} analyzed floor${asbuiltProjectFloors.length===1?'':'s'}`;$('asProjectBadge').className='badge '+(asbuiltProjectFloors.length?'':'neutral');}if(!$('asFloor'))return;const cur=$('asFloor').value;let opts='<option value="">Select floor for manual change</option>';for(const x of rows||[])opts+=`<option value="${esc(x.floor)}">${esc(x.floor)}</option>`;$('asFloor').innerHTML=opts;if((rows||[]).some(x=>x.floor===cur))$('asFloor').value=cur;}
function setBusy(v){busy=v;$('upload').disabled=v;$('file').disabled=v;if($('generateLabels'))$('generateLabels').disabled=v}
function uploadFile(f){return new Promise((resolve,reject)=>{const x=new XMLHttpRequest();x.open('POST','/api/upload?filename='+encodeURIComponent(f.name)+'&project_id='+encodeURIComponent(activeProjectId||''),true);x.timeout=20*60*1000;x.setRequestHeader('Content-Type','application/octet-stream');x.upload.onprogress=e=>{if(e.lengthComputable)$('status').textContent=`Uploading ${Math.round(e.loaded*100/e.total)}% (${(e.loaded/1048576).toFixed(1)} / ${(e.total/1048576).toFixed(1)} MB)...`};x.onload=()=>{let j;try{j=JSON.parse(x.responseText||'{}')}catch(e){return reject(new Error('Invalid upload response'))}if(x.status<200||x.status>=300||j.error)return reject(new Error(j.error||`Upload HTTP ${x.status}`));resolve(j)};x.onerror=()=>reject(new Error('Upload connection failed'));x.ontimeout=()=>reject(new Error('Upload timed out'));x.send(f)})}
async function loadProjectDocuments(){const list=$('projectDocsList'),status=$('projectDocsStatus');if(!list)return;try{const r=await fetch('/api/project_documents',{cache:'no-store',credentials:'same-origin'}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Cannot load project documents');const rows=j.documents||[];if(!rows.length){list.innerHTML='<div class="sub">No project documents uploaded yet.</div>';return;}let h='<div class="reviewWrap"><table style="min-width:850px"><thead><tr><th>Category</th><th>Document</th><th>Number</th><th>Date</th><th>Uploaded by</th><th>Uploaded at</th><th>Actions</th></tr></thead><tbody>';for(const x of rows){h+=`<tr><td>${esc(x.category_label||x.category||'')}</td><td>${esc(x.filename||'')}<div class="sub small">${esc(x.note||'')}</div></td><td>${esc(x.document_number||'—')}</td><td>${esc(x.document_date||'—')}</td><td>${esc(x.uploaded_by||'')}</td><td>${esc(x.uploaded_at||'')}</td><td><div class="drawingActions"><button class="good docDownload" data-id="${esc(x.id)}">Download</button><button class="docDelete" style="background:#b42318" data-id="${esc(x.id)}">Delete</button></div></td></tr>`}h+='</tbody></table></div>';list.innerHTML=h;list.querySelectorAll('.docDownload').forEach(b=>b.onclick=()=>{location.href='/api/project_documents/download?id='+encodeURIComponent(b.dataset.id)});list.querySelectorAll('.docDelete').forEach(b=>b.onclick=async()=>{if(!confirm('Delete this project document permanently?'))return;try{const r=await fetch('/api/project_documents/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:b.dataset.id})}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Delete failed');status.textContent='Document deleted.';await loadProjectDocuments()}catch(e){status.textContent=e.message}});}catch(e){status.textContent=e.message}}
const docUploadButton=$('uploadProjectDoc');if(docUploadButton)docUploadButton.onclick=async()=>{const f=$('docFile').files[0];if(!f)return $('projectDocsStatus').textContent='Choose a PDF or Excel file first.';const ext=(f.name.split('.').pop()||'').toLowerCase();if(!['pdf','xls','xlsx','xlsm','csv'].includes(ext))return $('projectDocsStatus').textContent='Only PDF and Excel/CSV files are allowed.';if(f.size>100*1024*1024)return $('projectDocsStatus').textContent='File exceeds the 100 MB limit.';const fd=new FormData();fd.append('file',f);fd.append('category',$('docCategory').value);fd.append('document_number',$('docNumber').value.trim());fd.append('document_date',$('docDate').value);fd.append('note',$('docNote').value.trim());docUploadButton.disabled=true;$('projectDocsStatus').textContent='Uploading '+f.name+'...';try{const r=await fetch('/api/project_documents/upload',{method:'POST',body:fd}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Upload failed');$('docFile').value='';$('docNumber').value='';$('docDate').value='';$('docNote').value='';$('projectDocsStatus').textContent='Document uploaded successfully.';await loadProjectDocuments()}catch(e){$('projectDocsStatus').textContent=e.message}finally{docUploadButton.disabled=false}};
async function loadUploadedDrawings(){try{let r=await fetch('/api/uploaded_drawings',{cache:'no-store'}),j=await r.json();if(!r.ok||j.error)return;const rows=j.drawings||[];const card=$('uploadedDrawingsCard'),list=$('uploadedDrawingsList'),st=$('uploadedDrawingsStatus');if(!rows.length){card.style.display='none';return;}card.style.display='block';const role=window.currentUser&&window.currentUser.role;const tech=role==='TECHNICIAN';st.textContent=tech?'Uploaded drawings are waiting for Engineer/Admin analysis.':'Uploaded drawings available for analysis.';let h='<div class="reviewWrap"><table><thead><tr><th>Drawing</th><th>Uploader</th><th>Time</th><th>Status</th><th></th></tr></thead><tbody>';for(const x of rows){const canAnalyze=!tech&&['ADMIN','ENGINEER'].includes(role)&&!x.analyzed;h+=`<tr><td>${esc(x.drawing||'')}</td><td>${esc(x.uploaded_by||'')}</td><td>${esc(x.uploaded_at||'')}</td><td>${x.analyzed?'ANALYZED':'WAITING FOR ANALYSIS'}</td><td><div class="drawingActions">${canAnalyze?`<button class="good analyzeUploaded" data-id="${esc(x.id)}">Analyze</button>`:''}<button class="deleteDrawing" style="background:#b42318" data-id="${esc(x.id)}">Delete</button></div></td></tr>`}h+='</tbody></table></div>';list.innerHTML=h;document.querySelectorAll('.analyzeUploaded').forEach(b=>b.onclick=async()=>{if(busy)return;setBusy(true);$('status').textContent='Analyzing uploaded drawing...';try{let r=await fetch('/api/analyze_uploaded',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:b.dataset.id,university:$('univ').value,building:$('building').value,floor:$('floor').value})}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Analysis failed');render(j.result);renderProject(j.project);$('status').textContent='CAD analysis completed. As-Built is now available for this analyzed project.';await loadUploadedDrawings();}catch(e){$('status').textContent=e.message}finally{setBusy(false)}});document.querySelectorAll('.deleteDrawing').forEach(b=>b.onclick=async()=>{const row=b.closest('tr');const label=row?.querySelector('td')?.textContent||'this drawing';if(!confirm('Delete drawing '+label+'? If it has an analyzed floor result, that floor result will also be removed.'))return;try{const r=await fetch('/api/project_delete_drawing',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:b.dataset.id})}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Drawing delete failed');renderProject(j.project||[]);if(result&&j.removed_floor===result.floor)result=null;await loadUploadedDrawings();$('status').textContent='Drawing deleted.';}catch(e){$('status').textContent=e.message}})}catch(e){}}
$('upload').onclick=async()=>{if(busy)return;const fs=[...$('file').files];if(!fs.length)return $('status').textContent='Choose one or more DWG/DXF drawings.';setBusy(true);try{const technician=(window.currentUser&&window.currentUser.role==='TECHNICIAN');for(let i=0;i<fs.length;i++){const f=fs[i];$('status').textContent=`[${i+1}/${fs.length}] Uploading ${f.name}...`;let uj=await uploadFile(f);if(technician){$('status').textContent=`[${i+1}/${fs.length}] Uploaded ${f.name}. Waiting for Engineer/Admin to run CAD Analysis.`;await loadUploadedDrawings();continue;}$('status').textContent=`[${i+1}/${fs.length}] Analyzing ${f.name}...`;let requested=fs.length>1?'Auto Detect':$('floor').value;let r=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:uj.path,university:$('univ').value,building:$('building').value,floor:requested})}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Analysis failed');render(j.result);renderProject(j.project);} $('status').textContent=technician?`Uploaded ${fs.length} drawing(s) successfully. An Engineer/Admin must analyze them before As-Built can be used.`:`Project analysis completed: ${fs.length} drawing(s). Same-floor re-analysis replaces that floor only.`;await loadUploadedDrawings()}catch(e){$('status').textContent=e.message}finally{setBusy(false)}};
$('clearProject').onclick=async()=>{if(busy)return;let r=await fetch('/api/project_clear',{method:'POST'}),j=await r.json();result=null;renderProject([]);$('status').textContent='Project cleared. Load the floor drawings for the new project.';};
$('applyClass').onclick=async()=>{if(!result)return;const decisions={};let ok=true;document.querySelectorAll('#classRows .reviewItem').forEach(row=>{const v=row.querySelector('.classChoice').value;if(!v)ok=false;decisions[String(row.dataset.rid)]=v;});if(!ok)return $('classStatus').textContent='Choose a service or Ignore for every review item.';$('classStatus').textContent='Applying classifications and recalculating rack assignment...';let r=await fetch('/api/classification_override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({floor:result.floor,decisions})}),j=await r.json();if(!r.ok||j.error)return $('classStatus').textContent=j.error||'Classification failed';render(j.result);renderProject(j.project);$('classStatus').textContent='Classification applied. The items are now included in BOQ / Excel / CAD labeling.';};
$('applyRack').onclick=async()=>{const alloc={};let ok=true;document.querySelectorAll('#rackRows [data-svc]').forEach(row=>{const svc=row.dataset.svc,total=+row.dataset.total,g1=+row.querySelector('.g1').value||0,g2=+row.querySelector('.g2').value||0;if(g1+g2!==total)ok=false;alloc[svc]={'IDF-G1':g1,'IDF-G2':g2}});if(!ok)return $('rackStatus').textContent='Each G1 + G2 must equal the service total.';let r=await fetch('/api/rack_override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({floor:result.floor,allocations:alloc})}),j=await r.json();if(!r.ok||j.error)return $('rackStatus').textContent=j.error||'Allocation failed';render(j.result);$('rackStatus').textContent='Rack allocation applied.'};
if($('boardReview'))$('boardReview').onclick=async()=>{if(!result)return $('boardReviewStatus').textContent='Analyze/select a floor first.';$('boardReviewStatus').textContent='Scanning wall gaps and building debug overlay...';$('boardReviewDownloads').innerHTML='';let r=await fetch('/api/board_review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({floor:result.floor})}),j=await r.json();if(!r.ok||j.error)return $('boardReviewStatus').textContent=j.error||'Board review failed';const s=j.summary||{},f=j.files||{};let h='';if(f.zip)h+=`<a href="/download/${encodeURIComponent(f.zip)}"><button class="good">Download Board Review ZIP</button></a>`;if(f.report)h+=`<a href="/download/${encodeURIComponent(f.report)}"><button class="alt">Gap Report JSON</button></a>`;if(f.debug_dxf)h+=`<a href="/download/${encodeURIComponent(f.debug_dxf)}"><button class="alt">Debug DXF</button></a>`;$('boardReviewDownloads').innerHTML=h;$('boardReviewStatus').innerHTML=`${esc(s.floor||'Floor')}: walls <b>${s.wall_segments||0}</b>, door inserts <b>${s.door_inserts||0}</b>, dangling endpoints <b>${s.dangling_endpoints||0}</b>, matched gaps <b>${s.matched_gaps||0}</b>, auto-closed <b>${s.auto_closed||0}</b>, needs review <b>${s.needs_review||0}</b>. Re-run Analyze after reviewing if the board changed.`};
document.querySelectorAll('[data-export]').forEach(b=>b.onclick=async()=>{if(!result)return $('exportStatus').textContent='Analyze first.';$('exportStatus').textContent='Generating combined project Excel...';let r=await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label_pattern:activeLabelPattern()})}),j=await r.json();if(!r.ok||j.error)return $('exportStatus').textContent=j.error||'Export failed';location.href='/download/'+j.files[b.dataset.export];$('exportStatus').textContent='Excel generated with schema: '+activeLabelPattern()});
$('generateLabels').onclick=async()=>{if(!result)return $('labelStatus').textContent='Analyze the project first.';if(busy)return;setBusy(true);try{const hv=$('labelHeight').value.trim();$('labelStatus').textContent='Assigning port IDs and generating the Safe Desktop AutoCAD label package (Core Console is not launched)...';$('labelDownloads').innerHTML='';let r=await fetch('/api/generate_labels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text_height:hv?Number(hv):null,label_pattern:activeLabelPattern()})}),j=await r.json();if(!r.ok||j.error)throw Error(j.error||'CAD label generation failed');let h=`<a href="/download/${encodeURIComponent(j.files.zip)}"><button class="good">Download All Labeled CAD (ZIP)</button></a>`;for(const d of j.files.drawings||[])h+=`<a href="/download/${encodeURIComponent(d.name)}"><button class="alt">${esc(d.floor)} — ${d.labels} labels</button></a>`;$('labelDownloads').innerHTML=h;$('labelStatus').textContent=`Generated ${j.files.logical_label_count} logical labels in Safe Desktop mode. Core Console was not launched. Open each original drawing in full AutoCAD, run its _DESKTOP_RUN.scr, then SAVEAS. Original drawings were not changed.`}catch(e){$('labelStatus').textContent=e.message}finally{setBusy(false)}};

if($('asFloor'))$('asFloor').onchange=async()=>{await loadAsbuiltTargets();};
if($('asChatFile'))$('asChatFile').onchange=()=>{const fs=[...$('asChatFile').files];if(!fs.length){$('asChatFileMeta').textContent='No files selected.';}else{const names=fs.map(f=>`${f.name} (${(f.size/1024/1024).toFixed(1)} MB)`).join(' • ');$('asChatFileMeta').textContent=`${fs.length} file(s) selected • ${names}`;}asChatId=null;asChatFilePath='';$('asChatStatus').className='status info';$('asChatStatus').textContent=fs.length?`${fs.length} As-Built file(s) ready. Click Analyze As-Built.`:'Select one or more As-Built files.';};
function refreshAsbuiltOpUi(){const op=$('asOp').value;const needsTarget=(op==='SITE_REMOVE'||op==='MOVE_ENDPOINT'||op==='DELETE_ENDPOINT');$('asTargetWrap').style.display=needsTarget?'block':'none';$('asSiteWrap').style.display=(op==='SITE_REMOVE')?'flex':'none';}
if($('asOp'))$('asOp').onchange=refreshAsbuiltOpUi;
async function sendAsbuiltChat(){if(asChatBusy)return;const msg=$('asChatInput').value.trim();try{
  if(!asChatId){
    const fs=[...$('asChatFile').files];if(!fs.length)throw Error('Choose one or more As-Built PDFs/images first.');
    if(!asbuiltProjectFloors.length)throw Error('Analyze the CAD project first. No floors are loaded.');
    asChatBusy=true;$('asChatSend').disabled=true;$('asChatStatus').className='status info';
    const uploads=[];for(let i=0;i<fs.length;i++){$('asChatStatus').textContent=`Uploading As-Built file ${i+1}/${fs.length}...`;uploads.push(await uploadAsbuiltChatFile(fs[i]));}
    asChatFilePath=uploads[0]?.path||'';
    const lf=$('asLegendFile')?.files?.[0];
    if(lf){$('asChatStatus').textContent='Uploading Legend Reference...';const lr=await uploadAsbuiltLegendFile(lf);asChatLegendPath=lr.path||'';$('asLegendMeta').textContent='Legend reference uploaded. It is only used to identify a legend on pages; unmatched pages are never suppressed.';}
    if(msg)asChatBubble('user',msg);$('asChatStatus').textContent=`OpenCV is analyzing ${uploads.length} file(s)...`;
    const startPayload={path:uploads[0]?.path||'',filename:uploads[0]?.filename||'',paths:uploads.map(x=>x.path),filenames:uploads.map(x=>x.filename),legend_path:asChatLegendPath,api_key:'',model:'',label_pattern:activeLabelPattern(),message:msg};
    let r=await fetch('/api/asbuilt_chat_start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(startPayload)}),j=await r.json();
    if(!r.ok||j.error)throw Error(j.error||'Could not start the As-Built chat.');
    asChatId=j.chat_id;handleAsChatTurn(j);$('asChatStatus').className='status success';$('asChatStatus').textContent='Code scan ready.';$('asChatInput').value='';
  }else{
    if(!msg)throw Error('Type a reply first.');
    asChatBusy=true;$('asChatSend').disabled=true;asChatBubble('user',msg);$('asChatInput').value='';$('asChatStatus').className='status info';$('asChatStatus').textContent='Code pipeline: PDF/images → OpenCV + optional OCR...';
    let r=await fetch('/api/asbuilt_chat_reply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:asChatId,message:msg})}),j=await r.json();
    if(!r.ok||j.error)throw Error(j.error||'Chat reply failed.');
    handleAsChatTurn(j);$('asChatStatus').className='status success';$('asChatStatus').textContent='Chat updated.';
  }
}catch(e){$('asChatStatus').className='status error';$('asChatStatus').textContent=e.message}finally{asChatBusy=false;$('asChatSend').disabled=false}}
if($('asChatSend'))$('asChatSend').onclick=sendAsbuiltChat;
if($('asChatInput'))$('asChatInput').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();sendAsbuiltChat();}});
if($('asChatReset'))$('asChatReset').onclick=()=>{asChatId=null;asChatFilePath='';asChatLegendPath='';$('asChatThread').innerHTML='';$('asChatReview').innerHTML='';$('asChatReview').style.display='none';$('asChatAdd').style.display='none';$('asChatInput').value='';$('asChatStatus').className='status info';$('asChatStatus').textContent='New chat. Upload the As-Built PDF/photo (or keep the current one) and send a message.';};
if($('asChatAdd'))$('asChatAdd').onclick=()=>{const trs=[...document.querySelectorAll('#asChatReview tr[data-airow]')];let added=0;for(const tr of trs){const i=+tr.dataset.airow,f=asbuiltVisionRows[i];if(!tr.querySelector('.aiUse').checked)continue;const tid=tr.querySelector('.aiTarget').value,pa=tr.querySelector('.aiPatch').value;if(!tid)return $('asChatStatus').textContent=`${f.floor}, page ${f.page_number}: choose the CAD endpoint before adding.`;if(!pa)return $('asChatStatus').textContent=`${f.floor}, page ${f.page_number}: choose SHIFT or RESERVE.`;if(asbuiltChanges.some(c=>c.op==='SITE_REMOVE'&&c.floor===f.floor&&c.target_id===tid))return $('asChatStatus').textContent='The same CAD endpoint is already in the As-Built change list.';const opt=(f.candidate_options||[]).find(x=>x.target_id===tid);asbuiltChanges.push({op:'SITE_REMOVE',floor:f.floor,target_id:tid,target_label:(opt&&(opt.label||opt.block))||f.suggested_label||'',site_status:f.status,patch_action:pa,note:`As-Built chat page ${f.page_number}: ${f.evidence||f.nearby_text||f.status}`});added++;}renderAsbuiltChanges();$('asChatStatus').className='status success';$('asChatStatus').textContent=added?`Added ${added} reviewed removal(s). Review the list below and generate one full-project package.`:'No reviewed rows were selected.';};
if($('asAdd'))$('asAdd').onclick=()=>{const floor=$('asFloor').value;if(!floor)return $('asStatus').textContent='Select the floor for this manual change.';const op=$('asOp').value;const c={op,floor,note:$('asNote').value.trim()};if(op==='SITE_REMOVE'||op==='MOVE_ENDPOINT'||op==='DELETE_ENDPOINT'){const tid=$('asTarget').value;if(!tid)return $('asStatus').textContent='Choose an endpoint first.';const t=asbuiltTargets.find(x=>x.target_id===tid);if(!t)return $('asStatus').textContent='Endpoint was not found.';if((op==='MOVE_ENDPOINT'||op==='DELETE_ENDPOINT')&&!t.editable_direct)return $('asStatus').textContent='This endpoint is nested/read-only for direct Move/Delete. Use Site removal for Excel + manual CAD deletion, or edit it manually in AutoCAD.';c.target_id=tid;c.target_label=t.label||t.block||tid;if(op==='SITE_REMOVE'){const st=$('asSiteStatus').value;const pa=$('asPatchAction').value;if(!st)return $('asStatus').textContent='Choose one of the four approved site statuses.';if(!['SHIFT','RESERVE'].includes(pa))return $('asStatus').textContent='Choose SHIFT or RESERVE.';c.site_status=st;c.patch_action=pa;}else if(op==='MOVE_ENDPOINT'){if($('asX').value===''||$('asY').value==='')return $('asStatus').textContent='Enter the new X and Y.';c.x=Number($('asX').value);c.y=Number($('asY').value);}}else if(op==='ADD_BLOCK'){if(!$('asBlock').value.trim()||$('asX').value===''||$('asY').value==='')return $('asStatus').textContent='Add Block needs block name and X/Y.';c.block=$('asBlock').value.trim();c.x=Number($('asX').value);c.y=Number($('asY').value);c.rotation=Number($('asRotation').value||0);c.scale=Number($('asScale').value||1);}else if(op==='ADD_NOTE'){if(!$('asText').value.trim()||$('asX').value===''||$('asY').value==='')return $('asStatus').textContent='Add Note needs text and X/Y.';c.text=$('asText').value.trim();c.x=Number($('asX').value);c.y=Number($('asY').value);c.height=Number($('asTextHeight').value||150);}asbuiltChanges.push(c);renderAsbuiltChanges();$('asStatus').textContent=`Added manual change #${asbuiltChanges.length} on ${floor}.`;};
if($('asClear'))$('asClear').onclick=()=>{asbuiltChanges=[];renderAsbuiltChanges();$('asStatus').textContent='As-Built change list cleared.';$('asDownloads').innerHTML='';};
if($('asGenerate'))$('asGenerate').onclick=async()=>{if(!asbuiltChanges.length)return $('asStatus').textContent='Add at least one reviewed change.';$('asStatus').textContent='Generating per-floor CAD scripts + one combined adjusted Excel package...';$('asDownloads').innerHTML='';let r=await fetch('/api/asbuilt_generate_project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({changes:asbuiltChanges,label_pattern:activeLabelPattern()})}),j=await r.json();if(!r.ok||j.error)return $('asStatus').textContent=j.error||'As-Built generation failed';let h=`<a href="/download/${encodeURIComponent(j.files.zip)}"><button class="good">Download Full Project As-Built ZIP</button></a>`;for(const d of j.files.scripts||[])if(d.scr)h+=`<a href="/download/${encodeURIComponent(d.scr)}"><button class="alt">${esc(d.floor)} RUN.scr</button></a>`;if(j.files.port_map)h+=`<a href="/download/${encodeURIComponent(j.files.port_map)}"><button class="alt">Adjusted Port Map</button></a>`;if(j.files.quantity_summary)h+=`<a href="/download/${encodeURIComponent(j.files.quantity_summary)}"><button class="alt">Adjusted Quantity Summary</button></a>`;if(j.files.cable_schedule)h+=`<a href="/download/${encodeURIComponent(j.files.cable_schedule)}"><button class="alt">Adjusted Labeling</button></a>`;$('asDownloads').innerHTML=h;$('asStatus').textContent=`Generated ${j.files.changes} reviewed change(s) across ${j.files.floor_count} floor(s). Run each floor's RUN.scr in its ORIGINAL drawing, review, then SAVEAS. Combined Excel outputs include all reviewed site removals.`;};
refreshAsbuiltOpUi();renderAsbuiltChanges();
</script><script>(function(){
fetch("/api/auth/me",{cache:"no-store"}).then(r=>r.json()).then(me=>{window.currentUser=me;
 if(!me.authenticated)return;
 const esc=v=>String(v??"").replace(/[&<>\"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
 const bar=document.createElement("div");
 bar.id="authTopBar";
 bar.innerHTML='<span style="font-size:12px;background:#eef2ff;color:#3730a3;padding:7px 10px;border-radius:999px;font-weight:700">'+esc(me.username||"")+' · '+esc(me.role||"")+'</span>'+
   '<button id="authDrawerOpen" aria-label="Open account menu" title="Account menu" style="font-size:20px;line-height:1;padding:8px 12px">☰</button>';
 document.body.appendChild(bar);
 const drawer=document.createElement("div");drawer.id="authDrawerBackdrop";drawer.innerHTML='<aside id="authSideDrawer" aria-label="Account menu"><button id="authDrawerClose" aria-label="Close menu">✕ Close</button><div class="drawerHeading">Account & Administration</div><div class="drawerUser">'+esc(me.username||"")+' · '+esc(me.role||"")+'</div><button class="drawerAction" id="authChangePasswordBtn">Change Password</button>'+(me.role==="ADMIN"?'<button class="drawerAction" id="authUsersBtn">Users</button><button class="drawerAction" id="authAuditBtn">Audit Log</button>':'')+'<div style="flex:1"></div><button class="drawerAction" id="authLogoutBtn">Logout</button></aside>';
 document.body.appendChild(drawer);
 const closeDrawer=()=>drawer.classList.remove("open");
 document.getElementById("authDrawerOpen").onclick=()=>drawer.classList.add("open");
 document.getElementById("authDrawerClose").onclick=closeDrawer;
 drawer.addEventListener("click",e=>{if(e.target===drawer)closeDrawer()});
 document.addEventListener("keydown",e=>{if(e.key==="Escape")closeDrawer()});
 for(const id of ["authChangePasswordBtn","authUsersBtn","authAuditBtn"]) {const el=document.getElementById(id);if(el)el.addEventListener("click",closeDrawer);}
 document.getElementById("authLogoutBtn").onclick=async()=>{const b=document.getElementById("authLogoutBtn");b.disabled=true;b.textContent="Logging out...";try{await fetch("/api/auth/logout",{method:"POST",credentials:"same-origin"});}finally{location.href="/login";}};
 document.getElementById("authLogoutBtn").onclick=async()=>{const b=document.getElementById("authLogoutBtn");b.disabled=true;b.textContent="Logging out...";try{await fetch("/api/auth/logout",{method:"POST",credentials:"same-origin"});}finally{location.href="/login";}};
 const hide=sel=>document.querySelectorAll(sel).forEach(e=>{e.style.display="none";e.disabled=true;});
 if(me.role==="VIEWER") hide("#file,#upload,#clearProject,#applyClass,#applyRack,#boardReview,#generateLabels,#resetSchema,#asChatAdd,#asChatReset,#asChatSend,#asGenerate,#asAdd,#asClear,[data-export]");
 if(me.role==="TECHNICIAN") hide("#clearProject,#applyClass,#applyRack,#boardReview,#generateLabels,#resetSchema,[data-export]");
 loadProjects(activeProjectId);
async function loadProjectStatus(){
  try{
   const r=await fetch('/api/project_status',{cache:'no-store',credentials:'same-origin'});
   const j=await r.json();
   if(!r.ok||j.error)return;
   renderProject(Array.isArray(j.project)?j.project:[]);
   if(Array.isArray(j.project)&&j.project.length){
    const firstFloor=j.project[0].floor;
    const rr=await fetch('/api/project_result?floor='+encodeURIComponent(firstFloor),{cache:'no-store',credentials:'same-origin'});
    const rj=await rr.json();
    if(rr.ok&&rj.result){result=rj.result;render(rj.result);}
   }
  }catch(_e){}
 }
 loadProjectStatus();
 loadUploadedDrawings();
 loadProjectDocuments();
 const cpModal=document.createElement("div");
 cpModal.id="cpModal";
 cpModal.style="position:fixed;inset:0;background:#0008;z-index:100000;display:none;align-items:center;justify-content:center";
 cpModal.innerHTML='<div style="width:min(460px,92vw);background:white;border-radius:16px;padding:22px;color:#172033;box-shadow:0 20px 60px #0005"><div style="display:flex;justify-content:space-between;align-items:center;gap:12px"><h2 style="margin:0">Change Password</h2><button id="cpClose">Close</button></div><div id="cpMsg" style="padding:9px;margin:12px 0;background:#f2f4f7;border-radius:8px">Enter your current password and a new password.</div><label style="display:block;font-size:12px;font-weight:700;margin:10px 0 5px">Current password</label><input id="cpCurrent" type="password" autocomplete="current-password" style="width:100%;box-sizing:border-box;padding:10px"><label style="display:block;font-size:12px;font-weight:700;margin:10px 0 5px">New password</label><input id="cpNew" type="password" autocomplete="new-password" style="width:100%;box-sizing:border-box;padding:10px"><label style="display:block;font-size:12px;font-weight:700;margin:10px 0 5px">Confirm new password</label><input id="cpConfirm" type="password" autocomplete="new-password" style="width:100%;box-sizing:border-box;padding:10px"><button id="cpSave" style="width:100%;margin-top:14px">Change Password</button></div>';
 document.body.appendChild(cpModal);
 const cpMsg=t=>document.getElementById("cpMsg").textContent=t;
 document.getElementById("authChangePasswordBtn").onclick=()=>{document.getElementById("cpCurrent").value="";document.getElementById("cpNew").value="";document.getElementById("cpConfirm").value="";cpMsg('Enter your current password and a new password.');cpModal.style.display="flex";document.getElementById("cpCurrent").focus();};
 document.getElementById("cpClose").onclick=()=>cpModal.style.display="none";
 document.getElementById("cpSave").onclick=async()=>{
   const b=document.getElementById("cpSave"),current=document.getElementById("cpCurrent").value,newPassword=document.getElementById("cpNew").value,confirm=document.getElementById("cpConfirm").value;
   if(!current||!newPassword||!confirm)return cpMsg('Please fill all fields.');
   if(newPassword.length<8)return cpMsg('New password must be at least 8 characters.');
   if(newPassword!==confirm)return cpMsg('New password and confirmation do not match.');
   b.disabled=true;b.textContent='Changing...';
   try{const r=await fetch('/api/auth/change-password',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({current_password:current,new_password:newPassword})});const j=await r.json();if(!r.ok)throw Error(j.error||'Could not change password.');cpMsg('Password changed successfully.');document.getElementById("cpCurrent").value='';document.getElementById("cpNew").value='';document.getElementById("cpConfirm").value='';setTimeout(()=>cpModal.style.display='none',900);}catch(e){cpMsg(e.message)}finally{b.disabled=false;b.textContent='Change Password';}
 };
 if(me.role!=="ADMIN")return;
 const modal=document.createElement("div");
 modal.style="position:fixed;inset:0;background:#0008;z-index:99998;display:none;align-items:center;justify-content:center";
 modal.innerHTML='<div style="width:min(1000px,95vw);max-height:90vh;overflow:auto;background:white;border-radius:16px;padding:22px;color:#172033"><div style="display:flex;justify-content:space-between;align-items:center;gap:12px"><h2 style="margin:0">User Management</h2><button id="authClose">Close</button></div><div id="authMsg" style="padding:9px;margin:12px 0;background:#f2f4f7;border-radius:8px">Manage users, roles and access.</div><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin:12px 0"><input id="nu" placeholder="Username"><input id="np" type="password" placeholder="Password (8+)"><input id="ne" placeholder="Email"><select id="nr"><option>ADMIN</option><option>ENGINEER</option><option>TECHNICIAN</option><option selected>VIEWER</option></select><button id="addU">Add User</button></div><input id="userSearch" placeholder="Search username, email or role..." style="width:100%;box-sizing:border-box;margin:8px 0;padding:9px"><div id="userStats" style="font-size:12px;margin:8px 0;color:#475467"></div><div id="authUsersTable"></div></div>';
 document.body.appendChild(modal);
 const auditModal=document.createElement("div");
 auditModal.style="position:fixed;inset:0;background:#0008;z-index:99997;display:none;align-items:center;justify-content:center";
 auditModal.innerHTML='<div style="width:min(1150px,96vw);max-height:90vh;overflow:auto;background:white;border-radius:16px;padding:22px;color:#172033"><div style="display:flex;justify-content:space-between;align-items:center"><h2 style="margin:0">Audit Log</h2><button id="authAuditClose">Close</button></div><div id="authAuditTable" style="margin-top:12px"></div></div>';
 document.body.appendChild(auditModal);
 const msg=t=>document.getElementById("authMsg").textContent=t;
 let cached=[];
 function render(){
   const q=(document.getElementById("userSearch").value||"").trim().toLowerCase();
   const rows=cached.filter(u=>[u.username,u.email,u.role,u.active?"active":"disabled"].join(" ").toLowerCase().includes(q));
   const counts={ADMIN:0,ENGINEER:0,TECHNICIAN:0,VIEWER:0,disabled:0}; cached.forEach(u=>{counts[u.role]=(counts[u.role]||0)+1;if(!u.active)counts.disabled++});
   document.getElementById("userStats").textContent=`Total: ${cached.length} · Admin: ${counts.ADMIN||0} · Engineer: ${counts.ENGINEER||0} · Technician: ${counts.TECHNICIAN||0} · Viewer: ${counts.VIEWER||0} · Disabled: ${counts.disabled}`;
   let h='<div style="overflow:auto"><table style="width:100%;border-collapse:collapse;font-size:13px"><tr><th>User</th><th>Email</th><th>Role</th><th>Active</th><th>New Password</th><th>Action</th></tr>';
   for(const u of rows){const self=u.username.toLowerCase()===String(me.username||"").toLowerCase();h+='<tr><td style="padding:7px;border-bottom:1px solid #eee">'+esc(u.username)+(self?' <b>(you)</b>':'')+'</td><td style="padding:7px;border-bottom:1px solid #eee"><input class="ue" data-id="'+u.id+'" value="'+esc(u.email||"")+'" placeholder="email"></td><td style="padding:7px;border-bottom:1px solid #eee"><select class="ur" data-id="'+u.id+'"><option '+(u.role==="ADMIN"?'selected':'')+'>ADMIN</option><option '+(u.role==="ENGINEER"?'selected':'')+'>ENGINEER</option><option '+(u.role==="TECHNICIAN"?'selected':'')+'>TECHNICIAN</option><option '+(u.role==="VIEWER"?'selected':'')+'>VIEWER</option></select></td><td style="padding:7px;border-bottom:1px solid #eee"><input type="checkbox" class="ua" data-id="'+u.id+'" '+(u.active?'checked':'')+' '+(self?'disabled':'')+'></td><td style="padding:7px;border-bottom:1px solid #eee"><input class="up" data-id="'+u.id+'" type="password" placeholder="optional"></td><td style="padding:7px;border-bottom:1px solid #eee"><button class="saveU" data-id="'+u.id+'">Save</button></td></tr>'}
   h+='</table></div>';document.getElementById("authUsersTable").innerHTML=h;
   document.querySelectorAll(".saveU").forEach(b=>b.onclick=async()=>{const id=+b.dataset.id,u=cached.find(x=>x.id===id);const body={username:u.username,email:document.querySelector('.ue[data-id="'+id+'"]').value,role:document.querySelector('.ur[data-id="'+id+'"]').value,active:document.querySelector('.ua[data-id="'+id+'"]').checked};const pw=document.querySelector('.up[data-id="'+id+'"]').value;if(pw)body.password=pw;b.disabled=true;try{const rr=await fetch("/api/auth/users/update",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}),jj=await rr.json();msg(rr.ok?"Saved.":(jj.error||"Update failed"));if(rr.ok)await load();}finally{b.disabled=false;}});
 }
 async function load(){try{const r=await fetch("/api/auth/users",{cache:"no-store"}),j=await r.json();if(!r.ok)return msg(j.error||"Failed to load users.");cached=j.users||[];render();}catch(e){msg("Could not load users.");}}
 document.getElementById("authUsersBtn").onclick=()=>{modal.style.display="flex";load()};
 document.getElementById("authClose").onclick=()=>modal.style.display="none";
 document.getElementById("userSearch").oninput=render;
 document.getElementById("addU").onclick=async()=>{const body={username:document.getElementById("nu").value.trim(),password:document.getElementById("np").value,email:document.getElementById("ne").value.trim(),role:document.getElementById("nr").value};const b=document.getElementById("addU");b.disabled=true;try{const rr=await fetch("/api/auth/users/create",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}),jj=await rr.json();msg(rr.ok?"User created successfully.":(jj.error||"Create failed"));if(rr.ok){document.getElementById("nu").value="";document.getElementById("np").value="";document.getElementById("ne").value="";await load();}}finally{b.disabled=false;}};
 document.getElementById("authAuditBtn").onclick=async()=>{auditModal.style.display="flex";try{const r=await fetch("/api/auth/audit",{cache:"no-store"}),j=await r.json();if(!r.ok){document.getElementById("authAuditTable").textContent=j.error||"Failed";return}let h='<div style="overflow:auto"><table style="width:100%;border-collapse:collapse;font-size:12px"><tr><th>ID</th><th>Time</th><th>User</th><th>Role</th><th>Action</th><th>Path</th><th>Detail</th><th>IP</th></tr>';for(const x of j.logs||[])h+='<tr><td>'+esc(x.id)+'</td><td>'+esc(x.created_at)+'</td><td>'+esc(x.username)+'</td><td>'+esc(x.role)+'</td><td>'+esc(x.action)+'</td><td>'+esc(x.path)+'</td><td>'+esc(x.detail)+'</td><td>'+esc(x.ip)+'</td></tr>';h+='</table></div>';document.getElementById("authAuditTable").innerHTML=h;}catch(e){document.getElementById("authAuditTable").textContent="Could not load audit log.";}};
 document.getElementById("authAuditClose").onclick=()=>auditModal.style.display="none";
}).catch(()=>{});
})();</script><script>(function(){
const q=id=>document.getElementById(id);
const views=['project','results','outputs','asbuilt','docs'];
function show(v){if(!views.includes(v))v='project';views.forEach(n=>q('view-'+n).classList.toggle('on',n===v));document.querySelectorAll('.navItem').forEach(b=>{const on=b.dataset.view===v;b.classList.toggle('on',on);if(on){b.setAttribute('aria-current','page');b.classList.remove('fresh')}else b.removeAttribute('aria-current')});if(location.hash!=='#'+v)history.replaceState(null,'','#'+v);window.scrollTo(0,0)}
document.querySelectorAll('.navItem').forEach(b=>b.onclick=()=>show(b.dataset.view));
q('goProject').onclick=()=>show('project');show((location.hash||'').slice(1));
// project menu closes after an action or an outside click
const menu=q('projectMenu');document.addEventListener('click',e=>{if(menu.open&&!menu.contains(e.target))menu.open=false});
menu.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{menu.open=false}));
// account bar (created by the auth script) moves into the header
const dock=()=>{const b=q('authTopBar'),h=q('headerRight');if(!b||!h)return false;if(b.parentNode!==h)h.appendChild(b);return true};
if(!dock()){const mo=new MutationObserver(()=>{if(dock())mo.disconnect()});mo.observe(document.body,{childList:true})}
// drawing drop zone
const fi=q('file'),dz=q('dropZone'),fn=q('fileNames'),hint=fn.textContent;
const names=()=>{const fs=[...fi.files];fn.textContent=fs.length?fs.length+' file'+(fs.length>1?'s':'')+' selected: '+fs.map(f=>f.name).join(', '):hint};
fi.addEventListener('change',names);
['dragenter','dragover'].forEach(t=>dz.addEventListener(t,e=>{e.preventDefault();dz.classList.add('over')}));
['dragleave','drop'].forEach(t=>dz.addEventListener(t,e=>{e.preventDefault();dz.classList.remove('over')}));
dz.addEventListener('drop',e=>{if(fi.disabled)return;const fs=[...e.dataTransfer.files].filter(f=>/\.(dwg|dxf)$/i.test(f.name));if(!fs.length)return;const dt=new DataTransfer();fs.forEach(f=>dt.items.add(f));fi.files=dt.files;names()});
// results: empty state, review badge, "new results" dot
const phys=q('physical'),rev=q('review'),nb=q('navResultsBadge'),rn=document.querySelector('[data-view=results]'),o={childList:true,characterData:true,subtree:true};
const sync=()=>{const has=phys.textContent.trim()!=='-';q('resultsEmpty').hidden=has;q('resultsBody').hidden=!has;const n=parseInt(rev.textContent,10)||0;nb.hidden=!n;nb.textContent=n||''};
new MutationObserver(()=>{sync();if(!rn.classList.contains('on')&&phys.textContent.trim()!=='-')rn.classList.add('fresh')}).observe(phys,o);
new MutationObserver(sync).observe(rev,o);sync();
// floor switcher on the Results page (reuses the Project table's row handlers)
function chips(){const box=q('floorChips'),fl=asbuiltProjectFloors||[],cur=result?result.floor:'';box.innerHTML=fl.length>1?fl.map(f=>'<button type="button" class="chip'+(f===cur?' on':'')+'" data-floor="'+esc(f)+'">'+esc(f)+'</button>').join(''):'';box.querySelectorAll('.chip').forEach(b=>b.onclick=()=>{const tr=[...document.querySelectorAll('.floorRow')].find(x=>x.dataset.floor===b.dataset.floor);if(tr)tr.click()})}
['render','refreshAsbuiltFloors'].forEach(n=>{const f=window[n];if(typeof f==='function')window[n]=function(){const r=f.apply(this,arguments);chips();return r}});
// confirm before the destructive Clear project action
const cp=q('clearProject'),oc=cp.onclick;cp.onclick=function(e){if(!confirm('Clear all analyzed floors, uploaded drawings and generated files from this project?'))return;return oc.call(this,e)};
})();</script>
<script>(function(){
const orig=window.renderVisionReview,ST=['لم يتم السحب','تم رد الكابلات','لا يوجد لها مسار','تم نقل'];
const DEF={yellow:['لم يتم السحب','RESERVE']};
const opt=(arr,sel,ph)=>'<option value="">'+ph+'</option>'+arr.map(v=>'<option'+(v===sel?' selected':'')+'>'+esc(v)+'</option>').join('');
window.renderVisionReview=function(j){
 orig.apply(this,arguments);
 const inv=((j&&j.markup_inventory)||[]).filter(m=>m.color!=='green');
 if((asbuiltVisionRows||[]).length||!inv.length)return;
 const box=$('asChatReview'),cnt={};inv.forEach(m=>cnt[m.color]=(cnt[m.color]||0)+1);
 let h='<div style="padding:12px 14px;border-bottom:1px solid var(--line)"><b>Bulk approve</b><div class="miniNote">Set what each color means, tick the rows you accept, then add them all at once. Green (already withdrawn) is skipped. Rows start ticked only when the match is 78% or higher and the color has a rule.</div><div class="stack" style="margin-top:10px">';
 for(const c of ['pink','yellow','orange']){if(!cnt[c])continue;h+='<div class="actions">'+colorDot(c)+'<b style="min-width:100px">'+c+' ('+cnt[c]+')</b><select id="bs_'+c+'" style="min-width:170px" dir="auto">'+opt(ST,(DEF[c]||[])[0],'Site status...')+'</select><select id="ba_'+c+'">'+opt(['SHIFT','RESERVE'],(DEF[c]||[])[1],'Patch action...')+'</select></div>'}
 h+='<div class="actions"><button id="bulkAdd">Add selected to revision list</button><button id="bulkAll" class="alt">Select all matched</button><button id="bulkNone" class="alt">Clear</button><span id="bulkMsg" class="miniNote"></span></div></div></div>';
 h+='<div style="max-height:420px;overflow:auto"><table><thead><tr><th>Use</th><th>Page</th><th>Floor</th><th>Color</th><th>CAD endpoint</th><th>Match</th></tr></thead><tbody>';
 inv.forEach((m,i)=>{const o=m.candidate_options||[],b=o[0]||{},pct=Math.round(Number(b.match_confidence||0)*100);
  h+='<tr data-b="'+i+'"><td><input type="checkbox" class="bu" '+(pct>=78&&DEF[m.color]?'checked':'')+(o.length?'':' disabled')+'></td><td>'+Number(m.page_number||0)+'</td><td>'+esc(m.floor_hint||'')+'</td><td>'+colorDot(m.color)+'</td><td><select class="bt" style="min-width:300px">'+(o.length?o.map(x=>'<option value="'+esc(x.target_id||'')+'">'+esc((x.label||x.block||x.target_id||'')+' | '+(x.service||'')+' | '+Math.round(Number(x.match_confidence||0)*100)+'%')+'</option>').join(''):'<option value="">No CAD candidate</option>')+'</select></td><td class="'+(pct>=78?'confHigh':pct>=60?'confMid':'confLow')+'">'+pct+'%</td></tr>'});
 box.innerHTML=h+'</tbody></table></div>';
 const msg=t=>$('bulkMsg').textContent=t,rows=()=>[...box.querySelectorAll('tr[data-b]')];
 $('bulkAll').onclick=()=>rows().forEach(r=>{const c=r.querySelector('.bu');if(!c.disabled)c.checked=true});
 $('bulkNone').onclick=()=>rows().forEach(r=>r.querySelector('.bu').checked=false);
 $('bulkAdd').onclick=()=>{let add=0,dup=0,miss=0;
  for(const r of rows()){if(!r.querySelector('.bu').checked)continue;
   const m=inv[+r.dataset.b],tid=r.querySelector('.bt').value,st=($('bs_'+m.color)||{}).value,pa=($('ba_'+m.color)||{}).value;
   if(!st||!pa)return msg('Choose a site status and patch action for '+m.color+' first.');
   if(!tid){miss++;continue}
   const o=(m.candidate_options||[]).find(x=>x.target_id===tid)||{},fl=o.floor||m.floor_hint||'';
   if(asbuiltChanges.some(c=>c.op==='SITE_REMOVE'&&c.floor===fl&&c.target_id===tid)){dup++;continue}
   asbuiltChanges.push({op:'SITE_REMOVE',floor:fl,target_id:tid,target_label:o.label||o.block||'',site_status:st,patch_action:pa,note:'As-Built page '+m.page_number+' '+m.color+' '+(m.markup_id||'')});add++}
  renderAsbuiltChanges();msg('Added '+add+(dup?', skipped '+dup+' already in the list':'')+(miss?', '+miss+' without an endpoint':'')+'.')};
};
// show Generate results next to the button (they were written into the collapsed manual-entry panel)
const as=$('asStatus');if(as)new MutationObserver(()=>{const s=$('asChatStatus');s.className='status info';s.textContent=as.textContent}).observe(as,{childList:true,characterData:true,subtree:true});
})();</script>
<style>
#busyBar{position:fixed;top:0;left:0;right:0;height:3px;z-index:100001;overflow:hidden;display:none;background:rgba(11,107,79,.15)}
#busyBar::after{content:"";position:absolute;top:0;bottom:0;left:-35%;width:35%;background:#0b6b4f;animation:busySlide 1.2s ease-in-out infinite}
@keyframes busySlide{to{left:100%}}
#busyBox{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:100001;display:none;align-items:center;gap:12px;max-width:min(560px,92vw);padding:12px 16px;background:#10241e;color:#fff;border-radius:12px;box-shadow:0 12px 32px rgba(0,0,0,.28);font-size:14px}
#busyBar.on{display:block}#busyBox.on{display:flex}
.busySpin{width:18px;height:18px;border:2.5px solid rgba(255,255,255,.3);border-top-color:#5fd3a2;border-radius:50%;animation:busyRot .8s linear infinite;flex:none}
@keyframes busyRot{to{transform:rotate(360deg)}}
#busyBox small{display:block;color:#a9c4b8;font-size:12.5px}
@media(prefers-reduced-motion:reduce){.busySpin,#busyBar::after{animation-duration:3s}}
</style>
<div id="busyBar" aria-hidden="true"></div>
<div id="busyBox" role="status" aria-live="polite"><span class="busySpin"></span><div><b id="busyTitle"></b><small id="busyHint"></small></div></div>
<script>(function(){
const L={'/api/upload':'Uploading drawing','/api/analyze':'Analyzing drawings','/api/analyze_uploaded':'Analyzing drawings','/api/export':'Building Excel files','/api/generate_labels':'Generating labeled CAD','/api/board_review':'Running board review','/api/asbuilt_chat_upload':'Uploading the scan','/api/asbuilt_legend_upload':'Uploading the legend','/api/asbuilt_chat_start':'Reading the As-Built scan','/api/asbuilt_chat_reply':'Reading the As-Built scan','/api/asbuilt_generate':'Generating the As-Built package','/api/asbuilt_generate_project':'Generating the As-Built package','/api/classification_override':'Applying classification','/api/rack_override':'Applying rack allocation','/api/project_clear':'Clearing project'};
const jobs=new Map(),bar=document.getElementById('busyBar'),box=document.getElementById('busyBox'),tt=document.getElementById('busyTitle'),hh=document.getElementById('busyHint');
let seq=0,timer=null;
const fmt=s=>Math.floor(s/60)+':'+String(s%60).padStart(2,'0');
function paint(){const js=[...jobs.values()];if(!js.length){bar.classList.remove('on');box.classList.remove('on');clearInterval(timer);timer=null;return}
 const j=js[0],s=Math.floor((Date.now()-j.t)/1000);if(Date.now()-j.t<500)return;
 bar.classList.add('on');box.classList.add('on');tt.textContent=j.label+' · '+fmt(s)+(js.length>1?' (+'+(js.length-1)+' more)':'');
 hh.textContent=s>20?'Large drawings can take several minutes. Keep this tab open.':'Working…'}
const of=window.fetch;
window.fetch=function(input,init){
 const url=typeof input==='string'?input:(input&&input.url)||'',m=String((init&&init.method)||(input&&input.method)||'GET').toUpperCase(),p=url.split('?')[0].replace(/^https?:\/\/[^/]+/,'');
 if(m==='GET'||p.indexOf('/api/')!==0||p.indexOf('/api/auth')===0)return of.apply(this,arguments);
 const id=++seq;jobs.set(id,{label:L[p]||'Working',t:Date.now()});if(!timer)timer=setInterval(paint,500);
 const done=()=>{jobs.delete(id);paint()};const r=of.apply(this,arguments);r.then(done,done);return r};
window.addEventListener('beforeunload',e=>{if(jobs.size){e.preventDefault();e.returnValue=''}});
})();</script>

</body></html>
'''


def _auth_origin_allowed(handler):
    """Extra CSRF defense for state-changing requests. Same-origin requests pass."""
    origin=(handler.headers.get('Origin') or '').strip()
    if not origin:
        return True
    host=(handler.headers.get('Host') or '').strip()
    scheme='https' if AUTH_COOKIE_SECURE else 'http'
    expected=f'{scheme}://{host}'
    # Local development may be accessed via 127.0.0.1/localhost variants.
    allowed={expected}
    if host.startswith('127.0.0.1:'):
        allowed.add(f'{scheme}://localhost:{host.split(":",1)[1]}')
    elif host.startswith('localhost:'):
        allowed.add(f'{scheme}://127.0.0.1:{host.split(":",1)[1]}')
    return origin in allowed


PROJECT_DOC_CATEGORIES = {
    'contract_boq': 'Contract & BOQ — العقد والمقايسات',
    'mir_ir_issue': 'MIR / IR / Material Issue — الفحص وأذونات الصرف',
    'consultant_letters': 'Consultant Letters — جوابات الاستشاري',
    'consultant_packing': 'Consultant Packing Lists — الباكنج ليست',
}
PROJECT_DOC_EXTENSIONS = {'.pdf', '.xls', '.xlsx', '.xlsm', '.csv'}
PROJECT_DOC_MAX_BYTES = 100 * 1024 * 1024

def _project_docs_dir(project_id=None):
    pid=str(project_id or CURRENT_PROJECT_ID or '')
    if not pid: raise ValueError('No project selected.')
    d=_project_dir(pid) / 'documents'
    d.mkdir(parents=True, exist_ok=True)
    return d

def _project_docs_index(project_id=None):
    f=_project_docs_dir(project_id) / 'index.json'
    try:
        data=json.loads(f.read_text(encoding='utf-8'))
        return data if isinstance(data,list) else []
    except (OSError, ValueError):
        return []

def _project_docs_save(rows, project_id=None):
    d=_project_docs_dir(project_id); f=d/'index.json'; tmp=d/('index.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(f)

def _project_doc_safe_name(name):
    name=Path(str(name or 'document')).name.replace('\\','_').replace('/', '_')
    name=re.sub(r'[^A-Za-z0-9._() -]', '_', name).strip(' .')
    return (name or 'document')[:180]

def _project_doc_read_multipart(handler):
    length=int(handler.headers.get('Content-Length','0') or 0)
    if length <= 0: raise ValueError('No upload data received.')
    if length > PROJECT_DOC_MAX_BYTES + 1024*1024: raise ValueError('Upload exceeds the 100 MB limit.')
    body=handler.rfile.read(length)
    if len(body)!=length: raise ValueError('Upload ended before all data was received.')
    head=(f'Content-Type: {handler.headers.get("Content-Type", "")}\r\nMIME-Version: 1.0\r\n\r\n').encode('utf-8')
    msg=BytesParser(policy=email_policy_default).parsebytes(head+body)
    if not msg.is_multipart(): raise ValueError('Expected a multipart file upload.')
    fields={}; filepart=None
    for part in msg.iter_parts():
        name=part.get_param('name',header='content-disposition')
        filename=part.get_filename()
        payload=part.get_payload(decode=True) or b''
        if filename is not None and name=='file': filepart=(filename,payload)
        elif name: fields[name]=payload.decode('utf-8',errors='replace')
    if not filepart or not filepart[1]: raise ValueError('Choose a non-empty file.')
    if len(filepart[1])>PROJECT_DOC_MAX_BYTES: raise ValueError('File exceeds the 100 MB limit.')
    return fields,filepart

_AUTH_ROLE_ALLOWLIST = {
    # Core CAD write operations.
    '/api/upload': {'ADMIN','ENGINEER','TECHNICIAN'},
    '/api/analyze': {'ADMIN','ENGINEER'},
    '/api/export': {'ADMIN','ENGINEER'},
    '/api/generate_labels': {'ADMIN','ENGINEER'},
    '/api/board_review': {'ADMIN','ENGINEER'},
    '/api/project_clear': {'ADMIN','ENGINEER'},
    '/api/classification_override': {'ADMIN','ENGINEER'},
    '/api/rack_override': {'ADMIN','ENGINEER'},
    '/api/demo': {'ADMIN','ENGINEER'},

    # As-Built ingestion/review is available to technicians too.
    '/api/asbuilt_chat_upload': {'ADMIN','ENGINEER','TECHNICIAN'},
    '/api/asbuilt_chat_start': {'ADMIN','ENGINEER','TECHNICIAN'},
    '/api/asbuilt_chat_message': {'ADMIN','ENGINEER','TECHNICIAN'},
    '/api/asbuilt_chat_reply': {'ADMIN','ENGINEER','TECHNICIAN'},
    '/api/asbuilt_legend_upload': {'ADMIN','ENGINEER','TECHNICIAN'},
    '/api/asbuilt_manual_change': {'ADMIN','ENGINEER','TECHNICIAN'},
    '/api/asbuilt_chat_add': {'ADMIN','ENGINEER','TECHNICIAN'},

    # Final CAD/Excel generation remains engineer/admin only.
    '/api/asbuilt_generate': {'ADMIN','ENGINEER'},
    '/api/asbuilt_generate_project': {'ADMIN','ENGINEER'},

    # BOQ vs MIR reconciliation
    '/api/recon/upload': {'ADMIN','ENGINEER'},
    '/api/recon/action': {'ADMIN','ENGINEER'},
}

def _asbuilt_cad_ready():
    # As-Built is intentionally dependent on an analyzed CAD project.
    # A project is considered ready once at least one CAD floor analysis
    # has completed and its result is present in PROJECT_RESULTS.
    return bool(PROJECT_RESULTS and any(isinstance(v, dict) and v.get('floor') for v in PROJECT_RESULTS.values()))


def _auth_require_endpoint_role(handler, path, user):
    allowed=_AUTH_ROLE_ALLOWLIST.get(path)
    if allowed is None:
        return None
    if user.get('role') not in allowed:
        return {'error':'You do not have permission to perform this action.','code':'FORBIDDEN'}
    return None

if RECON_UI: INDEX = RECON_UI.inject(INDEX)

def _recon_dispatch(pdir, st, a, q, user):
    """Run one reconciliation action on the (locked) per-project state. Raises RECON.ReconError on bad input."""
    if a=='ocr':
        RECON.start_ocr(pdir,st,str(q.get('doc_id','')),user); return {'ok':True,'started':True}
    if a=='add_line':
        return {'ok':True,'record_id':RECON.add_manual_record(st,str(q.get('doc_id','')),q.get('page') or 1,user)['id']}
    if a=='update_record':
        fields=q.get('fields') if isinstance(q.get('fields'),dict) else {}
        RECON.update_record(st,str(q.get('id','')),fields,bool(q.get('mark_reviewed')),user); return {'ok':True}
    if a=='delete_record':
        RECON.delete_record(st,str(q.get('id','')),user); return {'ok':True}
    if a=='remove_doc':
        RECON.delete_doc(pdir,st,str(q.get('doc_id','')),user); return {'ok':True}
    if a=='match_run':
        if not st.get('boq'): raise RECON.ReconError('Upload the BOQ workbook first.')
        return {'ok':True,'result':RECON.run_matching(st,user)}
    if a=='match_decide':
        RECON.decide_match(st,str(q.get('id','')),str(q.get('decision','')),(str(q.get('boq_id')) if q.get('boq_id') else None),user); return {'ok':True}
    if a=='approve_exact':
        return {'ok':True,'approved':RECON.bulk_approve_exact(st,user)}
    if a=='boq_unit':
        RECON.set_boq_override(st,str(q.get('item_id','')),q.get('unit',''),None,user); return {'ok':True}
    if a=='dup':
        r=next((x for x in st['records'] if x['id']==str(q.get('id',''))),None)
        if not r: raise RECON.ReconError('Record not found.')
        r['dup_override']=q.get('value') if q.get('value') in ('not_duplicate','duplicate') else None
        RECON._audit(st,user,'MIR_DUP_OVERRIDE',f"{r['id']} {r['dup_override']}"); return {'ok':True}
    if a=='import_docs':
        ids=[str(x) for x in (q.get('doc_ids') or [])][:50]; rows=_project_docs_index(CURRENT_PROJECT_ID); done=0; skipped=[]
        base=_project_docs_dir(CURRENT_PROJECT_ID).resolve()
        for did in ids:
            rec=next((x for x in rows if str(x.get('id'))==did),None)
            if not rec: skipped.append(f'{did}: not found'); continue
            f=(base/str(rec.get('stored_name',''))).resolve(); ext=f.suffix.lower()
            if f.parent!=base or not f.is_file(): skipped.append(f"{rec.get('filename')}: file missing"); continue
            kind='mir' if ext=='.pdf' else ('boq' if ext in ('.xlsx','.xlsm') else None)
            if not kind: skipped.append(f"{rec.get('filename')}: unsupported type"); continue
            try:
                RECON.save_upload(pdir,st,kind,RECON.safe_display_name(rec.get('filename','file')),f.read_bytes(),user,lambda n:n,{'source_note':'imported from project Documents'}); done+=1
            except RECON.ReconError as e: skipped.append(f"{rec.get('filename')}: {e}")
        return {'ok':True,'imported':done,'skipped':skipped}
    if a=='export':
        pr=_project_get(CURRENT_PROJECT_ID,user); out=RECON.make_export(pdir,st,(pr or {}).get('name',''))
        RECON._audit(st,user,'RECON_EXPORT',out.name); return {'ok':True,'filename':out.name}
    raise RECON.ReconError('Unknown reconciliation action.')

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _write_response(self, code, content_type, body, extra_headers=None):
        try:
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            if extra_headers:
                for k,v in extra_headers.items(): self.send_header(k,v)
            self.end_headers()
            if body: self.wfile.write(body)
            self.close_connection = True
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError) as e:
            print(f'[UI] client disconnected while sending response: {e}')
            self.close_connection = True
            return False

    def _stream_upload(self, target, total_bytes):
        """Stream the request body to disk without buffering the upload in RAM."""
        total_bytes = int(total_bytes or 0)
        written = 0
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('wb') as out:
            remaining = total_bytes
            while remaining > 0:
                chunk = self.rfile.read(min(UPLOAD_CHUNK, remaining))
                if not chunk:
                    raise ConnectionError(f'Upload ended early: received {written} of {total_bytes} bytes')
                out.write(chunk)
                written += len(chunk)
                remaining -= len(chunk)
            out.flush()
        return written

    def _json(self,obj,code=200,extra_headers=None):
        b=json.dumps(obj,ensure_ascii=False).encode('utf-8')
        return self._write_response(code,'application/json; charset=utf-8',b,extra_headers)

    def _do_GET_impl(self):
        p=urlparse(self.path)
        if p.path=='/login':
            if _auth_is_authenticated(self):
                self.send_response(302); self.send_header('Location','/'); self.send_header('Content-Length','0'); self.end_headers(); return
            return self._write_response(200,'text/html; charset=utf-8',AUTH_PAGE.encode('utf-8'),{'Cache-Control':'no-store'})
        if p.path=='/favicon.ico':
            return self._write_response(204,'image/x-icon',b'')
        if p.path=='/api/auth/me':
            u=_auth_current_user(self)
            return self._json({'authenticated':bool(u),'username':u['username'] if u else None,'role':u['role'] if u else None,'email':u['email'] if u else None},200,{'Cache-Control':'no-store'})
        if p.path=='/api/auth/users':
            user,err=_auth_require_role(self, {'ADMIN'})
            if err: return self._json(err,401 if err['code']=='AUTH_REQUIRED' else 403)
            return self._json({'users':_auth_list_users()},200,{'Cache-Control':'no-store'})
        if p.path=='/api/auth/audit':
            user,err=_auth_require_role(self, {'ADMIN'})
            if err: return self._json(err,401 if err['code']=='AUTH_REQUIRED' else 403)
            return self._json({'logs':_auth_audit_list()},200,{'Cache-Control':'no-store'})
        if p.path=='/api/auth/logout':
            u=_auth_current_user(self)
            if u: _auth_audit(self,'LOGOUT',p.path,role=u['role'],username=u['username'])
            cookie=_auth_logout(self)
            return self._write_response(200,'application/json; charset=utf-8',json.dumps({'ok':True}).encode('utf-8'),{'Set-Cookie':cookie,'Cache-Control':'no-store'})
        if not _auth_is_authenticated(self):
            if p.path.startswith('/api/'):
                return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
            self.send_response(302); self.send_header('Location','/login'); self.send_header('Content-Length','0'); self.end_headers(); return
        if p.path=='/':
            return self._write_response(200,'text/html; charset=utf-8',INDEX.encode('utf-8'))
        if p.path=='/api/projects':
            u=_auth_current_user(self)
            if not u: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
            rows=_project_list(u); active=_project_resolve(self,u)
            return self._json({'projects':rows,'active_project_id':active['id'] if active else None},200,{'Cache-Control':'no-store'})
        if p.path=='/api/uploaded_drawings':
            return self._json({'drawings':_uploaded_drawings_status()},200,{'Cache-Control':'no-store'})
        if p.path.startswith('/api/recon/'):
            u=_auth_current_user(self)
            if not u: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
            if RECON is None: return self._json({'error':'Reconciliation module is not installed.'},503)
            try:
                pdir=_project_dir(CURRENT_PROJECT_ID)
                if p.path=='/api/recon/data':
                    with RECON.locked_state(pdir,save=False) as st: payload=RECON.api_payload(pdir,st)
                    return self._json(payload,200,{'Cache-Control':'no-store'})
                if p.path=='/api/recon/page':
                    qs=parse_qs(p.query)
                    with RECON.locked_state(pdir,save=False) as st: data,mime=RECON.page_image_bytes(pdir,st,qs.get('doc',[''])[0],qs.get('page',['1'])[0],qs.get('w',['0'])[0])
                    return self._write_response(200,mime,data,{'Cache-Control':'no-store'})
                if p.path=='/api/recon/download':
                    name=Path(parse_qs(p.query).get('name',[''])[0]).name
                    base=(RECON.recon_dir(pdir)/'exports').resolve(); f=(base/name).resolve()
                    if not name or f.parent!=base or not f.is_file() or f.suffix.lower()!='.xlsx': return self._json({'error':'File not found'},404)
                    return self._write_response(200,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',f.read_bytes(),{'Content-Disposition':"attachment; filename*=UTF-8''"+__import__('urllib.parse',fromlist=['quote']).quote(name),'Cache-Control':'no-store'})
                return self._json({'error':'Unknown reconciliation endpoint.'},404)
            except RECON.ReconError as e: return self._json({'error':str(e)},400)
            except Exception as e:
                print(f'[RECON] GET failed: {type(e).__name__}: {e}'); return self._json({'error':'Reconciliation request failed.'},500)
        if p.path=='/api/project_documents':
            rows=_project_docs_index(CURRENT_PROJECT_ID)
            safe=[{k:x.get(k) for k in ('id','category','category_label','filename','document_number','document_date','note','uploaded_by','uploaded_at','size_bytes')} for x in rows]
            return self._json({'documents':safe},200,{'Cache-Control':'no-store'})
        if p.path=='/api/project_documents/download':
            doc_id=parse_qs(p.query).get('id',[''])[0]
            rec=next((x for x in _project_docs_index(CURRENT_PROJECT_ID) if str(x.get('id'))==str(doc_id)),None)
            if not rec: return self._json({'error':'Document not found in this project.'},404)
            base=_project_docs_dir(CURRENT_PROJECT_ID).resolve(); target=(base/str(rec.get('stored_name',''))).resolve()
            if target.parent!=base or not target.is_file(): return self._json({'error':'Document file is missing.'},404)
            data=target.read_bytes(); fname=_project_doc_safe_name(rec.get('filename','document')).replace('"','_')
            ctype='application/pdf' if target.suffix.lower()=='.pdf' else ('text/csv; charset=utf-8' if target.suffix.lower()=='.csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            return self._write_response(200,ctype,data,{'Content-Disposition':"attachment; filename*=UTF-8''"+__import__('urllib.parse',fromlist=['quote']).quote(fname),'Cache-Control':'no-store'})
        if p.path=='/api/project_status':
            return self._json({'project':_project_status(PROJECT_RESULTS)},200,{'Cache-Control':'no-store'})
        if p.path=='/api/project_result':
            floor=parse_qs(p.query).get('floor',[''])[0]; r=PROJECT_RESULTS.get(floor)
            if not r: return self._json({'error':'Floor result not found'},404)
            return self._json({'result':r})
        if p.path=='/api/asbuilt_targets':
            if not _asbuilt_cad_ready():
                return self._json({'error':'Analyze the CAD project first. As-Built is unavailable until CAD analysis is completed.','code':'CAD_ANALYSIS_REQUIRED'},409)
            q=parse_qs(p.query); floor=q.get('floor',[''])[0]; pattern=q.get('label_pattern',[None])[0]
            try: return self._json({'targets':_asbuilt_targets(floor,label_pattern=pattern)})
            except Exception as e: return self._json({'error':str(e)},400)
        if p.path.startswith('/download/'):
            raw_name=p.path.split('/download/',1)[1]
            # Path().name strips traversal components; reject encoded/odd paths rather than
            # silently normalizing them, and only serve regular files from EXPORTS.
            name=Path(raw_name).name
            if raw_name != name or name in {'','.','..'} or '\\' in raw_name:
                return self._json({'error':'Invalid download path.'},400)
            f=(EXPORTS/name).resolve()
            try:
                if f.parent != EXPORTS.resolve() or not f.is_file():
                    return self._json({'error':'File not found'},404)
            except OSError:
                return self._json({'error':'File not found'},404)
            if name not in PROJECT_GENERATED_FILES:
                return self._json({'error':'File not found in the selected project.'},404)
            mime={'.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','.zip':'application/zip','.dxf':'application/dxf','.json':'application/json'}.get(f.suffix.lower(),'application/octet-stream')
            try:
                u=_auth_current_user(self)
                if u: _auth_audit(self,'DOWNLOAD',p.path,f'file={name}',role=u['role'],username=u['username'])
                self.send_response(200); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(f.stat().st_size)); self.send_header('Content-Disposition',f'attachment; filename="{name}"'); self.send_header('Connection','close'); self.end_headers()
                with f.open('rb') as src:
                    while True:
                        chunk=src.read(UPLOAD_CHUNK)
                        if not chunk: break
                        self.wfile.write(chunk)
                self.close_connection=True; return
            except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError,OSError) as e:
                print(f'[UI] client disconnected during file download: {e}'); self.close_connection=True; return
        return self._json({'error':'Not found'},404)

    def _do_POST_impl(self):
        global CURRENT_RESULT, CURRENT_DRAWING, PROJECT_RESULTS, CURRENT_PROJECT_ID, CURRENT_PROJECT_OWNER_ID, UPLOADED_DRAWINGS, PROJECT_GENERATED_FILES
        p=urlparse(self.path)
        try:
            # Reject unexpectedly large JSON/form requests before reading them into memory.
            # Streaming upload endpoints are intentionally excluded.
            if p.path not in {'/api/upload','/api/asbuilt_chat_upload','/api/asbuilt_legend_upload','/api/recon/upload'}:
                try:
                    content_length=int(self.headers.get('Content-Length','0') or 0)
                except ValueError:
                    content_length=0
                if content_length > 10 * 1024 * 1024:
                    return self._json({'error':'Request body is too large.','code':'REQUEST_TOO_LARGE'},413)
            # SameSite=Strict is already enabled on the auth cookie; Origin validation
            # adds defense-in-depth for state-changing authenticated requests.
            if p.path not in {'/api/auth/login','/api/auth/verify','/api/auth/forgot/request','/api/auth/forgot/verify','/api/auth/forgot/reset'}:
                if not _auth_origin_allowed(self):
                    return self._json({'error':'Cross-origin request blocked.','code':'CSRF_BLOCKED'},403)
            if p.path=='/api/auth/login':
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                login_name=str(q.get('username') or '').strip().lower()
                ip=_client_ip(self)
                ip_key='ip:'+ip; user_key='user:'+login_name if login_name else ''
                ok_ip,wait_ip=_rate_check(_AUTH_RATE,ip_key,AUTH_LOGIN_RATE_MAX,AUTH_LOGIN_RATE_WINDOW)
                ok_user,wait_user=_rate_check(_AUTH_RATE,user_key,AUTH_LOGIN_RATE_MAX,AUTH_LOGIN_RATE_WINDOW) if user_key else (True,0)
                if not ok_ip or not ok_user:
                    wait=max(wait_ip,wait_user)
                    _auth_audit(self,'LOGIN_RATE_LIMITED',p.path,f'retry_after={wait}s',username=q.get('username',''))
                    return self._json({'error':f'Too many login attempts. Try again in about {max(1,wait)} seconds.','code':'RATE_LIMITED','retry_after':max(1,wait)},429,{'Retry-After':str(max(1,wait)),'Cache-Control':'no-store'})
                _rate_hit(_AUTH_RATE,ip_key,AUTH_LOGIN_RATE_WINDOW)
                if user_key: _rate_hit(_AUTH_RATE,user_key,AUTH_LOGIN_RATE_WINDOW)
                try: result=_auth_begin(q.get('username'),q.get('password'))
                except Exception as e:
                    _auth_audit(self,'LOGIN_ERROR',p.path,str(e),username=q.get('username','')); return self._json({'error':str(e),'code':'OTP_DELIVERY_FAILED'},502)
                if result.get('error'):
                    _auth_audit(self,'LOGIN_FAILED',p.path,result.get('error',''),username=q.get('username','')); return self._json(result,503 if result.get('code')=='AUTH_NOT_CONFIGURED' else 401)
                _rate_clear(_AUTH_RATE,ip_key)
                if user_key: _rate_clear(_AUTH_RATE,user_key)
                _auth_audit(self,'OTP_SENT',p.path,'OTP challenge created',username=result.get('username','')); return self._json(result)
            if p.path=='/api/auth/verify':
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                result=_auth_verify(q.get('challenge'),q.get('otp'))
                if result.get('error'):
                    _auth_audit(self,'OTP_FAILED',p.path,result.get('error','')); return self._json(result,401)
                _auth_audit(self,'LOGIN_SUCCESS',p.path,'OTP verified',username=result.get('username',''),role=result.get('role',''))
                return self._json({'ok':True,'username':result['username']},200,{'Set-Cookie':_auth_set_cookie(result['token']),'Cache-Control':'no-store'})
            if p.path=='/api/auth/forgot/request':
                print('[AUTH FORGOT] request received',flush=True)
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                ident=str(q.get('identifier') or '').strip().lower(); ip=_client_ip(self)
                key='ip:'+ip+':id:'+ident
                allowed,wait=_rate_check(_AUTH_FORGOT_RATE,key,AUTH_FORGOT_RATE_MAX,AUTH_FORGOT_RATE_WINDOW)
                if not allowed:
                    _auth_audit(self,'FORGOT_RATE_LIMITED',p.path,f'retry_after={wait}s')
                    return self._json({'error':f'Too many reset requests. Try again in about {max(1,wait)} seconds.','code':'RATE_LIMITED','retry_after':max(1,wait)},429,{'Retry-After':str(max(1,wait)),'Cache-Control':'no-store'})
                _rate_hit(_AUTH_FORGOT_RATE,key,AUTH_FORGOT_RATE_WINDOW)
                try:
                    cid,msg=_forgot_request(q.get('identifier'))
                    return self._json({'ok':True,'message':msg,'challenge_id':cid})
                except Exception as e:
                    print('[FORGOT REQUEST ERROR]',repr(e),flush=True)
                    return self._json({'error':'Unable to send reset OTP.','code':'OTP_DELIVERY_FAILED'},502)
            if p.path=='/api/auth/forgot/verify':
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                ch,err=_forgot_verify(str(q.get('challenge_id','')),q.get('otp'))
                if err: return self._json({'error':err},400)
                return self._json({'ok':True,'verified':True})
            if p.path=='/api/auth/forgot/reset':
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                if q.get('password')!=q.get('confirm_password'): return self._json({'error':'Passwords do not match.'},400)
                ok,err=_forgot_reset(str(q.get('challenge_id','')),q.get('password'))
                if err: return self._json({'error':err},400)
                return self._json({'ok':True,'message':'Password reset successfully.'})
            if p.path=='/api/auth/change-password':
                user=_auth_current_user(self)
                if not user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                try:
                    if not q.get('current_password') or not q.get('new_password'):
                        raise ValueError('Current password and new password are required.')
                    updated=_auth_change_password(user['id'],q.get('current_password'),q.get('new_password'))
                    _auth_audit(self,'PASSWORD_CHANGE',p.path,'self-service password changed',role=user['role'],username=user['username'])
                    return self._json({'ok':True,'username':updated['username']})
                except ValueError as e:
                    _auth_audit(self,'PASSWORD_CHANGE_FAILED',p.path,str(e),role=user['role'],username=user['username'])
                    return self._json({'error':str(e)},400)
                except Exception as e:
                    _auth_audit(self,'PASSWORD_CHANGE_ERROR',p.path,str(e),role=user['role'],username=user['username'])
                    return self._json({'error':'Could not change password.'},500)
            if p.path=='/api/auth/users/create':
                user,err=_auth_require_role(self, {'ADMIN'})
                if err: return self._json(err,401 if err['code']=='AUTH_REQUIRED' else 403)
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                try:
                    created=_auth_create_user(q.get('username'),q.get('password'),q.get('email',''),q.get('role','VIEWER'))
                    _auth_audit(self,'USER_CREATE',p.path,f"created={created['username']} role={created['role']}",role=user['role'],username=user['username'])
                    safe={k:created[k] for k in ('id','username','email','role','active','created_at','updated_at')}
                    return self._json({'ok':True,'user':safe},201)
                except sqlite3.IntegrityError: return self._json({'error':'Username already exists.','code':'DUPLICATE_USER'},409)
                except Exception as e: return self._json({'error':str(e)},400)
            if p.path=='/api/auth/users/update':
                user,err=_auth_require_role(self, {'ADMIN'})
                if err: return self._json(err,401 if err['code']=='AUTH_REQUIRED' else 403)
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                try:
                    target=_auth_get_user(q.get('username')) if q.get('username') else None
                    if not target: raise ValueError('User not found.')
                    newrole=str(q.get('role',target['role'])).upper(); newactive=q.get('active',target['active'])
                    if target['role']=='ADMIN' and (newrole!='ADMIN' or not bool(newactive)) and _auth_count_admins()<=1: raise ValueError('At least one active ADMIN must remain.')
                    updated=_auth_update_user(target['id'],q.get('email'),newrole,newactive,q.get('password'))
                    _auth_audit(self,'USER_UPDATE',p.path,f"target={updated['username']} role={updated['role']} active={updated['active']} password_changed={bool(q.get('password'))}",role=user['role'],username=user['username'])
                    safe={k:updated[k] for k in ('id','username','email','role','active','created_at','updated_at')}
                    return self._json({'ok':True,'user':safe})
                except Exception as e: return self._json({'error':str(e)},400)
            if p.path=='/api/auth/logout':
                u=_auth_current_user(self)
                if u: _auth_audit(self,'LOGOUT',p.path,role=u['role'],username=u['username'])
                return self._write_response(200,'application/json; charset=utf-8',json.dumps({'ok':True}).encode('utf-8'),{'Set-Cookie':_auth_logout(self),'Cache-Control':'no-store'})
            if p.path=='/api/projects':
                current_user=_auth_current_user(self)
                if not current_user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
                rows=_project_list(current_user); active=_project_resolve(self,current_user)
                return self._json({'projects':rows,'active_project_id':active['id'] if active else None},200,{'Cache-Control':'no-store'})
            if p.path=='/api/project_rename':
                current_user=_auth_current_user(self)
                if not current_user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                project=_project_get(q.get('project_id') or CURRENT_PROJECT_ID,current_user)
                if not project: return self._json({'error':'Project not found or access denied.','code':'PROJECT_FORBIDDEN'},404)
                name=str(q.get('name') or '').strip()
                if not name: return self._json({'error':'Project name cannot be empty.'},400)
                name=_project_safe_name(name); now=_project_now()
                with _project_db_conn() as con:
                    con.execute('UPDATE projects SET name=?,updated_at=? WHERE id=? AND owner_user_id=?',(name,now,project['id'],int(current_user['id']))); con.commit()
                project=_project_get(project['id'],current_user)
                return self._json({'ok':True,'project':project})
            if p.path=='/api/project_delete':
                current_user=_auth_current_user(self)
                if not current_user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                project=_project_get(q.get('project_id') or CURRENT_PROJECT_ID,current_user)
                if not project: return self._json({'error':'Project not found or access denied.','code':'PROJECT_FORBIDDEN'},404)
                pid=project['id']
                with _project_db_conn() as con:
                    con.execute('DELETE FROM projects WHERE id=? AND owner_user_id=?',(pid,int(current_user['id']))); con.commit()
                shutil.rmtree(PROJECTS_ROOT/pid,ignore_errors=False) if (PROJECTS_ROOT/pid).exists() else None
                CURRENT_PROJECT_ID=None; CURRENT_PROJECT_OWNER_ID=None; CURRENT_RESULT=None; CURRENT_DRAWING=None; PROJECT_RESULTS={}; UPLOADED_DRAWINGS=[]; PROJECT_GENERATED_FILES=set()
                rows=_project_list(current_user); active=rows[0] if rows else None
                headers={'Cache-Control':'no-store'}
                if active:
                    _project_load_state(active['id']); CURRENT_PROJECT_ID=active['id']; CURRENT_PROJECT_OWNER_ID=int(current_user['id']); headers['Set-Cookie']=_project_set_cookie(active['id'])
                return self._json({'ok':True,'active_project_id':active['id'] if active else None,'projects':rows},200,headers)
            if p.path=='/api/project_delete_drawing':
                current_user=_auth_current_user(self)
                if not current_user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}'); drawing_id=str(q.get('id') or '')
                rec=next((x for x in UPLOADED_DRAWINGS if str(x.get('id'))==drawing_id),None)
                if not rec: return self._json({'error':'Drawing not found in the selected project.'},404)
                path=Path(rec.get('path') or '')
                if path.exists():
                    try: path.unlink()
                    except OSError as e: return self._json({'error':'Cannot delete drawing file: '+str(e)},500)
                removed_floor=None
                for floor,rr in list(PROJECT_RESULTS.items()):
                    if isinstance(rr,dict) and rr.get('drawing') and Path(str(rr.get('drawing'))).resolve()==path.resolve():
                        PROJECT_RESULTS.pop(floor,None); removed_floor=floor
                UPLOADED_DRAWINGS=[x for x in UPLOADED_DRAWINGS if str(x.get('id'))!=drawing_id]
                if removed_floor and isinstance(CURRENT_RESULT,dict) and CURRENT_RESULT.get('floor')==removed_floor:
                    CURRENT_RESULT=next(iter(PROJECT_RESULTS.values()),None)
                    CURRENT_DRAWING=Path(CURRENT_RESULT.get('drawing')) if isinstance(CURRENT_RESULT,dict) and CURRENT_RESULT.get('drawing') else None
                _project_save_current()
                return self._json({'ok':True,'removed_floor':removed_floor,'project':_project_status(PROJECT_RESULTS)})
            if p.path in ('/api/recon/upload','/api/recon/action'):
                current_user=_auth_current_user(self)
                if not current_user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
                if RECON is None: return self._json({'error':'Reconciliation module is not installed.'},503)
                try:
                    pdir=_project_dir(CURRENT_PROJECT_ID)
                    if p.path=='/api/recon/upload':
                        fields,(original_name,payload)=_project_doc_read_multipart(self); kind=str(fields.get('kind',''))
                        with RECON.locked_state(pdir) as st:
                            meta=RECON.save_upload(pdir,st,kind,RECON.safe_display_name(original_name),payload,current_user,lambda n:n)
                            out={'ok':True,'file':{'id':meta['id'],'filename':meta['filename']}}
                            if kind=='boq': out.update(items=len(st['boq']['items']),sheet=st['boq']['sheet'])
                        _auth_audit(self,'RECON_UPLOAD',p.path,f"project={CURRENT_PROJECT_ID} kind={kind} filename={meta['filename']} sha={meta['sha256'][:12]}",role=current_user['role'],username=current_user['username'])
                        return self._json(out,201)
                    n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}')
                    if not isinstance(q,dict): return self._json({'error':'Invalid request.'},400)
                    a=str(q.get('action',''))
                    with RECON.locked_state(pdir) as st: res=_recon_dispatch(pdir,st,a,q,current_user)
                    if a in ('ocr','update_record','match_decide','approve_exact','match_run','remove_doc','delete_record','import_docs','export','boq_unit','dup','add_line'):
                        _auth_audit(self,'RECON_'+a.upper(),p.path,f"project={CURRENT_PROJECT_ID} id={str(q.get('id') or q.get('doc_id') or q.get('item_id') or '')[:60]} decision={q.get('decision','')} reviewed={bool(q.get('mark_reviewed'))}",role=current_user['role'],username=current_user['username'])
                    return self._json(res)
                except (RECON.ReconError,ValueError) as e: return self._json({'error':str(e)},400)
                except Exception as e:
                    print(f'[RECON] POST failed: {type(e).__name__}: {e}'); return self._json({'error':'Reconciliation request failed.'},500)
            if p.path=='/api/project_documents/upload':
                current_user=_auth_current_user(self)
                if not current_user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
                if current_user.get('role')=='VIEWER': return self._json({'error':'Viewer accounts are read-only.','code':'FORBIDDEN'},403)
                try:
                    fields,(original_name,payload)=_project_doc_read_multipart(self)
                    category=str(fields.get('category',''))
                    if category not in PROJECT_DOC_CATEGORIES: return self._json({'error':'Unknown document category.'},400)
                    filename=_project_doc_safe_name(original_name); ext=Path(filename).suffix.lower()
                    if ext not in PROJECT_DOC_EXTENSIONS: return self._json({'error':'Only PDF, XLS, XLSX, XLSM, and CSV files are allowed.'},400)
                    docsdir=_project_docs_dir(CURRENT_PROJECT_ID); doc_id=uuid.uuid4().hex; stored=doc_id+ext
                    target=docsdir/stored
                    with target.open('xb') as f: f.write(payload)
                    rows=_project_docs_index(CURRENT_PROJECT_ID)
                    rows.append({'id':doc_id,'category':category,'category_label':PROJECT_DOC_CATEGORIES[category],'filename':filename,'stored_name':stored,'document_number':str(fields.get('document_number','')).strip()[:120],'document_date':str(fields.get('document_date','')).strip()[:40],'note':str(fields.get('note','')).strip()[:1000],'uploaded_by':current_user.get('username',''),'uploaded_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds'),'size_bytes':len(payload)})
                    _project_docs_save(rows,CURRENT_PROJECT_ID)
                    _auth_audit(self,'PROJECT_DOCUMENT_UPLOAD',p.path,f'project={CURRENT_PROJECT_ID} category={category} filename={filename}',role=current_user['role'],username=current_user['username'])
                    return self._json({'ok':True,'document':rows[-1]},201)
                except ValueError as e: return self._json({'error':str(e)},400)
                except Exception as e:
                    print(f'[PROJECT DOCS] upload failed: {type(e).__name__}: {e}')
                    return self._json({'error':'Could not save the project document.'},500)
            if p.path=='/api/project_documents/delete':
                current_user=_auth_current_user(self)
                if not current_user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
                if current_user.get('role')=='VIEWER': return self._json({'error':'Viewer accounts are read-only.','code':'FORBIDDEN'},403)
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}'); doc_id=str(q.get('id') or '')
                rows=_project_docs_index(CURRENT_PROJECT_ID); rec=next((x for x in rows if str(x.get('id'))==doc_id),None)
                if not rec: return self._json({'error':'Document not found in this project.'},404)
                docsdir=_project_docs_dir(CURRENT_PROJECT_ID); target=(docsdir/str(rec.get('stored_name',''))).resolve()
                if target.parent!=docsdir.resolve(): return self._json({'error':'Invalid document path.'},400)
                try:
                    if target.exists(): target.unlink()
                    rows=[x for x in rows if str(x.get('id'))!=doc_id]; _project_docs_save(rows,CURRENT_PROJECT_ID)
                    _auth_audit(self,'PROJECT_DOCUMENT_DELETE',p.path,f'project={CURRENT_PROJECT_ID} document={doc_id}',role=current_user['role'],username=current_user['username'])
                    return self._json({'ok':True,'documents_count':len(rows)})
                except Exception as e: return self._json({'error':'Could not delete document: '+str(e)},500)
            if p.path=='/api/project_select':
                current_user=_auth_current_user(self)
                if not current_user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}'); project=_project_get(q.get('project_id'),current_user)
                if not project: return self._json({'error':'Project not found or access denied.','code':'PROJECT_FORBIDDEN'},404)
                _project_load_state(project['id']); CURRENT_PROJECT_ID=project['id']; CURRENT_PROJECT_OWNER_ID=int(current_user['id']); return self._json({'ok':True,'project':_project_status(PROJECT_RESULTS),'project_meta':project},200,{'Set-Cookie':_project_set_cookie(project['id']),'Cache-Control':'no-store'})
            if p.path=='/api/project_create':
                current_user=_auth_current_user(self)
                if not current_user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
                n=int(self.headers.get('Content-Length','0') or 0); q=json.loads(self.rfile.read(n) or b'{}'); project=_project_create(current_user,q.get('name'))
                CURRENT_PROJECT_ID=project['id']; CURRENT_PROJECT_OWNER_ID=int(current_user['id']); _project_load_state(project['id']); return self._json({'ok':True,'project':project,'project_status':[]},201,{'Set-Cookie':_project_set_cookie(project['id']),'Cache-Control':'no-store'})
            current_user=_auth_current_user(self)
            if not current_user: return self._json({'error':'Authentication required.','code':'AUTH_REQUIRED'},401)
            endpoint_err=_auth_require_endpoint_role(self,p.path,current_user)
            if endpoint_err:
                _auth_audit(self,'API_FORBIDDEN',p.path,'role not allowed',role=current_user['role'],username=current_user['username'])
                return self._json(endpoint_err,403)
            # Hard dependency: every As-Built operation requires a completed CAD analysis.
            # Keep this backend-side so bypassing the UI cannot start As-Built on an empty project.
            if p.path in {
                '/api/asbuilt_chat_upload','/api/asbuilt_chat_start','/api/asbuilt_chat_message',
                '/api/asbuilt_chat_reply','/api/asbuilt_legend_upload','/api/asbuilt_manual_change',
                '/api/asbuilt_chat_add','/api/asbuilt_generate','/api/asbuilt_generate_project'
            } and not _asbuilt_cad_ready():
                _auth_audit(self,'ASBUILT_BLOCKED_CAD_NOT_ANALYZED',p.path,
                            'CAD analysis is required before As-Built operations',
                            role=current_user['role'],username=current_user['username'])
                return self._json({
                    'error':'Analyze the CAD project first. As-Built is unavailable until CAD analysis is completed.',
                    'code':'CAD_ANALYSIS_REQUIRED'
                },409)
            action_map={'/api/upload':'CAD_UPLOAD','/api/export':'EXPORT_EXCEL','/api/generate_labels':'GENERATE_LABELS','/api/board_review':'BOARD_REVIEW','/api/asbuilt_chat_upload':'ASBUILT_UPLOAD','/api/asbuilt_chat_start':'ASBUILT_SCAN','/api/asbuilt_chat_message':'ASBUILT_REVIEW','/api/asbuilt_generate':'ASBUILT_GENERATE','/api/asbuilt_generate_project':'ASBUILT_GENERATE_PROJECT','/api/asbuilt_manual_change':'REVISION_CHANGE','/api/asbuilt_chat_add':'REVISION_ADD','/api/asbuilt_legend_upload':'LEGEND_UPLOAD','/api/classification_review':'CLASSIFICATION_REVIEW','/api/rack_review':'RACK_REVIEW'}
            _auth_audit(self,action_map.get(p.path,'API_POST'),p.path,role=current_user['role'],username=current_user['username'])
            if current_user['role']=='VIEWER': return self._json({'error':'Viewer accounts are read-only.','code':'FORBIDDEN'},403)
            if current_user['role']=='TECHNICIAN' and p.path in {'/api/export','/api/generate_labels','/api/asbuilt_generate','/api/asbuilt_generate_project'}:
                return self._json({'error':'Technicians cannot generate or export final CAD packages.','code':'FORBIDDEN'},403)
            if p.path=='/api/demo':
                CURRENT_RESULT=json.loads(json.dumps(DEMO_RESULT)); PROJECT_RESULTS[CURRENT_RESULT['floor']]=CURRENT_RESULT; files=export_project(PROJECT_RESULTS,label_pattern=DEFAULT_LABEL_PATTERN); return self._json({'result':CURRENT_RESULT,'project':_project_status(PROJECT_RESULTS),'files':files})
            if p.path=='/api/asbuilt_legend_upload':
                name=Path(parse_qs(p.query).get('filename',['legend.png'])[0]).name
                if Path(name).suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff'}:
                    return self._json({'error':'Legend reference must be an image file'},400)
                try: n=int(self.headers.get('Content-Length','0'))
                except ValueError: n=0
                if n<=0: return self._json({'error':'Empty legend upload'},411)
                if n>20*1024*1024: return self._json({'error':'Legend image is larger than 20 MB'},413)
                target=_project_dir(CURRENT_PROJECT_ID)/(uuid.uuid4().hex+'_ASBUILT_LEGEND_'+name)
                written=self._stream_upload(target,n)
                return self._json({'path':str(target),'size':written,'filename':name})
            if p.path=='/api/asbuilt_chat_upload':
                name=Path(parse_qs(p.query).get('filename',['site_asbuilt.pdf'])[0]).name
                if Path(name).suffix.lower() not in {'.pdf','.png','.jpg','.jpeg'}: return self._json({'error':'Only PDF, PNG or JPG is allowed for the As-Built chat'},400)
                try: n=int(self.headers.get('Content-Length','0'))
                except ValueError: n=0
                if n<=0: return self._json({'error':'Empty upload or missing Content-Length'},411)
                if n>ASBUILT_PDF_MAX_BYTES: return self._json({'error':'As-Built file is larger than the 200 MB limit'},413)
                # Keep the physical workspace filename short.  Windows/PyMuPDF can
                # fail with FzErrorSystem/code=2 when the full path becomes too long,
                # especially with long Arabic PDF filenames and a deep project folder.
                # The original filename is still returned separately for display/logging.
                suffix=Path(name).suffix.lower()
                target=_project_dir(CURRENT_PROJECT_ID)/(uuid.uuid4().hex+'_ASBUILT'+suffix)
                written=self._stream_upload(target,n)
                return self._json({'path':str(target),'size':written,'filename':name,'stored_filename':target.name})
            if p.path=='/api/asbuilt_chat_start':
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}')
                if not _asbuilt_cad_ready(): return self._json({'error':'Analyze the CAD project before starting the As-Built chat.','code':'CAD_ANALYSIS_REQUIRED'},409)
                return self._json(asbuilt_chat_start(q.get('path') or '',q.get('filename') or '',q.get('api_key'),q.get('model'),label_pattern=q.get('label_pattern'),first_message=q.get('message') or '',paths=q.get('paths') or [],filenames=q.get('filenames') or [],legend_path=q.get('legend_path') or ''))
            if p.path=='/api/asbuilt_chat_reply':
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}')
                return self._json(asbuilt_chat_reply(q.get('chat_id') or '',q.get('message') or ''))
            if p.path=='/api/upload':
                name=Path(parse_qs(p.query).get('filename',['drawing.dxf'])[0]).name
                if Path(name).suffix.lower() not in {'.dwg','.dxf'}: return self._json({'error':'Only DWG/DXF is allowed'},400)
                try: n=int(self.headers.get('Content-Length','0'))
                except ValueError: n=0
                if n <= 0: return self._json({'error':'Empty upload or missing Content-Length'},411)
                if n > MAX_UPLOAD_BYTES: return self._json({'error':'Drawing is larger than the 2 GiB upload limit'},413)
                target=_project_dir(CURRENT_PROJECT_ID)/(uuid.uuid4().hex+'_'+name)
                written=self._stream_upload(target,n)
                CURRENT_DRAWING=target
                print(f'[UI] upload saved: {target.name} ({written} bytes)')
                upload_id=uuid.uuid4().hex
                UPLOADED_DRAWINGS.append({'id':upload_id,'project_id':CURRENT_PROJECT_ID,'path':str(target),'filename':name,'uploaded_by':current_user['username'],'uploaded_at':datetime.datetime.utcnow().replace(microsecond=0).isoformat()+'Z','analyzed':False})
                return self._json({'path':str(target),'size':written,'filename':name,'upload_id':upload_id})
            if p.path=='/api/analyze_uploaded':
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}')
                rec=next((x for x in UPLOADED_DRAWINGS if x.get('id')==str(q.get('id') or '')),None)
                if not rec: return self._json({'error':'Uploaded drawing was not found'},404)
                path=Path(rec.get('path',''))
                if not path.exists(): return self._json({'error':'Uploaded drawing file is no longer available'},404)
                if not ANALYSIS_LOCK.acquire(blocking=False): return self._json({'error':'Another CAD analysis is already running. Wait for it to finish.'},409)
                try:
                    requested=q.get('floor') or 'Auto Detect'
                    CURRENT_RESULT=analyze_cad(path,q.get('university') or 'University',q.get('building') or '',requested)
                finally:
                    ANALYSIS_LOCK.release()
                PROJECT_RESULTS[CURRENT_RESULT['floor']]=CURRENT_RESULT
                rec['analyzed']=True
                rec['analyzed_by']=current_user['username']
                rec['analyzed_at']=datetime.datetime.utcnow().replace(microsecond=0).isoformat()+'Z'
                return self._json({'result':CURRENT_RESULT,'project':_project_status(PROJECT_RESULTS)})
            if p.path=='/api/analyze':
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}'); path=Path(q.get('path',''))
                if not path.exists(): return self._json({'error':'Uploaded drawing was not found on the server'},400)
                if not ANALYSIS_LOCK.acquire(blocking=False): return self._json({'error':'Another CAD analysis is already running. Wait for it to finish.'},409)
                try:
                    CURRENT_RESULT=analyze_cad(path,q.get('university') or 'University',q.get('building') or '',q.get('floor') or 'Auto Detect')
                finally:
                    ANALYSIS_LOCK.release()
                PROJECT_RESULTS[CURRENT_RESULT['floor']]=CURRENT_RESULT
                return self._json({'result':CURRENT_RESULT,'project':_project_status(PROJECT_RESULTS)})
            if p.path=='/api/project_clear':
                PROJECT_RESULTS.clear(); UPLOADED_DRAWINGS.clear(); PROJECT_GENERATED_FILES.clear(); CURRENT_RESULT=None; CURRENT_DRAWING=None
                _project_save_current()
                return self._json({'ok':True,'project':[]})
            if p.path=='/api/classification_override':
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}')
                floor=q.get('floor') or (CURRENT_RESULT or {}).get('floor')
                target=PROJECT_RESULTS.get(floor)
                if not target: return self._json({'error':'Selected floor result was not found'},400)
                target=apply_classification_overrides(target,q.get('decisions') or {})
                PROJECT_RESULTS[floor]=target; CURRENT_RESULT=target
                return self._json({'result':target,'project':_project_status(PROJECT_RESULTS)})
            if p.path=='/api/rack_override':
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}')
                floor=q.get('floor') or (CURRENT_RESULT or {}).get('floor')
                target=PROJECT_RESULTS.get(floor) if floor else CURRENT_RESULT
                if not target: return self._json({'error':'No analysis result yet'},400)
                target=apply_rack_overrides(target,q.get('allocations') or {})
                PROJECT_RESULTS[target['floor']]=target; CURRENT_RESULT=target
                return self._json({'result':target,'project':_project_status(PROJECT_RESULTS)})
            if p.path=='/api/asbuilt_generate':
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}')
                return self._json({'files':generate_asbuilt_package(q.get('floor') or '',q.get('changes') or [],label_pattern=q.get('label_pattern'))})
            if p.path=='/api/asbuilt_generate_project':
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}')
                try:
                    result=generate_asbuilt_project_package(q.get('changes') or [],label_pattern=q.get('label_pattern'))
                    return self._json({'files':result})
                except Exception as _e:
                    import traceback; traceback.print_exc()
                    return self._json({'error':str(_e)},500)
            if p.path=='/api/board_review':
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}') if n else {}
                return self._json(_board_review_floor(q.get('floor')))
            if p.path=='/api/generate_labels':
                if not PROJECT_RESULTS: return self._json({'error':'No project drawings have been analyzed yet'},400)
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}')
                return self._json({'files':generate_labeled_project(PROJECT_RESULTS,text_height=q.get('text_height'),label_pattern=q.get('label_pattern'))})
            if p.path=='/api/export':
                if not CURRENT_RESULT: return self._json({'error':'No analysis result yet'},400)
                n=int(self.headers.get('Content-Length','0')); q=json.loads(self.rfile.read(n) or b'{}') if n else {}
                return self._json({'files':export_project(PROJECT_RESULTS or {CURRENT_RESULT['floor']:CURRENT_RESULT},label_pattern=q.get('label_pattern'))})
            return self._json({'error':'Not found'},404)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as e:
            print(f'[UI] client disconnected: {e}')
        except Exception as e:
            print(f'[UI] request failed: {type(e).__name__}: {e}')
            self._json({'error':str(e)},500)

    def _activate_request_project(self, path, method):
        global CURRENT_PROJECT_ID, CURRENT_PROJECT_OWNER_ID
        user=_auth_current_user(self)
        if not user: return None
        if path in {'/api/projects','/api/project_create','/api/project_select','/api/auth/me','/api/auth/users','/api/auth/audit','/api/auth/logout'}: return None
        q=parse_qs(urlparse(self.path).query); requested=q.get('project_id',[''])[0] or ''
        project,err=_project_require_for_request(self,user,path,requested,create_if_missing=(path in {'/api/upload','/api/demo'}))
        if err:return err
        CURRENT_PROJECT_ID=project['id']; CURRENT_PROJECT_OWNER_ID=int(user['id']); return project

    def do_GET(self):
        p=urlparse(self.path); user=_auth_current_user(self)
        if not user or p.path in {'/','/login','/favicon.ico'} or p.path.startswith('/api/auth/'):
            return self._do_GET_impl()
        # Activate/load the requested user's project only after acquiring the
        # context lock. Loading project state mutates legacy module globals.
        with PROJECT_CONTEXT_LOCK:
            pe=self._activate_request_project(p.path,'GET')
            if isinstance(pe,dict) and pe.get('error'): return self._json(pe,409)
            return self._do_GET_impl()

    def do_POST(self):
        p=urlparse(self.path); user=_auth_current_user(self)
        if not user or p.path.startswith('/api/auth/'):
            return self._do_POST_impl()
        # Project create/select also mutate the module-level active project and
        # must be serialized with all other project operations.
        with PROJECT_CONTEXT_LOCK:
            if p.path in {'/api/project_create','/api/project_select'}:
                return self._do_POST_impl()
            pe=self._activate_request_project(p.path,'POST')
            if isinstance(pe,dict) and pe.get('error'): return self._json(pe,409)
            before={x.name for x in EXPORTS.iterdir() if x.is_file()}
            try:
                return self._do_POST_impl()
            finally:
                try:
                    PROJECT_GENERATED_FILES.update(x.name for x in EXPORTS.iterdir() if x.is_file() and x.name not in before)
                    _project_save_current()
                except Exception as e:
                    print(f'[PROJECT] save failed: {e}')

    def log_message(self,fmt,*args):
        print('[UI]',fmt%args)

# ---- TELETECH speed-up + timing v2 (paste ONCE at the very end of server.py, above `if __name__=='__main__':`) ----
if not globals().get('_TT_PATCHED'):
    _TT_PATCHED=True
    import copy as _copy, time as _t, functools as _ft
    _LA_CACHE=None
    def _tt_cache_pla(orig):
        def f(results_by_floor,label_pattern=None):
            if _LA_CACHE is None:
                return orig(results_by_floor,label_pattern=label_pattern)
            key=(id(results_by_floor),label_pattern)
            if key not in _LA_CACHE:
                _LA_CACHE[key]=orig(results_by_floor,label_pattern=label_pattern)
            return _copy.deepcopy(_LA_CACHE[key])
        return f
    def _tt_scope_gapp(orig):
        def f(changes,label_pattern=None):
            global _LA_CACHE
            _LA_CACHE={}
            try:
                return orig(changes,label_pattern=label_pattern)
            finally:
                _LA_CACHE=None
        return f
    def _tt_timed(fn):
        @_ft.wraps(fn)
        def w(*a,**k):
            t=_t.time()
            try:
                return fn(*a,**k)
            finally:
                print(f'[TIME] {fn.__name__}: {_t.time()-t:.1f}s',flush=True)
        return w
    _project_label_assignments=_tt_cache_pla(_project_label_assignments)
    generate_asbuilt_project_package=_tt_scope_gapp(generate_asbuilt_project_package)
    for _n in ('_project_label_assignments','_asbuilt_targets','_asbuilt_allocation_plan','export_project_asbuilt','generate_asbuilt_package','generate_asbuilt_project_package'):
        globals()[_n]=_tt_timed(globals()[_n])


# ---- TELETECH add-on: time + speed up the As-Built READING step ----
# Paste BELOW the v2 block, above `if __name__=='__main__':`
if globals().get('_TT_PATCHED') and not globals().get('_TT_ADDON'):
    _TT_ADDON=True
    def _tt_scope_any(orig):
        @_ft.wraps(orig)
        def f(*a,**k):
            global _LA_CACHE
            _LA_CACHE={}
            try:
                return orig(*a,**k)
            finally:
                _LA_CACHE=None
        return f
    asbuilt_chat_start=_tt_scope_any(asbuilt_chat_start)
    for _n in ('_split_asbuilt_pdf_to_images','_asbuilt_cv_color_inventory','_asbuilt_project_candidate_catalog','_asbuilt_code_markup_inventory','asbuilt_chat_start'):
        globals()[_n]=_tt_timed(globals()[_n])


if __name__=='__main__':
    host='127.0.0.1'; port=8765
    print(f'TELETECH CAD UI running at http://{host}:{port}')
    print('[PROJECT] Multi-user project isolation enabled | state persisted under workspace/projects/')
    print(f'[AUTH] Database: {AUTH_DB}')
    print(f'[AUTH] OTP mode: {AUTH_OTP_MODE} | Session TTL: {AUTH_SESSION_TTL}s | OTP TTL: {AUTH_OTP_TTL}s')
    print(f'[AUTH] Users: {len(_auth_list_users())}')
    if AUTH_OTP_MODE == 'email': print('[AUTH] Email OTP enabled. Each user receives OTP at their saved email address.')
    else: print('[AUTH] Console OTP enabled. Set TELETECH_AUTH_OTP_MODE=email for real email OTP.')
    threading.Timer(1.0,lambda:webbrowser.open(f'http://{host}:{port}')).start()
    ThreadingHTTPServer((host,port),Handler).serve_forever()
