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
        CREATE TABLE IF NOT EXISTS student_pin_approvals (
            student_id TEXT PRIMARY KEY REFERENCES student_pins(student_id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED')),
            approval_code_hash TEXT,
            requested_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by TEXT,
            review_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS pin_approval_queue_idx
            ON student_pin_approvals(status,requested_at);
        CREATE TABLE IF NOT EXISTS student_pin_review_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pin_review_student_idx
            ON student_pin_review_log(student_id,id);
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
        # Existing self-enrolled PINs become PENDING. Staff-issued PINs retain access.
        c.execute("""INSERT OR IGNORE INTO student_pin_approvals(
            student_id,status,approval_code_hash,requested_at,reviewed_at,reviewed_by,review_reason)
            SELECT student_id,
                   CASE WHEN updated_by='self-enrollment' THEN 'PENDING' ELSE 'APPROVED' END,
                   NULL,updated_at,
                   CASE WHEN updated_by='self-enrollment' THEN NULL ELSE updated_at END,
                   CASE WHEN updated_by='self-enrollment' THEN NULL ELSE updated_by END,
                   CASE WHEN updated_by='self-enrollment' THEN NULL ELSE '기존 관리자 발급 PIN 이관' END
            FROM student_pins""")
        c.execute("INSERT OR IGNORE INTO policies(key,value) VALUES('monthly_limit',10)")
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


def log(c, umbrella_id, action, student=None, rental=None, actor="system", reason=""):
    c.execute("INSERT INTO events(umbrella_id,action,student_id,rental_id,actor,reason,created_at) VALUES(?,?,?,?,?,?,?)",
              (umbrella_id, action, student, rental, actor, reason, now()))


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
    month,start,end=korean_month_window()
    count=c.execute(
        "SELECT COUNT(*) FROM events WHERE action='BORROW' AND student_id=? AND created_at>=? AND created_at<?",
        (student,start,end)
    ).fetchone()[0]
    limit=c.execute("SELECT value FROM policies WHERE key='monthly_limit'").fetchone()[0]
    row=c.execute("SELECT extra FROM student_allowances WHERE student_id=? AND month=?",(student,month)).fetchone()
    extra=row["extra"] if row else 0
    # A penalty deducts that many borrowing opportunities in its issue month.
    # Reversal restores the deduction, even when the correction is made later.
    # Previous months' penalties are not re-applied every future month.
    penalty=c.execute(
        """SELECT COALESCE(SUM(p.points),0)
           FROM penalty_actions p
           WHERE p.student_id=? AND p.reverses_id IS NULL
             AND p.created_at>=? AND p.created_at<?
             AND NOT EXISTS (
               SELECT 1 FROM penalty_actions reversal
               WHERE reversal.reverses_id=p.id
             )""",
        (student,start,end)
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


class StudentPinApproveRequest(BaseModel):
    verified_identity: bool = False
    approval_code: str = Field(min_length=8,max_length=8,pattern=r"^[A-HJ-NP-Z2-9]{8}$")


class StudentPinRejectRequest(BaseModel):
    reason: str = Field(min_length=5,max_length=500)


class StudentPinIssueRequest(BaseModel):
    student_id: str
    verified_identity: bool = False


def new_approval_code() -> str:
    # Not a PIN. Only the applicant receives this verification ticket.
    return "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))


def approval_code_digest(code: str) -> str:
    return hashlib.sha256(("keisung-pin-approval:v1:" + code).encode("ascii")).hexdigest()


def student_pin_hash(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode("ascii"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def check_student_pin(c, student: str, pin: str) -> str:
    """Return ok/invalid/locked. Never throw inside transaction or failed-attempt updates roll back."""
    current = int(time.time())
    row = c.execute(
        """SELECT p.*,a.status AS approval_status FROM student_pins p
           LEFT JOIN student_pin_approvals a ON a.student_id=p.student_id
           WHERE p.student_id=?""", (student,)
    ).fetchone()
    # Missing approval record is not an implicit grant.
    if row and row["approval_status"] != "APPROVED":
        return "rejected" if row["approval_status"] == "REJECTED" else "pending"
    if row and row["locked_until"] > current:
        return "locked"
    salt = row["salt"] if row else b"0" * 16
    hashed = student_pin_hash(pin, salt)  # Dummy hash even for missing students.
    if row and hmac.compare_digest(hashed, row["pin_hash"]):
        if row["failed_attempts"] or row["locked_until"]:
            c.execute(
                "UPDATE student_pins SET failed_attempts=0,last_failed_at=0,locked_until=0 WHERE student_id=?",
                (student,)
            )
        return "ok"
    if row:
        failures = (row["failed_attempts"] if current - row["last_failed_at"] < 900 else 0) + 1
        lock = current + 900 if failures >= 5 else 0
        c.execute(
            "UPDATE student_pins SET failed_attempts=?,last_failed_at=?,locked_until=? WHERE student_id=?",
            (failures, current, lock, student)
        )
        if lock:
            return "locked"
    return "invalid"


def student_pin_error(result: str):
    if result == "pending":
        raise HTTPException(403, "담당 선생님의 최초 PIN 등록 승인을 기다리고 있습니다.")
    if result == "rejected":
        raise HTTPException(403, "PIN 등록 신청이 반려되었습니다. 담당 선생님께 새 PIN 발급을 요청하세요.")
    if result == "locked":
        raise HTTPException(429, "PIN 입력 실패가 누적되어 15분간 인증이 제한됩니다. 담당 선생님께 문의하세요.")
    if result != "ok":
        raise HTTPException(401, "학생 PIN이 일치하지 않거나 등록되지 않았습니다. 담당 선생님께 확인하세요.")


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
        c.execute(
            """INSERT INTO student_pins(student_id,salt,pin_hash,failed_attempts,last_failed_at,
               locked_until,updated_at,updated_by) VALUES(?,?,?,0,0,0,?,?)
               ON CONFLICT(student_id) DO UPDATE SET salt=excluded.salt,pin_hash=excluded.pin_hash,
               failed_attempts=0,last_failed_at=0,locked_until=0,
               updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
            (student, salt, digest, now(), actor)
        )
        c.execute(
            """INSERT INTO student_pin_approvals
               (student_id,status,approval_code_hash,requested_at,reviewed_at,reviewed_by,review_reason)
               VALUES(?,'APPROVED',NULL,?,?,?,'관리자 PIN 발급 및 신원 확인')
               ON CONFLICT(student_id) DO UPDATE SET
                 status='APPROVED',approval_code_hash=NULL,
                 reviewed_at=excluded.reviewed_at,reviewed_by=excluded.reviewed_by,
                 review_reason=excluded.review_reason""",
            (student, now(), now(), actor)
        )
        c.execute(
            "INSERT INTO student_pin_review_log(student_id,action,actor,reason,created_at) VALUES(?,'ISSUED',?,?,?)",
            (student,actor,"담당 교사 신원 확인 후 PIN 발급",now())
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
        record = c.execute(
            """SELECT p.student_id,a.status FROM student_pins p
               LEFT JOIN student_pin_approvals a ON p.student_id=a.student_id
               WHERE p.student_id=?""", (student,)
        ).fetchone()
    return Response(
        json.dumps({"has_pin": record is not None,
                    "approval_status":(record["status"] or "PENDING") if record else "NOT_REGISTERED"}),
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
    approval_code = new_approval_code()
    with transaction() as c:
        # No UPSERT: never overwrite existing credentials, even during concurrent requests.
        cur = c.execute(
            """INSERT OR IGNORE INTO student_pins(
                 student_id,salt,pin_hash,failed_attempts,last_failed_at,
                 locked_until,updated_at,updated_by)
               VALUES(?,?,?,0,0,0,?,?)""",
            (student, salt, student_pin_hash(data.pin, salt), now(), "self-enrollment")
        )
        created = cur.rowcount == 1
        if created:
            c.execute(
                """INSERT INTO student_pin_approvals
                   (student_id,status,approval_code_hash,requested_at)
                   VALUES(?,'PENDING',?,?)""",
                (student,approval_code_digest(approval_code),now())
            )
            c.execute(
                "INSERT INTO student_pin_review_log(student_id,action,actor,created_at) VALUES(?,'REQUESTED',?,?)",
                (student,student,now())
            )
    if not created:
        raise HTTPException(
            409, "이미 PIN 신청 내역이 있습니다. 담당 선생님께 승인 상태를 확인하거나 새 PIN 발급을 요청하세요."
        )
    return Response(
        json.dumps({"ok": True, "student_id": student,
                    "approval_status": "PENDING","approval_code": approval_code}, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "no-store"}
    )



@app.get("/api/admin/student-pin/requests")
def pending_student_pin_requests(request: Request):
    """Admin-only approval queue; PINs and confirmation codes are never returned."""
    require_admin(request)
    with connect() as c:
        rows = c.execute(
            """SELECT student_id,status,requested_at,
                      approval_code_hash IS NOT NULL AS has_approval_code
               FROM student_pin_approvals
               WHERE status='PENDING'
               ORDER BY requested_at ASC LIMIT 1000"""
        ).fetchall()
    return Response(json.dumps({"pending_count":len(rows),"requests":[dict(r) for r in rows]},
                               ensure_ascii=False),media_type="application/json",
                    headers={"Cache-Control":"no-store"})


@app.post("/api/admin/student-pin/requests/{student_id}/approve")
def approve_student_pin_request(student_id: str, data: StudentPinApproveRequest,
                                request: Request):
    actor = require_admin(request, csrf=True)
    student = validate_student(student_id)
    if not data.verified_identity:
        raise HTTPException(422,"학생의 신원을 직접 확인한 뒤 승인해 주세요.")
    with transaction() as c:
        record = c.execute(
            "SELECT status,approval_code_hash FROM student_pin_approvals WHERE student_id=?",
            (student,)
        ).fetchone()
        if not record or record["status"] != "PENDING":
            raise HTTPException(409,"승인 대기 중인 PIN 신청이 아닙니다.")
        if not record["approval_code_hash"]:
            raise HTTPException(409,"기존 신청에는 확인 코드가 없습니다. 관리자 PIN을 재발급해 주세요.")
        if not hmac.compare_digest(approval_code_digest(data.approval_code),
                                   record["approval_code_hash"]):
            raise HTTPException(403,"학생이 제시한 신청 확인 코드가 일치하지 않습니다.")
        c.execute(
            """UPDATE student_pin_approvals
               SET status='APPROVED',approval_code_hash=NULL,reviewed_at=?,
                   reviewed_by=?,review_reason='신원 및 신청 확인 코드 대조 완료'
               WHERE student_id=? AND status='PENDING'""",
            (now(),actor,student)
        )
        c.execute(
            "INSERT INTO student_pin_review_log(student_id,action,actor,reason,created_at) VALUES(?,'APPROVED',?,?,?)",
            (student,actor,"대면 신원 및 신청 코드 확인",now())
        )
    return {"ok":True,"student_id":student,"approval_status":"APPROVED"}


@app.post("/api/admin/student-pin/requests/{student_id}/reject")
def reject_student_pin_request(student_id: str,data: StudentPinRejectRequest,
                               request: Request):
    actor = require_admin(request,csrf=True)
    student = validate_student(student_id)
    reason = data.reason.strip()
    if len(reason)<5:
        raise HTTPException(422,"반려 사유는 5자 이상 작성해 주세요.")
    with transaction() as c:
        cur = c.execute(
            """UPDATE student_pin_approvals
               SET status='REJECTED',approval_code_hash=NULL,
                   reviewed_at=?,reviewed_by=?,review_reason=?
               WHERE student_id=? AND status='PENDING'""",
            (now(),actor,reason,student)
        )
        if not cur.rowcount:
            raise HTTPException(409,"승인 대기 중인 PIN 신청이 아닙니다.")
        c.execute(
            "INSERT INTO student_pin_review_log(student_id,action,actor,reason,created_at) VALUES(?,'REJECTED',?,?,?)",
            (student,actor,reason,now())
        )
    return {"ok":True,"student_id":student,"approval_status":"REJECTED"}


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
    return c.execute("SELECT COALESCE(SUM(points),0) FROM penalty_actions WHERE student_id=?",(student,)).fetchone()[0]


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
        usage=month_usage(c,student)
        pin_status=c.execute(
            """SELECT p.locked_until,p.updated_at,a.status AS approval_status
               FROM student_pins p LEFT JOIN student_pin_approvals a
               ON a.student_id=p.student_id WHERE p.student_id=?""",(student,)
        ).fetchone()
        umbrella=c.execute("SELECT id,status FROM umbrellas WHERE borrower=?",(student,)).fetchone()
        penalties=c.execute("""SELECT id,student_id,umbrella_id,code,points,reason,actor,created_at,reverses_id
          FROM penalty_actions WHERE student_id=? ORDER BY id DESC LIMIT 200""",(student,)).fetchall()
        total=penalty_total(c,student)
    return {"student_id":student,"usage":usage,"has_pin":bool(pin_status),
        "pin_approval_status":(pin_status["approval_status"] or "PENDING") if pin_status else "NOT_REGISTERED",
        "pin_locked":bool(pin_status and pin_status["locked_until"]>int(time.time())),
        "active_umbrella":uid(umbrella["id"]) if umbrella else None,
        "penalty_points":total,
        "penalties":[{**dict(p),"umbrella_id":uid(p["umbrella_id"]) if p["umbrella_id"] else None} for p in penalties]}


@app.put("/api/admin/student-allowance")
def set_allowance(data: AllowanceRequest,request: Request):
    actor=require_admin(request,csrf=True)
    student=validate_student(data.student_id)
    month,_,_=korean_month_window()
    with transaction() as c:
        c.execute("""INSERT INTO student_allowances(student_id,month,extra,updated_by,updated_at)
          VALUES(?,?,?,?,?) ON CONFLICT(student_id,month) DO UPDATE SET extra=excluded.extra,
          updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
          (student,month,data.extra,actor,now()))
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
        if umbrella_num is not None and not c.execute("SELECT 1 FROM umbrellas WHERE id=?",(umbrella_num,)).fetchone():
            raise HTTPException(404,"등록되지 않은 우산입니다.")
        cur=c.execute("""INSERT INTO penalty_actions
          (student_id,umbrella_id,code,points,reason,actor,created_at,reverses_id)
          VALUES(?,?,?,?,?,?,?,NULL)""",
          (student,umbrella_num,data.code,PENALTY_POINTS[data.code],data.reason,actor,now()))
        total=penalty_total(c,student)
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
          (student_id,umbrella_id,code,points,reason,actor,created_at,reverses_id)
          VALUES(?,?,?,?,?,?,?,?)""",
          (row["student_id"],row["umbrella_id"],"REVERSAL",-row["points"],data.reason,actor,now(),penalty_id))
        total=penalty_total(c,row["student_id"])
    return {"ok":True,"penalty_points":total}


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT / "kiosk.html", headers={"Cache-Control": "no-store"})


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
        authentication = check_student_pin(c, student, data.pin)
        if authentication != "ok":
            rejection = authentication
        else:
            u = c.execute("SELECT * FROM umbrellas WHERE id=?", (parse_id(data.umbrella_id),)).fetchone()
            if not u:
                raise HTTPException(404, "등록된 우산이 아닙니다.")
            if u["status"] != "AVAILABLE":
                raise HTTPException(409, "이 우산은 대여할 수 없습니다.")
            if c.execute("SELECT 1 FROM umbrellas WHERE borrower=?", (student,)).fetchone():
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
            c.execute("UPDATE umbrellas SET status='BORROWED',borrower=?,rental_id=? WHERE id=?",
                      (student, rental, u["id"]))
            log(c, u["id"], "BORROW", student, rental, actor="self-service")
    student_pin_error(rejection or "ok")
    return {"ok": True, "id": data.umbrella_id, "status": "BORROWED", "remaining":usage["remaining"]-1}


@app.post("/api/return")
def return_umbrella(data: StudentRequest):
    student = f"{data.grade}-{data.classroom}-{data.number}"
    rejection = None
    with transaction() as c:
        authentication = check_student_pin(c, student, data.pin)
        if authentication != "ok":
            rejection = authentication
        else:
            u = c.execute("SELECT * FROM umbrellas WHERE id=?", (parse_id(data.umbrella_id),)).fetchone()
            if not u or u["status"] != "BORROWED":
                raise HTTPException(409, "현재 대여 중인 우산이 아닙니다.")
            if u["borrower"] != student:
                raise HTTPException(403, "등록된 대여자 정보와 다릅니다.")
            log(c, u["id"], "RETURN", student, u["rental_id"], actor="self-service")
            c.execute("UPDATE umbrellas SET status='AVAILABLE',borrower=NULL,rental_id=NULL WHERE id=?", (u["id"],))
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
        pin_approvals = c.execute(
            """SELECT student_id,status,requested_at,reviewed_at,reviewed_by,review_reason
               FROM student_pin_approvals ORDER BY student_id"""
        ).fetchall()
        pin_reviews = c.execute("SELECT * FROM student_pin_review_log ORDER BY id").fetchall()
        stocktake_sessions = c.execute("SELECT * FROM stocktake_sessions ORDER BY id").fetchall()
        stocktake_items = c.execute("SELECT * FROM stocktake_items ORDER BY session_id,umbrella_id").fetchall()
        umbrella_locations = c.execute("SELECT * FROM umbrella_locations ORDER BY umbrella_id").fetchall()
    payload = {
        "schema_version":"3.2",
        "exported_at":now(),
        "umbrellas":[{"id":uid(r["id"]),"status":r["status"],"borrower":r["borrower"],"rental_id":r["rental_id"]} for r in rows],
        "events":[dict(e) for e in events],
        "policies":[dict(p) for p in policies],
        "student_allowances":[dict(a) for a in allowances],
        "penalty_actions":[dict(p) for p in penalties],
        "student_pin_approvals":[dict(a) for a in pin_approvals],
        "student_pin_review_log":[dict(r) for r in pin_reviews],
        "umbrella_locations":[dict(loc) for loc in umbrella_locations],
        "stocktake_sessions":[dict(sess) for sess in stocktake_sessions],
        "stocktake_items":[dict(item) for item in stocktake_items]
    }
    return Response(json.dumps(payload,ensure_ascii=False,indent=2),media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="umbrella-backup.json"', "Cache-Control":"no-store"})
