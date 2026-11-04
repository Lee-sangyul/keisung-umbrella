"""Keisung Umbrella server MVP: FastAPI + SQLite, admin session login."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)  # .env is authoritative for this single-instance MVP.
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "umbrellas.db"
COOKIE = "umbrella_admin"
APP_ORIGIN = os.getenv("APP_ORIGIN", "").rstrip("/")
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "false").lower() == "true"
app = FastAPI(title="Keisung Umbrella API", docs_url=None, redoc_url=None)
STATES = {"AVAILABLE", "BORROWED", "LOST", "DAMAGED", "DISPOSED"}
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
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_student
            ON umbrellas(borrower) WHERE borrower IS NOT NULL;
        """)
    finally:
        c.close()
    with transaction() as c:
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
    classroom: int = Field(ge=1, le=12)
    number: int = Field(ge=1, le=40)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class StatusRequest(BaseModel):
    status: str
    reason: str = Field(default="", max_length=160)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT / "kiosk.html", headers={"Cache-Control": "no-store"})


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
def admin_page():
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/umbrellas")
def umbrellas():
    with connect() as c:
        rows = c.execute("SELECT id,status FROM umbrellas ORDER BY id").fetchall()
    return [{"id": uid(r["id"]), "status": r["status"]} for r in rows]


@app.post("/api/borrow")
def borrow(data: StudentRequest):
    student = f"{data.grade}-{data.classroom}-{data.number}"
    with transaction() as c:
        u = c.execute("SELECT * FROM umbrellas WHERE id=?", (parse_id(data.umbrella_id),)).fetchone()
        if not u:
            raise HTTPException(404, "등록된 우산이 아닙니다.")
        if u["status"] != "AVAILABLE":
            raise HTTPException(409, "이 우산은 대여할 수 없습니다.")
        if c.execute("SELECT 1 FROM umbrellas WHERE borrower=?", (student,)).fetchone():
            raise HTTPException(409, "이미 대여 중인 우산이 있습니다.")
        rental = secrets.token_hex(16)
        c.execute("UPDATE umbrellas SET status='BORROWED',borrower=?,rental_id=? WHERE id=?",
                  (student, rental, u["id"]))
        log(c, u["id"], "BORROW", student, rental, actor="self-service")
    return {"ok": True, "id": data.umbrella_id, "status": "BORROWED"}


@app.post("/api/return")
def return_umbrella(data: StudentRequest):
    student = f"{data.grade}-{data.classroom}-{data.number}"
    with transaction() as c:
        u = c.execute("SELECT * FROM umbrellas WHERE id=?", (parse_id(data.umbrella_id),)).fetchone()
        if not u or u["status"] != "BORROWED":
            raise HTTPException(409, "현재 대여 중인 우산이 아닙니다.")
        if u["borrower"] != student:
            raise HTTPException(403, "등록된 대여자 정보와 다릅니다.")
        log(c, u["id"], "RETURN", student, u["rental_id"], actor="self-service")
        c.execute("UPDATE umbrellas SET status='AVAILABLE',borrower=NULL,rental_id=NULL WHERE id=?", (u["id"],))
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
    payload = {"schema_version":"2.0","umbrellas":[{"id":uid(r["id"]),"status":r["status"],"borrower":r["borrower"],"rental_id":r["rental_id"]} for r in rows],"events":[dict(e) for e in events]}
    return Response(json.dumps(payload,ensure_ascii=False,indent=2),media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="umbrella-backup.json"', "Cache-Control":"no-store"})
