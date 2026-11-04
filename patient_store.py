"""Patient memory: the middleware that stands in for the hospital's own system (HIS / ABDM Health Information Provider).

Privacy rules this file enforces:
- Data lives only here, in data/oncosaathi.db on our server. Sarvam and WhatsApp never get the record; they get one
  short answer at a time from the tool endpoints in server.py.
- Phone numbers are looked up by a keyed hash (PATIENT_HASH_KEY); phone, name and ABHA number are stored encrypted
  (PATIENT_DATA_KEY, Fernet). Keys live in .env only.
- Nothing clinical is stored: no diagnosis, no report contents, no medicine names. Tasks are logistics
  (go to a hospital.json place, on a date, bring X).
- Every read and write is written to the audit table. Consent time is recorded; delete_patient() erases a person.
"""
import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time

from cryptography.fernet import Fernet
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
DB_PATH = os.environ.get("ONCO_DB") or os.path.join(HERE, "data", "oncosaathi.db")
_fernet = Fernet(os.environ["PATIENT_DATA_KEY"].encode())
_hash_key = os.environ["PATIENT_HASH_KEY"].encode()
_lock = threading.Lock()
SESSION_HOURS = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
  id TEXT PRIMARY KEY, phone_hash TEXT UNIQUE NOT NULL, phone_enc TEXT NOT NULL, name_enc TEXT, abha_enc TEXT,
  hospital TEXT, stage TEXT DEFAULT 'before_arrival', consent_at TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, unit_id TEXT, title TEXT NOT NULL, title_hi TEXT,
  due_date TEXT, due_time TEXT, status TEXT DEFAULT 'pending', source TEXT, seq INTEGER DEFAULT 0,
  created_at TEXT, done_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, patient_id TEXT NOT NULL, channel TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT, ts TEXT, text TEXT, channel TEXT, delivered INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reminders (task_id TEXT, kind TEXT, sent_at TEXT, channel TEXT, status TEXT, PRIMARY KEY (task_id, kind));
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, patient_id TEXT, action TEXT, detail TEXT);
"""


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


_conn = _db()


def _enc(s):
    return _fernet.encrypt(s.encode()).decode() if s else None


def _dec(s):
    return _fernet.decrypt(s.encode()).decode() if s else None


def normalize_phone(raw):
    """Indian mobile -> +91XXXXXXXXXX. Returns None if it isn't a 10-digit mobile."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return f"+91{d}" if len(d) == 10 and d[0] in "6789" else None


def _phone_hash(phone):
    return hmac.new(_hash_key, phone.encode(), hashlib.sha256).hexdigest()


def audit(actor, patient_id, action, detail=""):
    with _lock:
        _conn.execute("INSERT INTO audit (ts, actor, patient_id, action, detail) VALUES (?,?,?,?,?)",
                      (_now(), actor, patient_id, action, detail[:300]))
        _conn.commit()


# ---------------- patients ----------------
def _row_to_patient(r):
    if not r:
        return None
    return {"id": r["id"], "name": _dec(r["name_enc"]), "abha": _dec(r["abha_enc"]), "hospital": r["hospital"],
            "stage": r["stage"], "consent_at": r["consent_at"], "created_at": r["created_at"]}


def find_by_phone(phone, actor="system"):
    phone = normalize_phone(phone)
    if not phone:
        return None
    r = _conn.execute("SELECT * FROM patients WHERE phone_hash=?", (_phone_hash(phone),)).fetchone()
    p = _row_to_patient(r)
    if p:
        audit(actor, p["id"], "read", "lookup by phone")
    return p


def get(patient_id, actor="system"):
    p = _row_to_patient(_conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone())
    if p:
        audit(actor, patient_id, "read", "record")
    return p


def get_phone(patient_id, actor):
    """Decrypted phone, only for sending a reminder. Audited."""
    r = _conn.execute("SELECT phone_enc FROM patients WHERE id=?", (patient_id,)).fetchone()
    if r:
        audit(actor, patient_id, "read", "phone for reminder")
        return _dec(r["phone_enc"])
    return None


def create(phone, consent=True, actor="system"):
    phone = normalize_phone(phone)
    if not phone:
        raise ValueError("not a valid Indian mobile number")
    existing = find_by_phone(phone, actor)
    if existing:
        if consent and not existing["consent_at"]:
            update(existing["id"], actor, consent_at=_now())
        return existing
    pid = "P" + secrets.token_hex(6)
    with _lock:
        _conn.execute("INSERT INTO patients (id, phone_hash, phone_enc, created_at, updated_at, consent_at) VALUES (?,?,?,?,?,?)",
                      (pid, _phone_hash(phone), _enc(phone), _now(), _now(), _now() if consent else None))
        _conn.commit()
    audit(actor, pid, "create", "consent given" if consent else "no consent yet")
    return get(pid, actor)


def update(patient_id, actor, **fields):
    cols = {}
    for k, v in fields.items():
        if k == "name":
            cols["name_enc"] = _enc(v.strip()[:60]) if v else None
        elif k == "abha":
            cols["abha_enc"] = _enc(v)
        elif k in ("hospital", "stage", "consent_at"):
            cols[k] = v
    if not cols:
        return
    cols["updated_at"] = _now()
    with _lock:
        _conn.execute(f"UPDATE patients SET {', '.join(c + '=?' for c in cols)} WHERE id=?", (*cols.values(), patient_id))
        _conn.commit()
    audit(actor, patient_id, "update", ", ".join(k for k in fields))


def delete_patient(patient_id, actor):
    """Right to erasure (DPDP): removes the person and everything linked to them. The audit keeps only the fact of deletion."""
    with _lock:
        for t in ("tasks", "sessions", "outbox"):
            _conn.execute(f"DELETE FROM {t} WHERE patient_id=?", (patient_id,))
        _conn.execute("DELETE FROM patients WHERE id=?", (patient_id,))
        _conn.execute("DELETE FROM audit WHERE patient_id=? AND action != 'erase'", (patient_id,))
        _conn.commit()
    audit(actor, patient_id, "erase", "all personal data deleted on request")


# ---------------- sessions (opaque tokens; the only thing a channel ever holds) ----------------
def new_session(patient_id, channel):
    tok = secrets.token_urlsafe(24)
    with _lock:
        _conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", (tok, patient_id, channel, time.time() + SESSION_HOURS * 3600))
        _conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        _conn.commit()
    return tok


def patient_for_session(token):
    if not token:
        return None
    r = _conn.execute("SELECT patient_id, expires FROM sessions WHERE token=?", (token,)).fetchone()
    if not r or r["expires"] < time.time():
        return None
    return r["patient_id"]


# ---------------- tasks (the journey's to-do list) ----------------
def add_task(patient_id, title, title_hi=None, unit_id=None, due_date=None, due_time=None, source="staff", actor="system"):
    seq = (_conn.execute("SELECT COALESCE(MAX(seq),0) FROM tasks WHERE patient_id=?", (patient_id,)).fetchone()[0] or 0) + 1
    tid = "T" + secrets.token_hex(5)
    with _lock:
        _conn.execute("INSERT INTO tasks (id, patient_id, unit_id, title, title_hi, due_date, due_time, source, seq, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", (tid, patient_id, unit_id, title, title_hi, due_date, due_time, source, seq, _now()))
        _conn.commit()
    audit(actor, patient_id, "add_task", f"{title} @ {unit_id or '-'} {due_date or ''} {due_time or ''}")
    return tid


def delete_tasks(patient_id, source, actor="system"):
    """Remove all of a person's tasks from one source (e.g. the demo seed)."""
    with _lock:
        n = _conn.execute("DELETE FROM tasks WHERE patient_id=? AND source=?", (patient_id, source)).rowcount
        _conn.commit()
    audit(actor, patient_id, "delete_tasks", f"{n} from {source}")
    return n


def tasks(patient_id, status=None, actor="system"):
    q = "SELECT * FROM tasks WHERE patient_id=?" + (" AND status=?" if status else "") + \
        " ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date, due_time, seq"
    rows = _conn.execute(q, (patient_id, status) if status else (patient_id,)).fetchall()
    audit(actor, patient_id, "read", f"tasks ({status or 'all'})")
    return [dict(r) for r in rows]


def next_tasks(patient_id, actor="system", limit=3):
    """Pending tasks in order: today's first, then dated ones, then undated (e.g. from a slip)."""
    return tasks(patient_id, "pending", actor)[:limit]


def mark_done(patient_id, task_id=None, hint=None, actor="system", require_match=False):
    """Mark a task done by id, or by a word the person used ("blood test ho gaya" -> the task whose title mentions blood).
    With no match, marks the first pending task. Returns the task marked, or None."""
    pending = tasks(patient_id, "pending", actor)
    if not pending:
        return None
    pick = None
    if task_id:
        pick = next((t for t in pending if t["id"] == task_id), None)
    elif hint:
        words = [w for w in re.findall(r"[a-z]+|[ऀ-ॿ]+", hint.lower()) if len(w) > 2]
        pick = next((t for t in pending if any(w in (t["title"] + " " + (t["title_hi"] or "")).lower() for w in words)), None)
    if pick is None and require_match:
        return None
    pick = pick or pending[0]
    with _lock:
        _conn.execute("UPDATE tasks SET status='done', done_at=? WHERE id=?", (_now(), pick["id"]))
        _conn.commit()
    audit(actor, patient_id, "task_done", pick["title"])
    return pick


def done_tasks(patient_id, actor="system", limit=3):
    rows = _conn.execute("SELECT * FROM tasks WHERE patient_id=? AND status='done' ORDER BY done_at DESC LIMIT ?",
                         (patient_id, limit)).fetchall()
    audit(actor, patient_id, "read", "done tasks")
    return [dict(r) for r in rows]


# ---------------- outbox + reminders ----------------
def push_outbox(patient_id, text, channel="webchat"):
    with _lock:
        _conn.execute("INSERT INTO outbox (patient_id, ts, text, channel) VALUES (?,?,?,?)", (patient_id, _now(), text, channel))
        _conn.commit()


def pull_outbox(patient_id):
    rows = _conn.execute("SELECT id, ts, text FROM outbox WHERE patient_id=? AND delivered=0 AND channel='webchat' ORDER BY id",
                         (patient_id,)).fetchall()
    if rows:
        with _lock:
            _conn.executemany("UPDATE outbox SET delivered=1 WHERE id=?", [(r["id"],) for r in rows])
            _conn.commit()
    return [dict(r) for r in rows]


def due_for_reminder(day):
    """Pending tasks due on `day` (YYYY-MM-DD) with a patient who has consented."""
    rows = _conn.execute("SELECT t.* FROM tasks t JOIN patients p ON p.id=t.patient_id "
                         "WHERE t.status='pending' AND t.due_date=? AND p.consent_at IS NOT NULL", (day,)).fetchall()
    return [dict(r) for r in rows]


def reminder_sent(task_id, kind):
    return _conn.execute("SELECT 1 FROM reminders WHERE task_id=? AND kind=?", (task_id, kind)).fetchone() is not None


def record_reminder(task_id, kind, channel, status):
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO reminders VALUES (?,?,?,?,?)", (task_id, kind, _now(), channel, status))
        _conn.commit()
