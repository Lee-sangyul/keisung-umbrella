"""Keisung Umbrella server MVP: FastAPI + SQLite, admin session login."""
import hashlib
import csv
import io
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import segno
import school_year as academic

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)  # .env is authoritative for this single-instance MVP.
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "umbrellas.db"
COOKIE = "umbrella_admin"
APP_ORIGIN = os.getenv("APP_ORIGIN", "").rstrip("/")
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "false").lower() == "true"
app = FastAPI(title="Keisung Umbrella API", docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")
STATES = {"AVAILABLE", "BORROWED", "LOST", "DAMAGED", "DISPOSED"}
SEOUL = ZoneInfo("Asia/Seoul")
STUDENT_RE = re.compile(r"^[1-6]-[1-4]-(?:[1-9]|[12][0-9]|3[0-6])$")
STAFF_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,19}$")
STAFF_PREFIX = "STAFF:"
PENALTY_POINTS = {"LOST": 3, "ACCIDENTAL_DAMAGE": 1, "INTENTIONAL_DAMAGE": 5, "WARNING": 0}
ID_RE = re.compile(r"^UMB-\d{4,}$")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
if not ADMIN_USERNAME or len(ADMIN_PASSWORD) < 12 or ADMIN_PASSWORD.startswith("CHANGE_ME"):
    raise RuntimeError("Set ADMIN_USERNAME and a real ADMIN_PASSWORD (12+ characters) in .env before starting.")


def password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def connect():
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c


@contextmanager
def transaction():
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def init_db():
    c = connect()
    try:
        c.executescript("""CREATE TABLE IF NOT EXISTS umbrellas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL DEFAULT 'AVAILABLE',
            borrower TEXT,
            rental_id TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            umbrella_id INTEGER NOT NULL REFERENCES umbrellas(id),
            action TEXT NOT NULL,
            student_id TEXT,
            rental_id TEXT,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admins (
            username TEXT PRIMARY KEY,
            salt BLOB NOT NULL,
            pw_hash BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL REFERENCES admins(username),
            csrf TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS student_pins (
            student_id TEXT PRIMARY KEY,
            salt BLOB NOT NULL,
            pin_hash BLOB NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            last_failed_at INTEGER NOT NULL DEFAULT 0,
            locked_until INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS staff_accounts (
            staff_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            salt BLOB NOT NULL,
            pin_hash BLOB NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            last_failed_at INTEGER NOT NULL DEFAULT 0,
            locked_until INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS staff_account_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id TEXT NOT NULL REFERENCES staff_accounts(staff_id),
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS staff_events_idx
            ON staff_account_events(staff_id,id);
        CREATE TABLE IF NOT EXISTS umbrella_locations (
            umbrella_id INTEGER PRIMARY KEY REFERENCES umbrellas(id),
            location TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS umbrella_location_idx
            ON umbrella_locations(location,umbrella_id);
        CREATE TABLE IF NOT EXISTS stocktake_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            location TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('OPEN','COMPLETED')),
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            completed_at TEXT,
            completed_by TEXT
        );
        CREATE TABLE IF NOT EXISTS stocktake_items (
            session_id INTEGER NOT NULL REFERENCES stocktake_sessions(id),
            umbrella_id INTEGER NOT NULL REFERENCES umbrellas(id),
            expected INTEGER NOT NULL CHECK(expected IN (0,1)),
            snapshot_status TEXT NOT NULL,
            seen_at TEXT,
            seen_by TEXT,
            PRIMARY KEY(session_id,umbrella_id)
        );
        CREATE INDEX IF NOT EXISTS stocktake_item_session_idx
            ON stocktake_items(session_id,expected,seen_at);
        CREATE TABLE IF NOT EXISTS rental_terms (
            rental_id TEXT PRIMARY KEY,
            umbrella_id INTEGER NOT NULL REFERENCES umbrellas(id),
            student_id TEXT NOT NULL,
            borrowed_at TEXT NOT NULL,
            due_at TEXT NOT NULL,
            returned_at TEXT,
            issued_by TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS rental_terms_due_idx
            ON rental_terms(returned_at,due_at);
        CREATE TABLE IF NOT EXISTS rental_extensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rental_id TEXT NOT NULL REFERENCES rental_terms(rental_id),
            previous_due_at TEXT NOT NULL,
            due_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policies (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS student_allowances (
            student_id TEXT NOT NULL,
            month TEXT NOT NULL,
            extra INTEGER NOT NULL DEFAULT 0,
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(student_id,month)
        );
        CREATE TABLE IF NOT EXISTS penalty_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            umbrella_id INTEGER REFERENCES umbrellas(id),
            code TEXT NOT NULL,
            points INTEGER NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reverses_id INTEGER UNIQUE REFERENCES penalty_actions(id)
        );
        CREATE INDEX IF NOT EXISTS penalty_student_idx ON penalty_actions(student_id,id);
        CREATE INDEX IF NOT EXISTS events_student_action_idx ON events(student_id,action,created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_student
            ON umbrellas(borrower) WHERE borrower IS NOT NULL;
        """)
    finally:
        c.close()
    with transaction() as c:
        c.execute("INSERT OR IGNORE INTO policies(key,value) VALUES('monthly_limit',10)")
        c.execute("INSERT OR IGNORE INTO policies(key,value) VALUES('rental_days',7)")
        academic.init_identity(c,datetime.now(timezone.utc).isoformat())
        # Give existing active rentals a fresh grace period from first upgrade:
        # old loans never had a promised due date, so avoid retroactive overdue.
        migration_time = datetime.now(timezone.utc)
        grace_due = (migration_time + timedelta(days=7)).isoformat()
        c.execute(
            """INSERT OR IGNORE INTO rental_terms
                 (rental_id,umbrella_id,student_id,borrowed_at,due_at,issued_by)
               SELECT u.rental_id,u.id,u.borrower,
                 COALESCE((SELECT e.created_at FROM events e
                           WHERE e.rental_id=u.rental_id AND e.action='BORROW'
                           ORDER BY e.seq DESC LIMIT 1),?),
                 ?,'migration'
               FROM umbrellas u
               WHERE u.status='BORROWED' AND u.rental_id IS NOT NULL AND u.borrower IS NOT NULL""",
            (migration_time.isoformat(),grace_due)
        )
        salt = secrets.token_bytes(16)
        # .env is the source of truth for the single bootstrap admin.
        # Updating it must also update an account that was already created in SQLite.
        c.execute("DELETE FROM sessions")
        c.execute("DELETE FROM admins WHERE username <> ?", (ADMIN_USERNAME,))
        c.execute("""INSERT INTO admins(username,salt,pw_hash) VALUES (?,?,?)
                     ON CONFLICT(username) DO UPDATE SET
                       salt=excluded.salt,pw_hash=excluded.pw_hash""",
                  (ADMIN_USERNAME, salt, password_hash(ADMIN_PASSWORD, salt)))


init_db()


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(num):
    return f"UMB-{num:04d}"


def parse_id(value):
    if not ID_RE.fullmatch(value or ""):
        raise HTTPException(422, "우산 ID 형식이 올바르지 않습니다.")
    return int(value.split("-")[1])


def log(c, umbrella_id, action, student=None, rental=None, actor="system", reason="",student_uid=None):
    c.execute("""INSERT INTO events(umbrella_id,action,student_id,rental_id,actor,reason,created_at,student_uid)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (umbrella_id, action, student, rental, actor, reason, now(),student_uid))


def verify_origin(request: Request):
    """Same-origin protection that works behind Codespaces' HTTPS reverse proxy.

    Compare the browser's Origin against the public Host header. Do not blindly
    trust arbitrary X-Forwarded-Host or X-Forwarded-Proto headers.
    APP_ORIGIN can explicitly allow a public reverse-proxy origin if Host differs.
    """
    origin = request.headers.get("origin", "")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(403, "요청 출처가 올바르지 않습니다.")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise HTTPException(403, "요청 출처가 올바르지 않습니다.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(403, "외부 접속은 HTTPS를 사용해야 합니다.")

    requested_host = request.headers.get("host", "").lower()
    trusted_origins = set()
    if requested_host:
        trusted_origins.add(f"{parsed.scheme}://{requested_host}")
    if APP_ORIGIN:
        trusted_origins.add(APP_ORIGIN.lower())

    if origin.lower().rstrip("/") not in trusted_origins:
        raise HTTPException(403, "요청 출처가 올바르지 않습니다. .env의 APP_ORIGIN을 실제 접속 주소로 확인하세요.")


def require_admin(request: Request, csrf=False):
    token = request.cookies.get(COOKIE, "")
    if not token:
        raise HTTPException(401, "관리자 로그인이 필요합니다.")
    key = hashlib.sha256(token.encode()).hexdigest()
    with connect() as c:
        row = c.execute("SELECT username,csrf,expires_at FROM sessions WHERE token_hash=?", (key,)).fetchone()
    if not row or row["expires_at"] <= int(time.time()):
        raise HTTPException(401, "로그인이 만료되었습니다.")
    if csrf:
        verify_origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf"]):
            raise HTTPException(403, "CSRF 인증에 실패했습니다.")
    return row["username"]


class StudentRequest(BaseModel):
    umbrella_id: str
    grade: int = Field(ge=1, le=6)
    classroom: int = Field(ge=1, le=4)
    number: int = Field(ge=1, le=36)
    pin: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class BatchRegisterRequest(BaseModel):
    count: int = Field(ge=1, le=500)


class StatusRequest(BaseModel):
    status: str
    reason: str = Field(default="", max_length=160)



def korean_month_window():
    local = datetime.now(SEOUL)
    start = local.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    end = start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1)
    return start.strftime("%Y-%m"),start.astimezone(timezone.utc).isoformat(),end.astimezone(timezone.utc).isoformat()


def month_usage(c,student):
    stable_id=academic.resolve_student(c,student)
    month,start,end=korean_month_window()
    count=c.execute(
        "SELECT COUNT(*) FROM events WHERE action='BORROW' AND student_uid=? AND created_at>=? AND created_at<?",
        (stable_id,start,end)
    ).fetchone()[0]
    limit=c.execute("SELECT value FROM policies WHERE key='monthly_limit'").fetchone()[0]
    row=c.execute("SELECT extra FROM student_allowances WHERE student_uid=? AND month=?",(stable_id,month)).fetchone()
    extra=row["extra"] if row else 0
    # A penalty deducts that many borrowing opportunities in its issue month.
    # Reversal restores the deduction, even when the correction is made later.
    # Previous months' penalties are not re-applied every future month.
    penalty=c.execute(
        """SELECT COALESCE(SUM(p.points),0)
           FROM penalty_actions p
           WHERE p.student_uid=? AND p.reverses_id IS NULL
             AND p.created_at>=? AND p.created_at<?
             AND NOT EXISTS (
               SELECT 1 FROM penalty_actions reversal
               WHERE reversal.reverses_id=p.id
             )""",
        (stable_id,start,end)
    ).fetchone()[0]
    deducted=max(0,penalty)
    effective_limit=max(0,limit+extra-deducted)
    return {"month":month,"used":count,"limit":limit,"extra":extra,
            "penalty_deduction":deducted,"effective_limit":effective_limit,
            "remaining":max(0,effective_limit-count)}


def validate_student(student):
    if not STUDENT_RE.fullmatch(student):
        raise HTTPException(422,"학생 정보는 1~6학년, 1~4반, 1~36번이어야 합니다.")
    return student


class StudentPinStatusRequest(BaseModel):
    grade: int = Field(ge=1, le=6)
    classroom: int = Field(ge=1, le=4)
    number: int = Field(ge=1, le=36)


class StudentPinEnrollRequest(StudentPinStatusRequest):
    pin: str = Field(pattern=r"^\d{6}$", min_length=6, max_length=6)
    pin_confirm: str = Field(pattern=r"^\d{6}$", min_length=6, max_length=6)


class StudentPinIssueRequest(BaseModel):
    student_id: str
    verified_identity: bool = False


def student_pin_hash(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode("ascii"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def check_student_pin(c, student: str, pin: str) -> str:
    """Return ok/invalid/locked. Never throw inside transaction or failed-attempt updates roll back."""
    current = int(time.time())
    stable_id = academic.resolve_student(c,student)
    row = c.execute(
        "SELECT * FROM student_credentials WHERE student_uid=?", (stable_id,)
    ).fetchone()
    if row and row["locked_until"] > current:
        return "locked"
    salt = row["salt"] if row else b"0" * 16
    hashed = student_pin_hash(pin, salt)  # Dummy hash even for missing students.
    if row and hmac.compare_digest(hashed, row["pin_hash"]):
        if row["failed_attempts"] or row["locked_until"]:
            c.execute(
                "UPDATE student_credentials SET failed_attempts=0,last_failed_at=0,locked_until=0 WHERE student_uid=?",
                (stable_id,)
            )
        return "ok"
    if row:
        failures = (row["failed_attempts"] if current - row["last_failed_at"] < 900 else 0) + 1
        lock = current + 900 if failures >= 5 else 0
        c.execute(
            "UPDATE student_credentials SET failed_attempts=?,last_failed_at=?,locked_until=? WHERE student_uid=?",
            (failures, current, lock, stable_id)
        )
        if lock:
            return "locked"
    return "invalid"


def student_pin_error(result: str):
    if result == "locked":
        raise HTTPException(429, "PIN 입력 실패가 누적되어 15분간 인증이 제한됩니다. 담당 선생님께 문의하세요.")
    if result != "ok":
        raise HTTPException(401, "학생 PIN이 일치하지 않거나 등록되지 않았습니다. 담당 선생님께 확인하세요.")



class StaffAccountRequest(BaseModel):
    staff_id: str = Field(min_length=3,max_length=20)
    display_name: str = Field(min_length=2,max_length=50)
    verified_identity: bool = False


class StaffPinResetRequest(BaseModel):
    verified_identity: bool = False


class StaffActiveRequest(BaseModel):
    active: bool


class StaffBorrowRequest(BaseModel):
    staff_id: str = Field(min_length=3,max_length=20)
    umbrella_id: str
    pin: str = Field(min_length=6,max_length=6,pattern=r"^\d{6}$")


def validate_staff_id(value: str) -> str:
    result=value.strip().upper()
    if not STAFF_ID_RE.fullmatch(result):
        raise HTTPException(422,"교직원 ID는 영문 대문자·숫자·_·-로 된 3~20자여야 합니다.")
    return result


def staff_borrower(staff_id: str) -> str:
    return STAFF_PREFIX+staff_id


def staff_public_record(row) -> dict:
    return {"staff_id":row["staff_id"],"display_name":row["display_name"],
            "active":bool(row["active"]),"created_at":row["created_at"],
            "updated_at":row["updated_at"],"updated_by":row["updated_by"]}


def generate_staff_pin() -> tuple[str,bytes,bytes]:
    pin=f"{secrets.randbelow(1000000):06d}"
    salt=secrets.token_bytes(16)
    return pin,salt,student_pin_hash(pin,salt)


def check_staff_pin(c,staff_id: str,pin: str,allow_disabled_return: bool=False) -> str:
    """Like student PIN authentication, record failures inside a committed transaction."""
    current=int(time.time())
    row=c.execute("SELECT * FROM staff_accounts WHERE staff_id=?",(staff_id,)).fetchone()
    if row and row["locked_until"]>current:
        return "locked"
    salt=row["salt"] if row else b"0"*16
    guess=student_pin_hash(pin,salt)
    verified=bool(row and hmac.compare_digest(guess,row["pin_hash"]))
    if verified:
        if row["failed_attempts"] or row["locked_until"]:
            c.execute("""UPDATE staff_accounts SET failed_attempts=0,last_failed_at=0,locked_until=0
                         WHERE staff_id=?""",(staff_id,))
        if not row["active"] and not allow_disabled_return:
            return "disabled"
        return "ok"
    if row:
        failures=(row["failed_attempts"] if current-row["last_failed_at"]<900 else 0)+1
        lock=current+900 if failures>=5 else 0
        c.execute("""UPDATE staff_accounts SET failed_attempts=?,last_failed_at=?,locked_until=?
                     WHERE staff_id=?""",(failures,current,lock,staff_id))
        if lock:
            return "locked"
    return "invalid"


def staff_pin_error(result: str):
    if result=="locked":
        raise HTTPException(429,"PIN 입력 오류가 누적되어 15분간 잠겼습니다.")
    if result=="disabled":
        raise HTTPException(403,"비활성화된 교직원 계정입니다. 관리자에게 문의하세요.")
    if result!="ok":
        raise HTTPException(401,"교직원 ID 또는 PIN을 확인해 주세요.")


@app.get("/api/admin/staff")
def admin_staff_list(request: Request):
    require_admin(request)
    with connect() as c:
        rows=c.execute("""SELECT staff_id,display_name,active,created_at,updated_at,updated_by
                          FROM staff_accounts ORDER BY staff_id""").fetchall()
    return {"staff":[staff_public_record(row) for row in rows]}


@app.post("/api/admin/staff")
def admin_staff_create(data: StaffAccountRequest,request: Request):
    actor=require_admin(request,csrf=True)
    staff_id=validate_staff_id(data.staff_id)
    display_name=data.display_name.strip()
    if len(display_name)<2:
        raise HTTPException(422,"교직원 이름 또는 표시명을 두 글자 이상 입력해 주세요.")
    if not data.verified_identity:
        raise HTTPException(422,"교직원의 신원을 직접 확인해 주세요.")
    pin,salt,digest=generate_staff_pin()
    with transaction() as c:
        exists=c.execute("SELECT 1 FROM staff_accounts WHERE staff_id=?",(staff_id,)).fetchone()
        if exists:
            raise HTTPException(409,"이미 등록된 교직원 ID입니다. PIN 재발급을 사용하세요.")
        c.execute(
            """INSERT INTO staff_accounts(staff_id,display_name,salt,pin_hash,
                                           created_at,updated_at,updated_by)
               VALUES(?,?,?,?,?,?,?)""",
            (staff_id,display_name,salt,digest,now(),now(),actor))
        c.execute("""INSERT INTO staff_account_events(staff_id,action,actor,created_at)
                     VALUES(?,'CREATED',?,?)""",(staff_id,actor,now()))
    return Response(json.dumps({"ok":True,"staff_id":staff_id,"pin":pin},ensure_ascii=False),
                    media_type="application/json",headers={"Cache-Control":"no-store","Pragma":"no-cache"})


@app.post("/api/admin/staff/{staff_id}/pin-reset")
def admin_staff_reset_pin(staff_id: str,data: StaffPinResetRequest,request: Request):
    actor=require_admin(request,csrf=True)
    staff_id=validate_staff_id(staff_id)
    if not data.verified_identity:
        raise HTTPException(422,"PIN 재발급 전 교직원의 신원 확인이 필요합니다.")
    pin,salt,digest=generate_staff_pin()
    with transaction() as c:
        cur=c.execute(
            """UPDATE staff_accounts SET salt=?,pin_hash=?,failed_attempts=0,
                       last_failed_at=0,locked_until=0,updated_at=?,updated_by=?
               WHERE staff_id=?""",(salt,digest,now(),actor,staff_id))
        if not cur.rowcount:
            raise HTTPException(404,"교직원 계정을 찾을 수 없습니다.")
        c.execute("""INSERT INTO staff_account_events(staff_id,action,actor,created_at)
                     VALUES(?,'PIN_RESET',?,?)""",(staff_id,actor,now()))
    return Response(json.dumps({"ok":True,"staff_id":staff_id,"pin":pin},ensure_ascii=False),
                    media_type="application/json",headers={"Cache-Control":"no-store","Pragma":"no-cache"})


@app.put("/api/admin/staff/{staff_id}/status")
def admin_staff_set_active(staff_id: str,data: StaffActiveRequest,request: Request):
    actor=require_admin(request,csrf=True)
    staff_id=validate_staff_id(staff_id)
    with transaction() as c:
        cur=c.execute("""UPDATE staff_accounts SET active=?,updated_at=?,updated_by=?
                         WHERE staff_id=?""",(int(data.active),now(),actor,staff_id))
        if not cur.rowcount:
            raise HTTPException(404,"교직원 계정을 찾을 수 없습니다.")
        c.execute("""INSERT INTO staff_account_events(staff_id,action,actor,created_at)
                     VALUES(?,?,?,?)""",
                  (staff_id,"ENABLED" if data.active else "DISABLED",actor,now()))
    return {"ok":True,"staff_id":staff_id,"active":data.active}


@app.post("/api/staff/borrow")
def staff_borrow(data: StaffBorrowRequest,request: Request):
    verify_origin(request)
    staff_id=validate_staff_id(data.staff_id)
    identity=staff_borrower(staff_id)
    rejection=None
    with transaction() as c:
        rejection=check_staff_pin(c,staff_id,data.pin)
        if rejection=="ok":
            umbrella=c.execute("SELECT * FROM umbrellas WHERE id=?",(parse_id(data.umbrella_id),)).fetchone()
            if not umbrella:
                raise HTTPException(404,"등록되지 않은 우산입니다.")
            if umbrella["status"]!="AVAILABLE":
                raise HTTPException(409,"현재 대여할 수 없는 우산입니다.")
            if c.execute("SELECT 1 FROM umbrellas WHERE borrower=?",(identity,)).fetchone():
                raise HTTPException(409,"교직원 한 명당 우산은 한 개만 대여할 수 있습니다.")
            rental=secrets.token_hex(16)
            borrowed_at=now()
            days=c.execute("SELECT value FROM policies WHERE key='rental_days'").fetchone()[0]
            due_at=utc_due_in_days(days,datetime.fromisoformat(borrowed_at))
            c.execute("""UPDATE umbrellas SET status='BORROWED',borrower=?,rental_id=? WHERE id=?""",
                      (identity,rental,umbrella["id"]))
            c.execute("""INSERT INTO rental_terms
                         (rental_id,umbrella_id,student_id,borrowed_at,due_at,issued_by)
                         VALUES(?,?,?,?,?,'staff-self-service')""",
                      (rental,umbrella["id"],identity,borrowed_at,due_at))
            log(c,umbrella["id"],"BORROW",identity,rental,actor="staff-self-service")
    staff_pin_error(rejection)
    return {"ok":True,"id":data.umbrella_id,"status":"BORROWED","due_at":due_at}


@app.post("/api/staff/return")
def staff_return(data: StaffBorrowRequest,request: Request):
    verify_origin(request)
    staff_id=validate_staff_id(data.staff_id)
    identity=staff_borrower(staff_id)
    rejection=None
    with transaction() as c:
        # Disabled staff may return their outstanding umbrella; no new borrowing.
        rejection=check_staff_pin(c,staff_id,data.pin,allow_disabled_return=True)
        if rejection=="ok":
            umbrella=c.execute("SELECT * FROM umbrellas WHERE id=?",(parse_id(data.umbrella_id),)).fetchone()
            if not umbrella or umbrella["status"]!="BORROWED":
                raise HTTPException(409,"현재 대여 중인 우산이 아닙니다.")
            if umbrella["borrower"]!=identity:
                raise HTTPException(403,"해당 교직원의 대여 기록이 아닙니다.")
            log(c,umbrella["id"],"RETURN",identity,umbrella["rental_id"],actor="staff-self-service")
            c.execute("""UPDATE rental_terms SET returned_at=? WHERE rental_id=?
                         AND returned_at IS NULL""",(now(),umbrella["rental_id"]))
            c.execute("""UPDATE umbrellas SET status='AVAILABLE',borrower=NULL,rental_id=NULL
                         WHERE id=?""",(umbrella["id"],))
    staff_pin_error(rejection)
    return {"ok":True,"id":data.umbrella_id,"status":"AVAILABLE"}


@app.post("/api/admin/student-pin")
def issue_student_pin(data: StudentPinIssueRequest, request: Request):
    """Only an admin who verified the student's identity may create/reset a one-time PIN."""
    actor = require_admin(request, csrf=True)
    student = validate_student(data.student_id)
    if not data.verified_identity:
        raise HTTPException(422, "학생 신원을 직접 확인한 뒤 발급해 주세요.")
    pin = f"{secrets.randbelow(1000000):06d}"
    salt = secrets.token_bytes(16)
    digest = student_pin_hash(pin, salt)
    with transaction() as c:
        stable_id=academic.resolve_student(c,student,create=True,timestamp=now(),actor=actor)
        c.execute(
            """INSERT INTO student_credentials(student_uid,salt,pin_hash,failed_attempts,last_failed_at,
               locked_until,updated_at,updated_by) VALUES(?,?,?,0,0,0,?,?)
               ON CONFLICT(student_uid) DO UPDATE SET salt=excluded.salt,pin_hash=excluded.pin_hash,
               failed_attempts=0,last_failed_at=0,locked_until=0,
               updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
            (stable_id, salt, digest, now(), actor)
        )
    return Response(content=json.dumps({"student_id":student,"pin":pin},ensure_ascii=False),
                    media_type="application/json",
                    headers={"Cache-Control":"no-store","Pragma":"no-cache"})



@app.post("/api/student-pin/status")
def student_pin_status(data: StudentPinStatusRequest, request: Request):
    """Check whether the student already has a PIN; never return a PIN value."""
    verify_origin(request)
    student = validate_student(f"{data.grade}-{data.classroom}-{data.number}")
    with connect() as c:
        stable_id=academic.resolve_student(c,student)
        registered=bool(stable_id and c.execute(
            "SELECT 1 FROM student_credentials WHERE student_uid=?", (stable_id,)
        ).fetchone())
    return Response(
        json.dumps({"has_pin": registered}),
        media_type="application/json",
        headers={"Cache-Control": "no-store"}
    )


@app.post("/api/student-pin/enroll")
def enroll_student_pin(data: StudentPinEnrollRequest, request: Request):
    """First-time student creates a PIN. Existing PINs cannot be overwritten."""
    verify_origin(request)
    student = validate_student(f"{data.grade}-{data.classroom}-{data.number}")
    if data.pin != data.pin_confirm:
        raise HTTPException(422, "PIN 확인 입력이 일치하지 않습니다.")
    if len(set(data.pin)) == 1 or data.pin in {"123456", "654321", "121212", "112233"}:
        raise HTTPException(422, "너무 쉬운 PIN입니다. 다른 숫자 6자리로 설정해 주세요.")
    salt = secrets.token_bytes(16)
    with transaction() as c:
        # Race-safe registration bound to an immutable student UID, not class number.
        stable_id=academic.resolve_student(c,student,create=True,timestamp=now())
        cur = c.execute(
            """INSERT OR IGNORE INTO student_credentials(
                 student_uid,salt,pin_hash,failed_attempts,last_failed_at,
                 locked_until,updated_at,updated_by)
               VALUES(?,?,?,0,0,0,?,?)""",
            (stable_id, salt, student_pin_hash(data.pin, salt), now(), "self-enrollment")
        )
        created = cur.rowcount == 1
    if not created:
        raise HTTPException(
            409, "이미 PIN을 설정한 학생입니다. 기존 PIN으로 이용하거나 담당 선생님께 재발급을 요청하세요."
        )
    return Response(
        json.dumps({"ok": True, "student_id": student}, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "no-store"}
    )



class RentalPolicyRequest(BaseModel):
    rental_days: int = Field(ge=1,le=30)


class RentalExtendRequest(BaseModel):
    extra_days: int = Field(ge=1,le=30)
    reason: str = Field(min_length=5,max_length=500)


def utc_due_in_days(days: int,base: datetime | None = None) -> str:
    return ((base or datetime.now(timezone.utc)) + timedelta(days=days)).isoformat()


def rental_is_overdue(due_at: str) -> bool:
    return datetime.fromisoformat(due_at) < datetime.now(timezone.utc)


@app.get("/api/admin/rental-policy")
def get_rental_policy(request: Request):
    require_admin(request)
    with connect() as c:
        days=c.execute("SELECT value FROM policies WHERE key='rental_days'").fetchone()[0]
    return {"rental_days":days}


@app.put("/api/admin/rental-policy")
def update_rental_policy(data: RentalPolicyRequest,request: Request):
    actor=require_admin(request,csrf=True)
    with transaction() as c:
        c.execute("UPDATE policies SET value=? WHERE key='rental_days'",(data.rental_days,))
    return {"ok":True,"rental_days":data.rental_days,"updated_by":actor}


@app.get("/api/admin/rentals")
def list_active_rentals(request: Request):
    """Private due-date and overdue dashboard; never returned by public APIs."""
    require_admin(request)
    with connect() as c:
        rows=c.execute(
            """SELECT u.id AS umbrella_id,u.borrower,t.rental_id,t.borrowed_at,t.due_at
               FROM umbrellas u
               JOIN rental_terms t ON t.rental_id=u.rental_id
               WHERE u.status='BORROWED' AND t.returned_at IS NULL
               ORDER BY t.due_at ASC,u.id ASC"""
        ).fetchall()
    rentals=[{"umbrella_id":uid(r["umbrella_id"]),"student_id":r["borrower"],
              "rental_id":r["rental_id"],"borrowed_at":r["borrowed_at"],
              "due_at":r["due_at"],"overdue":rental_is_overdue(r["due_at"])}
             for r in rows]
    return {"total":len(rentals),"overdue_count":sum(r["overdue"] for r in rentals),
            "rentals":rentals}


@app.post("/api/admin/rentals/{rental_id}/extend")
def extend_active_rental(rental_id: str,data: RentalExtendRequest,request: Request):
    actor=require_admin(request,csrf=True)
    if not re.fullmatch(r"[0-9a-f]{32}",rental_id):
        raise HTTPException(422,"대여 식별자가 올바르지 않습니다.")
    reason=data.reason.strip()
    if len(reason)<5:
        raise HTTPException(422,"연장 사유를 5자 이상 작성해 주세요.")
    with transaction() as c:
        row=c.execute(
            """SELECT t.due_at FROM rental_terms t JOIN umbrellas u
               ON u.rental_id=t.rental_id
               WHERE t.rental_id=? AND t.returned_at IS NULL AND u.status='BORROWED'""",
            (rental_id,)
        ).fetchone()
        if not row:
            raise HTTPException(409,"현재 대여 중인 우산이 아니거나 이미 반납됐습니다.")
        previous_due_at=row["due_at"]
        base=max(datetime.now(timezone.utc),datetime.fromisoformat(previous_due_at))
        due_at=utc_due_in_days(data.extra_days,base)
        c.execute("UPDATE rental_terms SET due_at=? WHERE rental_id=?",
                  (due_at,rental_id))
        c.execute(
            """INSERT INTO rental_extensions(rental_id,previous_due_at,due_at,reason,actor,created_at)
               VALUES(?,?,?,?,?,?)""",
            (rental_id,previous_due_at,due_at,reason,actor,now())
        )
    return {"ok":True,"rental_id":rental_id,"previous_due_at":previous_due_at,
            "due_at":due_at,"extended_days":data.extra_days}


class PolicyRequest(BaseModel):
    monthly_limit: int = Field(ge=1,le=100)


class AllowanceRequest(BaseModel):
    student_id: str
    extra: int = Field(ge=0,le=100)


class PenaltyRequest(BaseModel):
    student_id: str
    umbrella_id: str | None = None
    code: str
    reason: str = Field(min_length=5,max_length=500)
    confirmed: bool = False


class ReversePenaltyRequest(BaseModel):
    reason: str = Field(min_length=5,max_length=500)


def penalty_total(c,student):
    return penalty_total_uid(c,academic.resolve_student(c,student))


def penalty_total_uid(c,stable_id):
    return c.execute("SELECT COALESCE(SUM(points),0) FROM penalty_actions WHERE student_uid=?",
                     (stable_id,)).fetchone()[0]


@app.get("/api/admin/policy")
def get_policy(request: Request):
    require_admin(request)
    with connect() as c:
        return {"monthly_limit":c.execute("SELECT value FROM policies WHERE key='monthly_limit'").fetchone()[0]}


@app.put("/api/admin/policy")
def update_policy(data: PolicyRequest,request: Request):
    actor=require_admin(request,csrf=True)
    with transaction() as c:
        c.execute("UPDATE policies SET value=? WHERE key='monthly_limit'",(data.monthly_limit,))
    return {"ok":True,"monthly_limit":data.monthly_limit,"updated_by":actor}


@app.get("/api/admin/student-record")
def student_record(student_id: str,request: Request):
    require_admin(request)
    student=validate_student(student_id)
    with connect() as c:
        stable_id=academic.resolve_student(c,student)
        usage=month_usage(c,student)
        pin_status=c.execute("SELECT locked_until,updated_at FROM student_credentials WHERE student_uid=?",
                             (stable_id,)).fetchone()
        umbrella=c.execute(
            """SELECT u.id,u.status,t.due_at FROM umbrellas u
               LEFT JOIN rental_terms t ON t.rental_id=u.rental_id
               WHERE u.borrower_uid=?""",(stable_id,)
        ).fetchone()
        penalties=c.execute("""SELECT id,student_id,umbrella_id,code,points,reason,actor,created_at,reverses_id
          FROM penalty_actions WHERE student_uid=? ORDER BY id DESC LIMIT 200""",(stable_id,)).fetchall()
        total=penalty_total_uid(c,stable_id)
    return {"student_id":student,"student_uid":stable_id,"usage":usage,"has_pin":bool(pin_status),
        "pin_locked":bool(pin_status and pin_status["locked_until"]>int(time.time())),
        "active_umbrella":uid(umbrella["id"]) if umbrella else None,
        "active_due_at":umbrella["due_at"] if umbrella else None,
        "active_overdue":bool(umbrella and umbrella["due_at"] and rental_is_overdue(umbrella["due_at"])),
        "penalty_points":total,
        "penalties":[{**dict(p),"umbrella_id":uid(p["umbrella_id"]) if p["umbrella_id"] else None} for p in penalties]}


@app.put("/api/admin/student-allowance")
def set_allowance(data: AllowanceRequest,request: Request):
    actor=require_admin(request,csrf=True)
    student=validate_student(data.student_id)
    month,_,_=korean_month_window()
    with transaction() as c:
        stable_id=academic.resolve_student(c,student,create=True,timestamp=now(),actor=actor)
        c.execute("""INSERT INTO student_allowances(student_id,month,extra,updated_by,updated_at,student_uid)
          VALUES(?,?,?,?,?,?) ON CONFLICT(student_id,month) DO UPDATE SET extra=excluded.extra,
          updated_by=excluded.updated_by,updated_at=excluded.updated_at,
          student_uid=excluded.student_uid""",
          (student,month,data.extra,actor,now(),stable_id))
        usage=month_usage(c,student)
    return {"ok":True,"usage":usage}


@app.post("/api/admin/penalties")
def add_penalty(data: PenaltyRequest,request: Request):
    actor=require_admin(request,csrf=True)
    student=validate_student(data.student_id)
    if not data.confirmed:
        raise HTTPException(422,"담당 교사의 사실관계 확인이 필요합니다.")
    if data.code not in PENALTY_POINTS:
        raise HTTPException(422,"지원하지 않는 패널티 유형입니다.")
    umbrella_num=parse_id(data.umbrella_id) if data.umbrella_id else None
    with transaction() as c:
        stable_id=academic.resolve_student(c,student,create=True,timestamp=now(),actor=actor)
        if umbrella_num is not None and not c.execute("SELECT 1 FROM umbrellas WHERE id=?",(umbrella_num,)).fetchone():
            raise HTTPException(404,"등록되지 않은 우산입니다.")
        cur=c.execute("""INSERT INTO penalty_actions
          (student_id,umbrella_id,code,points,reason,actor,created_at,reverses_id,student_uid)
          VALUES(?,?,?,?,?,?,?,NULL,?)""",
          (student,umbrella_num,data.code,PENALTY_POINTS[data.code],data.reason,actor,now(),stable_id))
        total=penalty_total_uid(c,stable_id)
    return {"ok":True,"id":cur.lastrowid,"penalty_points":total}


@app.post("/api/admin/penalties/{penalty_id}/reverse")
def reverse_penalty(penalty_id: int,data: ReversePenaltyRequest,request: Request):
    actor=require_admin(request,csrf=True)
    with transaction() as c:
        row=c.execute("SELECT * FROM penalty_actions WHERE id=?",(penalty_id,)).fetchone()
        if not row or row["reverses_id"] is not None:
            raise HTTPException(404,"해당 패널티가 없습니다.")
        if c.execute("SELECT 1 FROM penalty_actions WHERE reverses_id=?",(penalty_id,)).fetchone():
            raise HTTPException(409,"이미 취소된 패널티입니다.")
        c.execute("""INSERT INTO penalty_actions
          (student_id,umbrella_id,code,points,reason,actor,created_at,reverses_id,student_uid)
          VALUES(?,?,?,?,?,?,?,?,?)""",
          (row["student_id"],row["umbrella_id"],"REVERSAL",-row["points"],data.reason,actor,now(),penalty_id,row["student_uid"]))
        total=penalty_total_uid(c,row["student_uid"])
    return {"ok":True,"penalty_points":total}


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT / "kiosk.html", headers={"Cache-Control": "no-store"})


@app.get("/staff", include_in_schema=False)
@app.get("/staff/", include_in_schema=False)
def staff_page():
    """Backward-compatible path: use the same unified kiosk without a separate staff UI."""
    return Response(status_code=307,headers={"Location":"/","Cache-Control":"no-store"})


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
def admin_page():
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/admin/print", include_in_schema=False)
@app.get("/admin/print/", include_in_schema=False)
def print_page():
    return FileResponse(ROOT / "print.html", headers={"Cache-Control": "no-store"})


@app.get("/api/umbrellas")
def umbrellas():
    with connect() as c:
        rows = c.execute("SELECT id,status FROM umbrellas ORDER BY id").fetchall()
    return [{"id": uid(r["id"]), "status": r["status"]} for r in rows]


@app.post("/api/borrow")
def borrow(data: StudentRequest):
    student = f"{data.grade}-{data.classroom}-{data.number}"
    rejection = None
    usage = None
    with transaction() as c:
        stable_id=academic.resolve_student(c,student)
        authentication = check_student_pin(c, student, data.pin)
        if authentication != "ok":
            rejection = authentication
        else:
            u = c.execute("SELECT * FROM umbrellas WHERE id=?", (parse_id(data.umbrella_id),)).fetchone()
            if not u:
                raise HTTPException(404, "등록된 우산이 아닙니다.")
            if u["status"] != "AVAILABLE":
                raise HTTPException(409, "이 우산은 대여할 수 없습니다.")
            if c.execute("SELECT 1 FROM umbrellas WHERE borrower_uid=?", (stable_id,)).fetchone():
                raise HTTPException(409, "이미 대여 중인 우산이 있습니다.")
            usage = month_usage(c,student)
            if usage["remaining"]<=0:
                raise HTTPException(
                    409,
                    f"이번 달 대여 가능 횟수를 모두 사용했습니다. "
                    f"(기본 {usage['limit']}회 + 추가 {usage['extra']}회 "
                    f"- 이번 달 패널티 {usage['penalty_deduction']}회 = "
                    f"최대 {usage['effective_limit']}회) 담당 선생님께 문의해 주세요."
                )
            rental = secrets.token_hex(16)
            borrowed_at=now()
            rental_days=c.execute("SELECT value FROM policies WHERE key='rental_days'").fetchone()[0]
            due_at=utc_due_in_days(rental_days,datetime.fromisoformat(borrowed_at))
            c.execute("""UPDATE umbrellas SET status='BORROWED',borrower=?,borrower_uid=?,rental_id=?
                         WHERE id=?""",(student,stable_id,rental,u["id"]))
            c.execute(
                """INSERT INTO rental_terms
                   (rental_id,umbrella_id,student_id,borrowed_at,due_at,issued_by,student_uid)
                   VALUES(?,?,?,?,?,'self-service',?)""",
                (rental,u["id"],student,borrowed_at,due_at,stable_id)
            )
            log(c, u["id"], "BORROW", student, rental, actor="self-service",student_uid=stable_id)
    student_pin_error(rejection or "ok")
    return {"ok": True, "id": data.umbrella_id, "status": "BORROWED",
            "remaining":usage["remaining"]-1,"due_at":due_at}


@app.post("/api/return")
def return_umbrella(data: StudentRequest):
    student = f"{data.grade}-{data.classroom}-{data.number}"
    rejection = None
    with transaction() as c:
        stable_id=academic.resolve_student(c,student)
        authentication = check_student_pin(c, student, data.pin)
        if authentication != "ok":
            rejection = authentication
        else:
            u = c.execute("SELECT * FROM umbrellas WHERE id=?", (parse_id(data.umbrella_id),)).fetchone()
            if not u or u["status"] != "BORROWED":
                raise HTTPException(409, "현재 대여 중인 우산이 아닙니다.")
            if not stable_id or u["borrower_uid"]!=stable_id:
                raise HTTPException(403, "등록된 대여자 정보와 다릅니다.")
            log(c, u["id"], "RETURN", student, u["rental_id"], actor="self-service",student_uid=stable_id)
            c.execute("UPDATE rental_terms SET returned_at=? WHERE rental_id=? AND returned_at IS NULL",
                      (now(),u["rental_id"]))
            c.execute("UPDATE umbrellas SET status='AVAILABLE',borrower=NULL,borrower_uid=NULL,rental_id=NULL WHERE id=?", (u["id"],))
    student_pin_error(rejection or "ok")
    return {"ok": True, "id": data.umbrella_id, "status": "AVAILABLE"}


@app.post("/api/admin/login")
def login(data: LoginRequest, request: Request, response: Response):
    verify_origin(request)
    with connect() as c:
        row = c.execute("SELECT * FROM admins WHERE username=?", (data.username,)).fetchone()
    # Dummy hash to reduce username timing leaks.
    salt = row["salt"] if row else b"0" * 16
    digest = password_hash(data.password, salt)
    if not row or not hmac.compare_digest(digest, row["pw_hash"]):
        raise HTTPException(401, "계정 또는 비밀번호가 올바르지 않습니다.")
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with transaction() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
        c.execute("INSERT INTO sessions(token_hash,username,csrf,expires_at) VALUES(?,?,?,?)",
                  (hashlib.sha256(raw.encode()).hexdigest(), data.username, csrf, int(time.time()) + 8*3600))
    response.set_cookie(COOKIE, raw, httponly=True,
                        secure=SECURE_COOKIES or urlsplit(request.headers.get("origin", "")).scheme == "https",
                        samesite="strict", max_age=8*3600, path="/")
    return {"ok": True, "username": data.username, "csrf": csrf}


@app.get("/api/admin/me")
def me(request: Request):
    username = require_admin(request)
    token = request.cookies[COOKIE]
    with connect() as c:
        row = c.execute("SELECT csrf FROM sessions WHERE token_hash=?",
                        (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return {"username": username, "csrf": row["csrf"]}


@app.post("/api/admin/logout")
def logout(request: Request, response: Response):
    require_admin(request, csrf=True)
    with transaction() as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?",
                  (hashlib.sha256(request.cookies[COOKIE].encode()).hexdigest(),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.post("/api/admin/umbrellas")
def register(request: Request):
    actor = require_admin(request, csrf=True)
    with transaction() as c:
        cur = c.execute("INSERT INTO umbrellas(status) VALUES('AVAILABLE')")
        log(c, cur.lastrowid, "REGISTER", actor=actor)
        value = uid(cur.lastrowid)
    return {"ok": True, "id": value, "status": "AVAILABLE"}


@app.post("/api/admin/umbrellas/batch")
def register_batch(data: BatchRegisterRequest, request: Request):
    """Register 1–500 umbrellas atomically with unique, never reused IDs."""
    actor = require_admin(request, csrf=True)
    registered = []
    with transaction() as c:
        for _ in range(data.count):
            cur = c.execute("INSERT INTO umbrellas(status) VALUES('AVAILABLE')")
            log(c, cur.lastrowid, "REGISTER", actor=actor, reason="일괄 등록")
            registered.append(uid(cur.lastrowid))
    return {
        "ok": True,
        "count": len(registered),
        "ids": registered,
        "first_id": registered[0],
        "last_id": registered[-1],
    }


@app.patch("/api/admin/umbrellas/{umbrella_id}")
def change_state(umbrella_id: str, data: StatusRequest, request: Request):
    actor = require_admin(request, csrf=True)
    target = data.status
    if target not in {"LOST", "DAMAGED", "DISPOSED", "AVAILABLE"}:
        raise HTTPException(422, "지원하지 않는 상태입니다.")
    with transaction() as c:
        u = c.execute("SELECT * FROM umbrellas WHERE id=?", (parse_id(umbrella_id),)).fetchone()
        if not u:
            raise HTTPException(404, "등록되지 않은 우산입니다.")
        old = u["status"]
        if old == "DISPOSED":
            raise HTTPException(409, "폐기된 우산은 변경할 수 없습니다.")
        if target == old:
            raise HTTPException(409, "이미 같은 상태입니다.")
        if target == "AVAILABLE" and old not in {"LOST", "DAMAGED"}:
            raise HTTPException(409, "분실 또는 파손 상태만 복구할 수 있습니다.")
        action = "RESTORE" if target == "AVAILABLE" else target
        log(c, u["id"], action, u["borrower"], u["rental_id"], actor, data.reason)
        if u["rental_id"]:
            c.execute("UPDATE rental_terms SET returned_at=? WHERE rental_id=? AND returned_at IS NULL",
                      (now(),u["rental_id"]))
        c.execute("UPDATE umbrellas SET status=?,borrower=NULL,rental_id=NULL WHERE id=?", (target, u["id"]))
    return {"ok": True, "id": umbrella_id, "status": target}




class InventoryLocationRange(BaseModel):
    start_id: str
    end_id: str
    location: str = Field(min_length=1,max_length=50)


class StocktakeCreateRequest(BaseModel):
    title: str = Field(min_length=3,max_length=80)
    location: str = Field(default="ALL",min_length=1,max_length=50)


class StocktakeScanBatch(BaseModel):
    umbrella_ids: list[str] = Field(min_length=1,max_length=500)


def clean_location(value: str,allow_all: bool=False) -> str:
    location=value.strip()
    if not location or len(location)>50 or any(ord(ch)<32 for ch in location):
        raise HTTPException(422,"보관 위치를 1~50자로 입력해 주세요.")
    if location == "ALL" and not allow_all:
        raise HTTPException(422,"ALL은 학교 전체 실사 전용으로 예약된 위치명입니다.")
    return location


def stocktake_summary(c,session_id: int):
    row=c.execute(
        """SELECT s.id,s.title,s.location,s.status,s.created_at,s.created_by,
                  s.completed_at,s.completed_by,
                  COUNT(i.umbrella_id) AS recorded,
                  COALESCE(SUM(CASE WHEN i.expected=1 THEN 1 ELSE 0 END),0) AS expected,
                  COALESCE(SUM(CASE WHEN i.expected=1 AND i.seen_at IS NOT NULL THEN 1 ELSE 0 END),0) AS verified,
                  COALESCE(SUM(CASE WHEN i.expected=0 AND i.seen_at IS NOT NULL THEN 1 ELSE 0 END),0) AS unexpected
           FROM stocktake_sessions s
           LEFT JOIN stocktake_items i ON i.session_id=s.id
           WHERE s.id=?
           GROUP BY s.id""",(session_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404,"해당 실사 기록을 찾을 수 없습니다.")
    result=dict(row)
    result["missing"]=result["expected"]-result["verified"]
    return result


@app.put("/api/admin/inventory/locations")
def assign_inventory_location(data: InventoryLocationRange,request: Request):
    """Assign up to 500 consecutive registered umbrellas to a storage location."""
    actor=require_admin(request,csrf=True)
    location=clean_location(data.location)
    first,last=parse_id(data.start_id),parse_id(data.end_id)
    if first>last or last-first+1>500:
        raise HTTPException(422,"시작/끝 ID의 순서와 500개 이하의 범위를 확인해 주세요.")
    with transaction() as c:
        count=c.execute("SELECT COUNT(*) FROM umbrellas WHERE id BETWEEN ? AND ?",
                        (first,last)).fetchone()[0]
        if count!=last-first+1:
            raise HTTPException(404,"지정 범위에 등록되지 않은 우산 ID가 있습니다.")
        c.executemany(
            """INSERT INTO umbrella_locations(umbrella_id,location,updated_at,updated_by)
               VALUES(?,?,?,?)
               ON CONFLICT(umbrella_id) DO UPDATE SET
                 location=excluded.location,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
            [(num,location,now(),actor) for num in range(first,last+1)]
        )
    return {"ok":True,"count":count,"location":location,
            "first_id":uid(first),"last_id":uid(last)}


@app.get("/api/admin/stocktakes")
def list_stocktakes(request: Request):
    require_admin(request)
    with connect() as c:
        ids=c.execute("SELECT id FROM stocktake_sessions ORDER BY id DESC LIMIT 100").fetchall()
        rows=[stocktake_summary(c,row["id"]) for row in ids]
    return {"sessions":rows}


@app.post("/api/admin/stocktakes")
def create_stocktake(data: StocktakeCreateRequest,request: Request):
    actor=require_admin(request,csrf=True)
    title=data.title.strip()
    location=clean_location(data.location,allow_all=True)
    if len(title)<3:
        raise HTTPException(422,"실사 이름을 3자 이상 입력해 주세요.")
    with transaction() as c:
        if location!="ALL" and location!="미지정":
            exists=c.execute("SELECT 1 FROM umbrella_locations WHERE location=? LIMIT 1",
                             (location,)).fetchone()
            if not exists:
                raise HTTPException(422,"해당 보관 위치에 배정된 우산이 없습니다.")
        cur=c.execute(
            """INSERT INTO stocktake_sessions(title,location,status,created_at,created_by)
               VALUES(?,?,'OPEN',?,?)""",(title,location,now(),actor)
        )
        session_id=cur.lastrowid
        c.execute(
            """INSERT INTO stocktake_items(session_id,umbrella_id,expected,snapshot_status)
               SELECT ?,u.id,1,u.status FROM umbrellas u
               LEFT JOIN umbrella_locations loc ON loc.umbrella_id=u.id
               WHERE u.status IN ('AVAILABLE','DAMAGED')
                 AND (?='ALL' OR COALESCE(loc.location,'미지정')=?)""",
            (session_id,location,location)
        )
        result=stocktake_summary(c,session_id)
    return {"ok":True,"session":result}


@app.get("/api/admin/stocktakes/{session_id}")
def get_stocktake(session_id: int,request: Request):
    require_admin(request)
    with connect() as c:
        summary=stocktake_summary(c,session_id)
        items=c.execute(
            """SELECT i.umbrella_id,i.expected,i.snapshot_status,i.seen_at,i.seen_by,
                      u.status AS current_status,
                      COALESCE(loc.location,'미지정') AS current_location
               FROM stocktake_items i
               JOIN umbrellas u ON u.id=i.umbrella_id
               LEFT JOIN umbrella_locations loc ON loc.umbrella_id=i.umbrella_id
               WHERE i.session_id=?
               ORDER BY i.expected DESC,i.umbrella_id ASC""",
            (session_id,)
        ).fetchall()
    return {"session":summary,"items":[{**dict(i),"umbrella_id":uid(i["umbrella_id"])}
                                         for i in items]}


@app.post("/api/admin/stocktakes/{session_id}/scan-batch")
def scan_stocktake_batch(session_id: int,data: StocktakeScanBatch,request: Request):
    actor=require_admin(request,csrf=True)
    numbers=[parse_id(value) for value in data.umbrella_ids]
    with transaction() as c:
        sess=c.execute("SELECT status FROM stocktake_sessions WHERE id=?",
                       (session_id,)).fetchone()
        if not sess:
            raise HTTPException(404,"실사 세션을 찾을 수 없습니다.")
        if sess["status"]!="OPEN":
            raise HTTPException(409,"완료된 실사에는 번호를 추가할 수 없습니다.")
        results=[]
        for num in numbers:
            row=c.execute("SELECT status FROM umbrellas WHERE id=?",(num,)).fetchone()
            if not row:
                results.append({"id":uid(num),"result":"UNREGISTERED"})
                continue
            existing=c.execute(
                "SELECT expected,seen_at FROM stocktake_items WHERE session_id=? AND umbrella_id=?",
                (session_id,num)
            ).fetchone()
            if existing:
                if existing["seen_at"]:
                    results.append({"id":uid(num),"result":"DUPLICATE"})
                    continue
                c.execute(
                    "UPDATE stocktake_items SET seen_at=?,seen_by=? WHERE session_id=? AND umbrella_id=?",
                    (now(),actor,session_id,num)
                )
                results.append({"id":uid(num),"result":"VERIFIED" if existing["expected"] else "OUT_OF_SCOPE"})
            else:
                c.execute(
                    """INSERT INTO stocktake_items
                       (session_id,umbrella_id,expected,snapshot_status,seen_at,seen_by)
                       VALUES(?,?,0,?,?,?)""",
                    (session_id,num,row["status"],now(),actor)
                )
                results.append({"id":uid(num),"result":"OUT_OF_SCOPE"})
        summary=stocktake_summary(c,session_id)
    return {"ok":True,"results":results,"session":summary}


@app.post("/api/admin/stocktakes/{session_id}/complete")
def complete_stocktake(session_id: int,request: Request):
    actor=require_admin(request,csrf=True)
    with transaction() as c:
        cur=c.execute(
            """UPDATE stocktake_sessions SET status='COMPLETED',completed_at=?,completed_by=?
               WHERE id=? AND status='OPEN'""",
            (now(),actor,session_id)
        )
        if cur.rowcount!=1:
            raise HTTPException(409,"진행 중인 실사 세션이 아니거나 존재하지 않습니다.")
        result=stocktake_summary(c,session_id)
    return {"ok":True,"session":result}


@app.get("/api/admin/stocktakes/{session_id}/export")
def export_stocktake_csv(session_id: int,request: Request):
    """CSV only contains umbrella IDs, not student data or PINs."""
    require_admin(request)
    with connect() as c:
        summary=stocktake_summary(c,session_id)
        items=c.execute(
            """SELECT i.umbrella_id,i.expected,i.snapshot_status,i.seen_at,
                      u.status AS current_status,
                      COALESCE(loc.location,'미지정') AS current_location
               FROM stocktake_items i
               JOIN umbrellas u ON u.id=i.umbrella_id
               LEFT JOIN umbrella_locations loc ON loc.umbrella_id=i.umbrella_id
               WHERE i.session_id=? ORDER BY i.umbrella_id""",(session_id,)
        ).fetchall()
    out=io.StringIO()
    writer=csv.writer(out)
    writer.writerow(["umbrella_id","audit_result","snapshot_status",
                     "current_status","current_location","seen_at"])
    for item in items:
        audit_result=("VERIFIED" if item["seen_at"] else "MISSING") if item["expected"] else "OUT_OF_SCOPE"
        # Avoid spreadsheet formula interpretation from operator-provided location text.
        loc=item["current_location"]
        if loc and loc[0] in "=+-@":
            loc="'"+loc
        writer.writerow([uid(item["umbrella_id"]),audit_result,item["snapshot_status"],
                         item["current_status"],loc,item["seen_at"] or ""])
    return Response(chr(65279)+out.getvalue(),media_type="text/csv; charset=utf-8",
                    headers={"Cache-Control":"no-store",
                             "Content-Disposition":f'attachment; filename="stocktake-{session_id}.csv"'})


@app.get("/api/admin/inventory")
def admin_inventory(request: Request):
    """Private inventory view. Student identifiers never appear in the public list."""
    require_admin(request)
    with connect() as c:
        rows = c.execute(
            """SELECT u.id,u.status,u.borrower,u.rental_id,
                      COALESCE(loc.location,'미지정') AS location
               FROM umbrellas u LEFT JOIN umbrella_locations loc
               ON loc.umbrella_id=u.id ORDER BY u.id DESC"""
        ).fetchall()
    return [{"id": uid(r["id"]), "status": r["status"], "borrower": r["borrower"],
             "rental_id": r["rental_id"],"location":r["location"]} for r in rows]


class QRLabelRequest(BaseModel):
    umbrella_ids: list[str] = Field(min_length=1, max_length=200)


@app.post("/api/admin/qr-labels")
def qr_labels(data: QRLabelRequest, request: Request):
    """Only authenticated admins may generate verified umbrella QR labels.

    QR payloads contain only the public umbrella ID, never student information.
    """
    require_admin(request, csrf=True)
    identifiers = list(dict.fromkeys(parse_id(value) for value in data.umbrella_ids))
    with connect() as c:
        placeholders = ",".join("?" for _ in identifiers)
        rows = c.execute(
            f"SELECT id FROM umbrellas WHERE id IN ({placeholders})", identifiers
        ).fetchall()
    found = {row["id"] for row in rows}
    if len(found) != len(identifiers):
        raise HTTPException(404, "등록되지 않은 우산 ID가 포함되어 있습니다.")
    labels = []
    for number in identifiers:
        value = uid(number)
        output = io.BytesIO()
        qr = segno.make(value, error="m", micro=False)
        scale = 5
        border = 4  # QR quiet zone: four modules on each edge.
        qr.save(output, kind="svg", scale=scale, border=border, xmldecl=False)
        svg = output.getvalue().decode("utf-8")
        # Segno SVG uses explicit width/height but does not always include a
        # viewBox. When CSS sizes such an SVG, its path can be visibly cropped.
        # Explicit viewBox makes the entire matrix + quiet zone scale together.
        width, height = qr.symbol_size(scale=scale, border=border)
        svg = svg.replace(
            "<svg ", f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" ', 1
        )
        labels.append({"id": value, "svg": svg})
    return {"labels": labels}


@app.get("/api/admin/events")
def admin_events(request: Request):
    require_admin(request)
    with connect() as c:
        rows = c.execute("SELECT umbrella_id,action,student_id,rental_id,actor,reason,created_at FROM events ORDER BY seq DESC LIMIT 1000").fetchall()
    return [{"umbrella_id": uid(r["umbrella_id"]), **{k:r[k] for k in ("action","student_id","rental_id","actor","reason","created_at")}} for r in rows]


@app.get("/api/admin/export")
def export(request: Request):
    require_admin(request)
    with connect() as c:
        rows = c.execute("SELECT id,status,borrower,rental_id FROM umbrellas ORDER BY id").fetchall()
        events = c.execute("SELECT * FROM events ORDER BY seq").fetchall()
        policies = c.execute("SELECT key,value FROM policies ORDER BY key").fetchall()
        allowances = c.execute("SELECT * FROM student_allowances ORDER BY month,student_id").fetchall()
        penalties = c.execute("SELECT * FROM penalty_actions ORDER BY id").fetchall()
        stocktake_sessions = c.execute("SELECT * FROM stocktake_sessions ORDER BY id").fetchall()
        stocktake_items = c.execute("SELECT * FROM stocktake_items ORDER BY session_id,umbrella_id").fetchall()
        umbrella_locations = c.execute("SELECT * FROM umbrella_locations ORDER BY umbrella_id").fetchall()
        staff_accounts = c.execute(
            """SELECT staff_id,display_name,active,created_at,updated_at,updated_by
               FROM staff_accounts ORDER BY staff_id"""
        ).fetchall()
        staff_account_events = c.execute(
            "SELECT * FROM staff_account_events ORDER BY id"
        ).fetchall()
        rental_terms=c.execute("SELECT * FROM rental_terms ORDER BY borrowed_at").fetchall()
        rental_extensions=c.execute("SELECT * FROM rental_extensions ORDER BY id").fetchall()
    payload = {
        "schema_version":"3.4",
        "exported_at":now(),
        "umbrellas":[{"id":uid(r["id"]),"status":r["status"],"borrower":r["borrower"],"rental_id":r["rental_id"]} for r in rows],
        "events":[dict(e) for e in events],
        "policies":[dict(p) for p in policies],
        "student_allowances":[dict(a) for a in allowances],
        "penalty_actions":[dict(p) for p in penalties],
        "umbrella_locations":[dict(loc) for loc in umbrella_locations],
        "stocktake_sessions":[dict(sess) for sess in stocktake_sessions],
        "stocktake_items":[dict(item) for item in stocktake_items],
        "rental_terms":[dict(r) for r in rental_terms],
        "rental_extensions":[dict(x) for x in rental_extensions],
        "staff_accounts":[dict(row) for row in staff_accounts],
        "staff_account_events":[dict(row) for row in staff_account_events]
    }
    return Response(json.dumps(payload,ensure_ascii=False,indent=2),media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="umbrella-backup.json"', "Cache-Control":"no-store"})
