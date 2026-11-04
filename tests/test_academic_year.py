"""Regression tests for permanent student identity and annual homeroom rollover."""
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ["ADMIN_USERNAME"]="test-teacher"
os.environ["ADMIN_PASSWORD"]="test-admin-strong-password-123"
os.environ["SECURE_COOKIES"]="false"
os.environ["APP_ORIGIN"]=""
os.environ["DATA_DIR"]="/tmp/keisung-umbrella-academic-year-tests"
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
import server
import school_year as academic

ORIGIN={"origin":"http://localhost"}
A="5-2-14"
B="6-4-16"
C="4-1-10"
PIN_A="482739"
PIN_B="583492"
PIN_C="739251"


@pytest.fixture()
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(server,"DB_PATH",tmp_path/"umbrellas.db")
    server.init_db()
    with TestClient(server.app,base_url="http://localhost") as c:
        yield c


def post(c,path,body,headers=ORIGIN):
    return c.post(path,json=body,headers=headers)


def login(c):
    result=post(c,"/api/admin/login",{
        "username":"test-teacher","password":"test-admin-strong-password-123"})
    assert result.status_code==200,result.text
    return {**ORIGIN,"x-csrf-token":result.json()["csrf"]}


def parts(label):
    g,k,n=map(int,label.split("-"))
    return {"grade":g,"classroom":k,"number":n}


def enroll(c,label,pin):
    result=post(c,"/api/student-pin/enroll",{
        **parts(label),"pin":pin,"pin_confirm":pin})
    assert result.status_code==200,result.text


def year_info(c):
    response=c.get("/api/admin/academic-year")
    assert response.status_code==200,response.text
    return response.json()


def entry(row,to):
    return {"student_uid":row["student_uid"],"old_student_id":row["student_id"],
            "new_student_id":to}


def make_plan(c,by_label):
    info=year_info(c)
    return info,[entry(row,by_label[row["student_id"]]) for row in info["roster"]]


def preview(c,headers,year,rows):
    return post(c,"/api/admin/academic-year/preview",{"to_year":year,"entries":rows},headers)


def apply(c,headers,year,rows,token,confirmed=True):
    return post(c,"/api/admin/academic-year/apply",{
        "to_year":year,"entries":rows,"preview_token":token,"confirmed":confirmed},headers)


def test_permanent_uid_preserves_pin_penalty_quota_and_outstanding_return(client):
    auth=login(client)
    enroll(client,A,PIN_A)
    enroll(client,B,PIN_B)
    enroll(client,C,PIN_C)
    assert post(client,"/api/admin/umbrellas/batch",{"count":2},auth).status_code==200
    borrow=post(client,"/api/borrow",{
        **parts(A),"pin":PIN_A,"umbrella_id":"UMB-0001"})
    assert borrow.status_code==200,borrow.text
    assert client.put("/api/admin/student-allowance",headers=auth,json={
        "student_id":A,"extra":3
    }).status_code==200
    penalty=post(client,"/api/admin/penalties",{
        "student_id":A,"code":"WARNING","reason":"대여 규칙 안내 기록",
        "confirmed":True},auth)
    assert penalty.status_code==200,penalty.text
    info,rows=make_plan(client,{A:"6-1-7",B:"GRADUATE",C:"5-1-4"})
    assert len(rows)==3
    prior_uid=next(r["student_uid"] for r in info["roster"] if r["student_id"]==A)
    assert prior_uid.startswith("STU-")
    year=info["next_year"]
    assert client.get("/api/admin/academic-year").json()["school_year"]==year-1
    planned=preview(client,auth,year,rows)
    assert planned.status_code==200,planned.text
    data=planned.json()
    assert not data["errors"]
    assert data["counts"]=={"PROMOTE":2,"GRADUATE":1,"LEAVE":0}

    # Mutating the roster requires an admin session, CSRF and explicit confirmation.
    assert apply(client,ORIGIN,year,rows,data["preview_token"]).status_code==403
    assert apply(client,auth,year,rows,data["preview_token"],False).status_code==422
    applied=apply(client,auth,year,rows,data["preview_token"])
    assert applied.status_code==200,applied.text
    assert applied.json()["school_year"]==year
    assert year_info(client)["school_year"]==year
    assert len(year_info(client)["roster"])==2
    assert post(client,"/api/student-pin/status",parts("6-1-7")).json()=={"has_pin":True}
    assert post(client,"/api/student-pin/status",parts(A)).json()=={"has_pin":False}
    assert post(client,"/api/student-pin/status",parts(B)).json()=={"has_pin":False}
    record=client.get("/api/admin/student-record?student_id=6-1-7").json()
    assert record["student_uid"]==prior_uid
    assert record["has_pin"]
    assert record["active_umbrella"]=="UMB-0001"
    assert record["usage"]["used"]==1
    assert record["usage"]["extra"]==3
    assert record["penalty_points"]==0
    assert record["penalties"][0]["reason"]=="대여 규칙 안내 기록"
    inventory=client.get("/api/admin/inventory").json()
    first=next(item for item in inventory if item["id"]=="UMB-0001")
    assert first["borrower"]=="6-1-7"

    returned=post(client,"/api/return",{
        **parts("6-1-7"),"pin":PIN_A,"umbrella_id":"UMB-0001"})
    assert returned.status_code==200,returned.text
    with server.connect() as conn:
        events=conn.execute(
            """SELECT action,student_id,student_uid FROM events
               WHERE action IN ('BORROW','RETURN') ORDER BY seq""").fetchall()
        terms=conn.execute(
            "SELECT student_id,student_uid,returned_at FROM rental_terms"
        ).fetchone()
        pinned=conn.execute(
            "SELECT COUNT(*) FROM student_credentials WHERE student_uid=?",
            (prior_uid,)).fetchone()[0]
    assert [(e["action"],e["student_id"]) for e in events]==[
        ("BORROW",A),("RETURN","6-1-7")]
    assert all(e["student_uid"]==prior_uid for e in events)
    assert terms["student_id"]==A
    assert terms["student_uid"]==prior_uid
    assert terms["returned_at"]
    assert pinned==1

    # Old grade/class/number may be reused by a DIFFERENT student next year.
    enroll(client,A,"915286")
    new_roster=year_info(client)["roster"]
    new_uid=next(r["student_uid"] for r in new_roster if r["student_id"]==A)
    assert new_uid!=prior_uid
    assert post(client,"/api/admin/umbrellas",{},auth).status_code==200
    assert post(client,"/api/borrow",{
        **parts(A),"pin":PIN_A,"umbrella_id":"UMB-0003"}).status_code==401
    assert post(client,"/api/borrow",{
        **parts(A),"pin":"915286","umbrella_id":"UMB-0003"}).status_code==200
    server.init_db()  # No old PIN silently attaches to reused class number on restart.
    assert post(client,"/api/student-pin/status",parts(A)).json()=={"has_pin":True}
    with server.connect() as conn:
        credentials=conn.execute(
            "SELECT COUNT(*) FROM student_credentials WHERE student_uid=?",
            (new_uid,)).fetchone()[0]
    assert credentials==1


def test_conflicts_omissions_graduation_with_loan_and_stale_preview_blocked(client):
    auth=login(client)
    enroll(client,A,PIN_A)
    enroll(client,B,PIN_B)
    post(client,"/api/admin/umbrellas",{},auth)
    assert post(client,"/api/borrow",{
        **parts(B),"pin":PIN_B,"umbrella_id":"UMB-0001"}).status_code==200
    original,rows=make_plan(client,{A:"6-1-7",B:"GRADUATE"})
    year=original["next_year"]
    assert preview(client,auth,year,[rows[0]]).json()["errors"]
    incomplete=preview(client,auth,year,[rows[0]])
    assert apply(client,auth,year,[rows[0]],
                 incomplete.json()["preview_token"]).status_code==409
    blocked=preview(client,auth,year,rows)
    assert blocked.status_code==200
    assert any("대여 중" in e for e in blocked.json()["errors"])
    assert apply(client,auth,year,rows,blocked.json()["preview_token"]).status_code==409
    assert year_info(client)["school_year"]==original["school_year"]

    duplicated=[entry(r,"6-1-7") for r in original["roster"]]
    dup=preview(client,auth,year,duplicated)
    assert dup.status_code==422

    # Existing loan can be returned, then a new preview is required.
    assert post(client,"/api/return",{
        **parts(B),"pin":PIN_B,"umbrella_id":"UMB-0001"}).status_code==200
    ready=preview(client,auth,year,rows)
    assert not ready.json()["errors"]
    assert apply(client,auth,year,rows,"0"*64).status_code==409
    # Another account change after preview invalidates its roster snapshot.
    enroll(client,"3-1-9","864253")
    changed=apply(client,auth,year,rows,ready.json()["preview_token"])
    assert changed.status_code==409
    assert year_info(client)["school_year"]==original["school_year"]


def test_individual_assignment_conflict_and_historical_identity(client):
    auth=login(client)
    enroll(client,A,PIN_A)
    enroll(client,B,PIN_B)
    info,rows=make_plan(client,{A:"6-1-7",B:"GRADUATE"})
    y=info["next_year"]
    p=preview(client,auth,y,rows).json()
    assert not p["errors"]
    assert apply(client,auth,y,rows,p["preview_token"]).status_code==200
    uid_a=next(r["student_uid"] for r in rows if r["old_student_id"]==A)
    uid_b=next(r["student_uid"] for r in rows if r["old_student_id"]==B)
    endpoint="/api/admin/academic-year/assign"
    missing_identity=post(client,endpoint,{
        "student_uid":uid_a,"new_student_id":"6-1-8",
        "reason":"학교 공식 반 편성표 정정","verified_identity":False},auth)
    assert missing_identity.status_code==422
    assert post(client,endpoint,{
        "student_uid":uid_a,"new_student_id":"6-1-8",
        "reason":"학교 공식 반 편성표 정정","verified_identity":True},ORIGIN).status_code==403

    first=post(client,endpoint,{
        "student_uid":uid_a,"new_student_id":"6-1-8",
        "reason":"학교 공식 반 편성표 정정","verified_identity":True},auth)
    assert first.status_code==200,first.text
    assert post(client,"/api/student-pin/status",parts("6-1-8")).json()["has_pin"]
    assert not post(client,"/api/student-pin/status",parts("6-1-7")).json()["has_pin"]
    conflict=post(client,endpoint,{
        "student_uid":uid_b,"new_student_id":"6-1-8",
        "reason":"학급 배정 충돌 테스트","verified_identity":True},auth)
    assert conflict.status_code==409
    # A graduated student may be restored with explicit administrator verification.
    restored=post(client,endpoint,{
        "student_uid":uid_b,"new_student_id":"6-3-7",
        "reason":"명단 오류 및 학생 신원 확인","verified_identity":True},auth)
    assert restored.status_code==200
    assert post(client,"/api/student-pin/status",parts("6-3-7")).json()["has_pin"]
    rec=client.get("/api/admin/student-record?student_id=6-3-7").json()
    assert rec["student_uid"]==uid_b
    with server.connect() as conn:
        logs=conn.execute("SELECT * FROM academic_assignment_log ORDER BY id").fetchall()
        assert len(logs)==2
        assert logs[0]["old_student_id"]=="6-1-7"
        assert logs[1]["old_student_id"] is None


def test_old_schema_import_preserves_pin_hash_and_history_snapshots(client):
    auth=login(client)
    assert post(client,"/api/admin/umbrellas",{},auth).status_code==200
    salt=secrets.token_bytes(16)
    digest=server.student_pin_hash(PIN_A,salt)
    with server.transaction() as conn:
        conn.execute(
            """INSERT INTO student_pins(student_id,salt,pin_hash,updated_at,updated_by)
               VALUES(?,?,?,?,?)""",
            (A,salt,digest,server.now(),"self-enrollment"))
        conn.execute(
            """UPDATE umbrellas SET status='BORROWED',borrower=?,
               rental_id=? WHERE id=1""",(A,"e"*32))
        conn.execute(
            """INSERT INTO events(umbrella_id,action,student_id,rental_id,actor,reason,created_at)
               VALUES(1,'BORROW',?,?,'self-service','',?)""",
            (A,"e"*32,server.now()))
        conn.execute(
            """INSERT INTO student_allowances(student_id,month,extra,updated_by,updated_at)
               VALUES(?,?,3,'test-teacher',?)""",
            (A,server.korean_month_window()[0],server.now()))
    server.init_db()
    auth=login(client)
    info=year_info(client)
    uid=info["roster"][0]["student_uid"]
    with server.connect() as conn:
        cred=conn.execute(
            "SELECT * FROM student_credentials WHERE student_uid=?",(uid,)
        ).fetchone()
        old=conn.execute("SELECT * FROM student_pins WHERE student_id=?",(A,)).fetchone()
        umbrella=conn.execute("SELECT * FROM umbrellas WHERE id=1").fetchone()
        event=conn.execute("SELECT student_uid,student_id FROM events WHERE action='BORROW'").fetchone()
    assert cred["salt"]==old["salt"]==salt
    assert cred["pin_hash"]==old["pin_hash"]==digest
    assert umbrella["borrower_uid"]==uid
    assert event["student_uid"]==uid and event["student_id"]==A
    assert post(client,"/api/student-pin/status",parts(A)).json()["has_pin"]
    assert client.get("/api/admin/student-record?student_id="+A).json()["usage"]["extra"]==3
    assert post(client,"/api/return",{
        **parts(A),"pin":PIN_A,"umbrella_id":"UMB-0001"}).status_code==200


def test_staff_not_in_student_roster_or_changed_by_rollover(client):
    auth=login(client)
    enroll(client,A,PIN_A)
    staff=post(client,"/api/admin/staff",{
        "staff_id":"TEACH01","display_name":"컴퓨터 교사","verified_identity":True},auth)
    assert staff.status_code==200
    pin=staff.json()["pin"]
    assert post(client,"/api/admin/umbrellas",{},auth).status_code==200
    staff_borrow=post(client,"/api/staff/borrow",{
        "staff_id":"TEACH01","pin":pin,"umbrella_id":"UMB-0001"})
    assert staff_borrow.status_code==200
    info,rows=make_plan(client,{A:"6-1-7"})
    assert len(info["roster"])==1
    p=preview(client,auth,info["next_year"],rows)
    assert p.status_code==200 and not p.json()["errors"]
    assert apply(client,auth,info["next_year"],rows,p.json()["preview_token"]).status_code==200
    inventory=client.get("/api/admin/inventory").json()
    assert inventory[0]["borrower"]=="STAFF:TEACH01"
    assert post(client,"/api/staff/return",{
        "staff_id":"TEACH01","pin":pin,"umbrella_id":"UMB-0001"}).status_code==200
    assert not any("STAFF" in row["student_id"] for row in year_info(client)["roster"])


def test_academic_endpoints_require_admin_and_csrf(client):
    enroll(client,A,PIN_A)
    current=login(client)
    school=year_info(client)
    entries=[entry(row,"6-1-7") for row in school["roster"]]
    year=school["next_year"]
    assert client.get("/api/admin/academic-year").status_code==200
    # Separate anonymous client, not the authenticated cookie jar.
    with TestClient(server.app,base_url="http://localhost") as anonymous:
        assert anonymous.get("/api/admin/academic-year").status_code==401
        assert post(anonymous,"/api/admin/academic-year/preview",{
            "to_year":year,"entries":entries}).status_code==401
    assert post(client,"/api/admin/academic-year/preview",{
        "to_year":year,"entries":entries}).status_code==403
    p=preview(client,current,year,entries)
    assert p.status_code==200
    assert apply(client,current,year+1,entries,p.json()["preview_token"]).status_code==409
    assert apply(client,current,year,entries,p.json()["preview_token"]).status_code==200
    # A second transition into the same year must not reapply or mutate the roster.
    assert apply(client,current,year,entries,p.json()["preview_token"]).status_code==409
