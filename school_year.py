"""Stable student identity and explicitly reviewed school-year transitions.

Legacy grade-class-number fields remain immutable historical snapshots.
Student accounts get stable opaque UIDs for PIN, quota, penalties and active loans.
All migrations and year transitions run under the caller's BEGIN IMMEDIATE lock.
"""
import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

STUDENT_RE = re.compile(r"^[1-6]-[1-4]-(?:[1-9]|[12][0-9]|3[0-6])$")
UID_RE = re.compile(r"^STU-[A-F0-9]{20}$")
SEOUL = ZoneInfo("Asia/Seoul")


def default_school_year():
    today = datetime.now(SEOUL)
    return today.year if today.month >= 3 else today.year - 1


def school_year(c):
    row = c.execute("SELECT value FROM policies WHERE key='school_year'").fetchone()
    if not row:
        raise RuntimeError("The academic year policy is missing; call init_identity first.")
    return row["value"]


def student_uid():
    return "STU-" + secrets.token_hex(10).upper()


def validate_label(label):
    if not isinstance(label, str) or not STUDENT_RE.fullmatch(label):
        raise HTTPException(422, "학년·반·번호는 1~6학년, 1~4반, 1~36번 형식이어야 합니다.")
    return label


def init_identity(c, timestamp):
    """Idempotent on existing SQLite DBs: do not reset PINs, due dates or history."""
    c.execute("INSERT OR IGNORE INTO policies(key,value) VALUES('school_year',?)",
              (default_school_year(),))
    statements = [
        """CREATE TABLE IF NOT EXISTS student_accounts (
            uid TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS student_rosters (
            school_year INTEGER NOT NULL, student_id TEXT NOT NULL,
            student_uid TEXT NOT NULL REFERENCES student_accounts(uid),
            PRIMARY KEY(school_year,student_id),
            UNIQUE(school_year,student_uid))""",
        """CREATE INDEX IF NOT EXISTS roster_by_student_uid
            ON student_rosters(student_uid,school_year)""",
        """CREATE TABLE IF NOT EXISTS student_credentials (
            student_uid TEXT PRIMARY KEY REFERENCES student_accounts(uid),
            salt BLOB NOT NULL, pin_hash BLOB NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            last_failed_at INTEGER NOT NULL DEFAULT 0,
            locked_until INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS legacy_student_owners (
            student_id TEXT PRIMARY KEY,
            student_uid TEXT NOT NULL REFERENCES student_accounts(uid))""",
        """CREATE TABLE IF NOT EXISTS academic_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_year INTEGER NOT NULL, to_year INTEGER NOT NULL UNIQUE,
            actor TEXT NOT NULL, completed_at TEXT NOT NULL,
            transferred INTEGER NOT NULL, graduated INTEGER NOT NULL,
            left_school INTEGER NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS academic_transition_entries (
            transition_id INTEGER NOT NULL REFERENCES academic_transitions(id),
            student_uid TEXT NOT NULL REFERENCES student_accounts(uid),
            old_student_id TEXT NOT NULL,
            new_student_id TEXT,
            action TEXT NOT NULL,
            PRIMARY KEY(transition_id,student_uid))""",
        """CREATE TABLE IF NOT EXISTS academic_assignment_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_year INTEGER NOT NULL, student_uid TEXT NOT NULL
                REFERENCES student_accounts(uid),
            old_student_id TEXT, new_student_id TEXT NOT NULL,
            actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)"""
    ]
    for sql in statements:
        c.execute(sql)
    # Old DBs have no permanent IDs on rental, penalty and event tables.
    columns = (
        ("events", "student_uid"),
        ("student_allowances", "student_uid"),
        ("penalty_actions", "student_uid"),
        ("rental_terms", "student_uid"),
        ("umbrellas", "borrower_uid"),
    )
    for table, column in columns:
        known = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
        if column not in known:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS events_student_uid_idx ON events(student_uid,action,created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS allowances_student_uid_idx ON student_allowances(student_uid,month)")
    c.execute("CREATE INDEX IF NOT EXISTS penalty_student_uid_idx ON penalty_actions(student_uid,id)")
    c.execute("CREATE INDEX IF NOT EXISTS rental_student_uid_idx ON rental_terms(student_uid)")
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_student_uid
                 ON umbrellas(borrower_uid) WHERE borrower_uid IS NOT NULL""")

    current = school_year(c)
    candidates=set()
    for table, field in (("student_pins","student_id"),
                         ("events","student_id"),
                         ("student_allowances","student_id"),
                         ("penalty_actions","student_id"),
                         ("rental_terms","student_id"),
                         ("umbrellas","borrower")):
        candidates.update(row[0] for row in c.execute(
            f"SELECT DISTINCT {field} FROM {table} WHERE {field} IS NOT NULL"
        ))
    for label in sorted(candidates):
        if not isinstance(label,str) or not STUDENT_RE.fullmatch(label):
            continue  # STAFF:* and system events never become student accounts.
        owner=c.execute("SELECT student_uid FROM legacy_student_owners WHERE student_id=?",
                        (label,)).fetchone()
        if owner:
            uid=owner["student_uid"]
        else:
            existing=c.execute(
                """SELECT student_uid FROM student_rosters
                   WHERE school_year=? AND student_id=?""",(current,label)
            ).fetchone()
            uid=existing["student_uid"] if existing else student_uid()
            if not existing:
                c.execute(
                    "INSERT INTO student_accounts(uid,active,created_at,created_by) VALUES(?,1,?,'legacy-migration')",
                    (uid,timestamp))
                c.execute(
                    """INSERT INTO student_rosters(school_year,student_id,student_uid)
                       VALUES(?,?,?)""",(current,label,uid))
            c.execute("INSERT INTO legacy_student_owners(student_id,student_uid) VALUES(?,?)",
                      (label,uid))
        row=c.execute("SELECT * FROM student_pins WHERE student_id=?",(label,)).fetchone()
        if row:
            c.execute(
                """INSERT OR IGNORE INTO student_credentials(
                    student_uid,salt,pin_hash,failed_attempts,last_failed_at,
                    locked_until,updated_at,updated_by)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (uid,row["salt"],row["pin_hash"],row["failed_attempts"],
                 row["last_failed_at"],row["locked_until"],row["updated_at"],row["updated_by"]))
        for table in ("events","student_allowances","penalty_actions","rental_terms"):
            c.execute(
                f"UPDATE {table} SET student_uid=? WHERE student_id=? AND student_uid IS NULL",
                (uid,label))
        c.execute(
            """UPDATE umbrellas SET borrower_uid=? WHERE borrower=?
               AND borrower_uid IS NULL""",(uid,label))


def resolve_student(c, label, create=False, timestamp=None, actor="self-enrollment"):
    validate_label(label)
    current=school_year(c)
    row=c.execute(
        """SELECT r.student_uid,a.active FROM student_rosters r
           JOIN student_accounts a ON a.uid=r.student_uid
           WHERE r.school_year=? AND r.student_id=?""",(current,label)
    ).fetchone()
    if row:
        return row["student_uid"] if row["active"] else None
    if not create:
        return None
    uid=student_uid()
    c.execute(
        """INSERT INTO student_accounts(uid,active,created_at,created_by)
           VALUES(?,1,?,?)""",(uid,timestamp or datetime.now(SEOUL).isoformat(),actor))
    c.execute("INSERT INTO student_rosters(school_year,student_id,student_uid) VALUES(?,?,?)",
              (current,label,uid))
    return uid


def display_for_uid(c, uid, year=None):
    if not uid:
        return None
    row=c.execute(
        """SELECT student_id FROM student_rosters WHERE student_uid=? AND school_year=?""",
        (uid,year or school_year(c))
    ).fetchone()
    return row["student_id"] if row else None


def account_roster(c):
    year=school_year(c)
    items=c.execute(
        """SELECT r.student_uid,r.student_id,a.active,
                  EXISTS(SELECT 1 FROM student_credentials k
                         WHERE k.student_uid=r.student_uid) AS has_pin,
                  EXISTS(SELECT 1 FROM umbrellas u
                         WHERE u.borrower_uid=r.student_uid AND u.status='BORROWED') AS active_loan
           FROM student_rosters r
           JOIN student_accounts a ON a.uid=r.student_uid
           WHERE r.school_year=? ORDER BY r.student_id""",(year,)
    ).fetchall()
    return [dict(row) for row in items]


def _canonical_transition(c, year, entries):
    current=school_year(c)
    if year != current+1:
        raise HTTPException(409,"다음 학년도만 적용할 수 있습니다. 서버의 현재 학년도를 확인하세요.")
    if not isinstance(entries,list) or not (1 <= len(entries) <= 2500):
        raise HTTPException(422,"학년도 배정은 1~2500개 항목을 제출해야 합니다.")
    roster={row["student_uid"]:row for row in account_roster(c)}
    if not roster:
        raise HTTPException(409,"현재 학년도에 학생이 없습니다.")
    seen_uid=set()
    seen_new=set()
    plans=[]
    errors=[]
    for entry in entries:
        if not isinstance(entry,dict):
            raise HTTPException(422,"배정 항목 형식이 잘못되었습니다.")
        uid=entry.get("student_uid")
        old=entry.get("old_student_id")
        new=entry.get("new_student_id")
        if not isinstance(uid,str) or not UID_RE.fullmatch(uid):
            raise HTTPException(422,"학생의 영구 UID 형식이 올바르지 않습니다.")
        if uid in seen_uid:
            raise HTTPException(422,"같은 학생 UID가 중복됐습니다.")
        seen_uid.add(uid)
        if uid not in roster or roster[uid]["student_id"]!=old:
            raise HTTPException(409,"이전 학년도 학생 정보 또는 UID가 현재 명단과 일치하지 않습니다.")
        if new in {"GRADUATE","LEAVE"}:
            action=new
            target=None
            if action=="GRADUATE" and not old.startswith("6-"):
                raise HTTPException(422,"졸업 처리는 6학년에게만 가능합니다. 전출은 LEAVE를 사용하세요.")
            if roster[uid]["active_loan"]:
                errors.append(old+" 학생은 대여 중인 우산이 있어 졸업·전출할 수 없습니다.")
        else:
            if not isinstance(new,str):
                raise HTTPException(422,"승급 대상 학생에게는 새 학년·반·번호가 필요합니다.")
            validate_label(new)
            if new in seen_new:
                raise HTTPException(422,"새 학년도 학년·반·번호가 중복됐습니다: "+new)
            seen_new.add(new)
            action="PROMOTE"
            target=new
        plans.append({"student_uid":uid,"old_student_id":old,
                      "new_student_id":target,"action":action})
    omitted=[item for uid,item in roster.items() if uid not in seen_uid]
    if omitted:
        errors.append("현재 학생 "+str(len(omitted))+"명의 처리 정보가 누락됐습니다. 승급·졸업·전출을 명시해 주세요.")
    plans.sort(key=lambda item:item["student_uid"])
    counts={action:sum(row["action"]==action for row in plans)
            for action in ("PROMOTE","GRADUATE","LEAVE")}
    snapshot={
        "from_year":current,"to_year":year,"entries":plans,
        "roster":[{"student_uid":row["student_uid"],
                   "student_id":row["student_id"],
                   "active_loan":row["active_loan"]}
                  for row in sorted(roster.values(),key=lambda r:r["student_uid"])],
    }
    token=hashlib.sha256(json.dumps(snapshot,ensure_ascii=True,sort_keys=True).encode()).hexdigest()
    return {"from_year":current,"to_year":year,"counts":counts,
            "roster_count":len(roster),"omitted":[x["student_id"] for x in omitted][:50],
            "errors":errors,"preview_token":token,"normalized_entries":plans}


def preview_transition(c,year,entries):
    return _canonical_transition(c,year,entries)


def apply_transition(c,year,entries,preview_token,actor,timestamp):
    result=_canonical_transition(c,year,entries)
    if result["errors"]:
        raise HTTPException(409,"학년도 변경 불가: "+"; ".join(result["errors"])[:1400])
    if not isinstance(preview_token,str) or not secrets.compare_digest(
            result["preview_token"],preview_token):
        raise HTTPException(409,"미리보기 이후 학생/대여 데이터가 변경됐습니다. 다시 검토해 주세요.")
    counts=result["counts"]
    cur=c.execute(
        """INSERT INTO academic_transitions(
             from_year,to_year,actor,completed_at,transferred,graduated,left_school)
           VALUES(?,?,?,?,?,?,?)""",
        (result["from_year"],year,actor,timestamp,
         counts["PROMOTE"],counts["GRADUATE"],counts["LEAVE"]))
    transition_id=cur.lastrowid
    # The legacy unique index on umbrellas.borrower is still in place.
    # Use temporary stable labels to safely handle year-to-year slot swaps.
    for entry in result["normalized_entries"]:
        if entry["action"]=="PROMOTE":
            c.execute(
                """UPDATE umbrellas SET borrower=? WHERE borrower_uid=?
                   AND status='BORROWED'""",
                ("IN-TRANSFER:"+entry["student_uid"],entry["student_uid"]))
    for entry in result["normalized_entries"]:
        uid=entry["student_uid"]
        target=entry["new_student_id"]
        if entry["action"]=="PROMOTE":
            c.execute(
                """INSERT INTO student_rosters(school_year,student_id,student_uid)
                   VALUES(?,?,?)""",(year,target,uid))
            c.execute("UPDATE student_accounts SET active=1 WHERE uid=?",(uid,))
            c.execute(
                """UPDATE umbrellas SET borrower=? WHERE borrower_uid=?
                   AND status='BORROWED'""",(target,uid))
        else:
            c.execute("UPDATE student_accounts SET active=0 WHERE uid=?",(uid,))
        c.execute(
            """INSERT INTO academic_transition_entries(
                transition_id,student_uid,old_student_id,new_student_id,action)
               VALUES(?,?,?,?,?)""",
            (transition_id,uid,entry["old_student_id"],target,entry["action"]))
    c.execute("UPDATE policies SET value=? WHERE key='school_year'",(year,))
    return {"ok":True,"school_year":year,"transition_id":transition_id,"counts":counts}


def assign_student(c,uid,new_label,actor,reason,timestamp):
    if not isinstance(uid,str) or not UID_RE.fullmatch(uid):
        raise HTTPException(422,"영구 학생 UID 형식이 잘못됐습니다.")
    validate_label(new_label)
    if len(reason.strip())<5:
        raise HTTPException(422,"신원 확인 및 수정 사유를 5자 이상 기록해 주세요.")
    member=c.execute("SELECT uid FROM student_accounts WHERE uid=?",(uid,)).fetchone()
    if not member:
        raise HTTPException(404,"학생 UID를 찾을 수 없습니다.")
    year=school_year(c)
    occupant=c.execute(
        "SELECT student_uid FROM student_rosters WHERE school_year=? AND student_id=?",
        (year,new_label)
    ).fetchone()
    if occupant and occupant["student_uid"]!=uid:
        raise HTTPException(409,"해당 학년·반·번호에는 다른 학생이 배정되어 있습니다.")
    previous=c.execute(
        "SELECT student_id FROM student_rosters WHERE school_year=? AND student_uid=?",
        (year,uid)
    ).fetchone()
    old_label=previous["student_id"] if previous else None
    if old_label and old_label != new_label:
        c.execute("DELETE FROM student_rosters WHERE school_year=? AND student_uid=?",
                  (year,uid))
    if not previous or old_label!=new_label:
        c.execute("INSERT INTO student_rosters(school_year,student_id,student_uid) VALUES(?,?,?)",
                  (year,new_label,uid))
    c.execute("UPDATE student_accounts SET active=1 WHERE uid=?",(uid,))
    c.execute(
        """UPDATE umbrellas SET borrower=? WHERE borrower_uid=?
           AND status='BORROWED'""",(new_label,uid))
    c.execute("""INSERT INTO academic_assignment_log(
                 school_year,student_uid,old_student_id,new_student_id,actor,reason,created_at)
                 VALUES(?,?,?,?,?,?,?)""",
              (year,uid,old_label,new_label,actor,reason.strip(),timestamp))
    return {"ok":True,"student_uid":uid,"student_id":new_label,
            "school_year":year,"previous_student_id":old_label}
