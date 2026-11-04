"""Rental due dates, overdue listing, extension and legacy migration tests."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["ADMIN_USERNAME"] = "test-teacher"
os.environ["ADMIN_PASSWORD"] = "test-admin-strong-password-123"
os.environ["SECURE_COOKIES"] = "false"
os.environ["APP_ORIGIN"] = ""
os.environ["DATA_DIR"] = "/tmp/keisung-umbrella-rental-tests"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
import server

ORIGIN={"origin":"http://localhost"}
STUDENT={"grade":6,"classroom":4,"number":16}
PIN="482739"


@pytest.fixture()
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(server,"DB_PATH",tmp_path/"umbrellas.db")
    server.init_db()
    with TestClient(server.app,base_url="http://localhost") as c:
        yield c


def post(c,path,body,headers=ORIGIN):
    return c.post(path,json=body,headers=headers)


def login(c):
    response=post(c,"/api/admin/login",{
        "username":"test-teacher","password":"test-admin-strong-password-123"
    })
    assert response.status_code==200,response.text
    return {**ORIGIN,"x-csrf-token":response.json()["csrf"]}


def test_loan_due_dates_overdue_extension_history_and_return(client):
    assert client.get("/api/admin/rentals").status_code==401
    assert client.get("/api/admin/rental-policy").status_code==401
    auth=login(client)
    assert client.get("/api/admin/rental-policy").json()["rental_days"]==7
    assert client.put("/api/admin/rental-policy",json={"rental_days":14},headers=ORIGIN).status_code==403
    assert client.put("/api/admin/rental-policy",json={"rental_days":31},headers=auth).status_code==422
    assert client.put("/api/admin/rental-policy",json={"rental_days":14},headers=auth).status_code==200
    assert post(client,"/api/admin/umbrellas",{},auth).status_code==200

    registered=post(client,"/api/student-pin/enroll",{
        **STUDENT,"pin":PIN,"pin_confirm":PIN
    })
    assert registered.status_code==200,registered.text
    start=datetime.now(timezone.utc)
    borrowed=post(client,"/api/borrow",{
        **STUDENT,"pin":PIN,"umbrella_id":"UMB-0001"
    })
    assert borrowed.status_code==200,borrowed.text
    due=datetime.fromisoformat(borrowed.json()["due_at"])
    assert start+timedelta(days=13,hours=23)<=due<=datetime.now(timezone.utc)+timedelta(days=14,minutes=1)
    info=client.get("/api/admin/rentals").json()
    assert info["total"]==1 and info["overdue_count"]==0
    assert info["rentals"][0]["due_at"]==borrowed.json()["due_at"]
    rental_id=info["rentals"][0]["rental_id"]
    record=client.get("/api/admin/student-record?student_id=6-4-16").json()
    assert record["active_due_at"]==borrowed.json()["due_at"]
    assert record["active_overdue"] is False

    old_due=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
    with server.transaction() as conn:
        conn.execute("UPDATE rental_terms SET due_at=? WHERE rental_id=?",(old_due,rental_id))
    overdue=client.get("/api/admin/rentals").json()
    assert overdue["overdue_count"]==1 and overdue["rentals"][0]["overdue"] is True
    assert client.get("/api/admin/student-record?student_id=6-4-16").json()["active_overdue"] is True

    endpoint=f"/api/admin/rentals/{rental_id}/extend"
    assert post(client,endpoint,{"extra_days":2,"reason":"학생 사정 확인"},ORIGIN).status_code==403
    assert post(client,endpoint,{"extra_days":0,"reason":"학생 사정 확인"},auth).status_code==422
    assert post(client,endpoint,{"extra_days":2,"reason":"짧음"},auth).status_code==422
    extended=post(client,endpoint,{"extra_days":2,"reason":"학생의 사정을 확인함"},auth)
    assert extended.status_code==200,extended.text
    assert extended.json()["previous_due_at"]==old_due
    new_due=datetime.fromisoformat(extended.json()["due_at"])
    assert datetime.now(timezone.utc)+timedelta(days=1,hours=23)<new_due
    assert client.get("/api/admin/rentals").json()["overdue_count"]==0
    with server.connect() as conn:
        logs=conn.execute(
            "SELECT * FROM rental_extensions WHERE rental_id=?",(rental_id,)
        ).fetchall()
    assert len(logs)==1
    assert logs[0]["previous_due_at"]==old_due

    returned=post(client,"/api/return",{
        **STUDENT,"pin":PIN,"umbrella_id":"UMB-0001"
    })
    assert returned.status_code==200
    assert client.get("/api/admin/rentals").json()["total"]==0
    assert post(client,endpoint,{"extra_days":2,"reason":"사후 연장 시도 차단"},auth).status_code==409
    backup=client.get("/api/admin/export").json()
    assert backup["schema_version"]=="3.4"
    assert len(backup["rental_terms"])==1
    assert backup["rental_terms"][0]["returned_at"]
    assert len(backup["rental_extensions"])==1
    assert backup["policies"]


def test_old_active_rentals_grace_period_and_inventory_not_reset(client):
    auth=login(client)
    assert post(client,"/api/admin/umbrellas",{},auth).status_code==200
    old_rental="f"*32
    old_timestamp=(datetime.now(timezone.utc)-timedelta(days=180)).isoformat()
    with server.transaction() as conn:
        conn.execute(
            "UPDATE umbrellas SET status='BORROWED',borrower='6-4-16',rental_id=? WHERE id=1",
            (old_rental,)
        )
        conn.execute(
            """INSERT INTO events(umbrella_id,action,student_id,rental_id,actor,reason,created_at)
               VALUES(1,'BORROW','6-4-16',?,'self-service','',?)""",
            (old_rental,old_timestamp)
        )
    server.init_db()
    # A migration should preserve the borrow record but apply a NEW grace period.
    auth=login(client)  # init_db invalidates old sessions
    rental=client.get("/api/admin/rentals").json()
    assert rental["total"]==1 and rental["overdue_count"]==0
    assert rental["rentals"][0]["borrowed_at"]==old_timestamp
    assert datetime.fromisoformat(rental["rentals"][0]["due_at"])>datetime.now(timezone.utc)+timedelta(days=6)
    assert client.get("/api/admin/inventory").json()[0]["status"]=="BORROWED"
    assert client.get("/api/admin/inventory").json()[0]["borrower"]=="6-4-16"
    server.init_db()
    auth=login(client)
    assert client.get("/api/admin/rentals").json()["total"]==1
    with server.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rental_terms WHERE rental_id=?",
                            (old_rental,)).fetchone()[0]==1


def test_admin_lost_state_closes_open_loan_but_never_auto_penalizes(client):
    auth=login(client)
    assert post(client,"/api/admin/umbrellas",{},auth).status_code==200
    issued=post(client,"/api/admin/student-pin",{
        "student_id":"6-4-16","verified_identity":True
    },auth)
    pin=issued.json()["pin"]
    borrow=post(client,"/api/borrow",{**STUDENT,"pin":pin,"umbrella_id":"UMB-0001"})
    assert borrow.status_code==200
    marked=client.patch("/api/admin/umbrellas/UMB-0001",headers=auth,json={
        "status":"LOST","reason":"현장 사실 확인"
    })
    assert marked.status_code==200
    assert client.get("/api/admin/rentals").json()["total"]==0
    with server.connect() as conn:
        terms=conn.execute("SELECT * FROM rental_terms").fetchone()
        penalties=conn.execute("SELECT COUNT(*) FROM penalty_actions").fetchone()[0]
    assert terms["returned_at"] is not None
    assert penalties==0
