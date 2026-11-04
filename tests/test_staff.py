"""Regression tests for separate teacher/staff umbrella lending and PIN security."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["ADMIN_USERNAME"] = "test-teacher"
os.environ["ADMIN_PASSWORD"] = "test-admin-strong-password-123"
os.environ["SECURE_COOKIES"] = "false"
os.environ["APP_ORIGIN"] = ""
os.environ["DATA_DIR"] = "/tmp/keisung-umbrella-staff-tests"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
import server

ORIGIN = {"origin": "http://localhost"}
STAFF_ID = "TEACH01"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "umbrellas.db")
    server.init_db()
    with TestClient(server.app, base_url="http://localhost") as c:
        yield c


def post(client, path, body, headers=ORIGIN):
    return client.post(path, json=body, headers=headers)


def login(client):
    r = post(client, "/api/admin/login", {
        "username": "test-teacher", "password": "test-admin-strong-password-123"
    })
    assert r.status_code == 200, r.text
    return {**ORIGIN, "x-csrf-token": r.json()["csrf"]}


def issue_staff(client, headers, staff_id=STAFF_ID, display_name="컴퓨터 교사"):
    r = post(client, "/api/admin/staff", {
        "staff_id": staff_id, "display_name": display_name, "verified_identity": True
    }, headers)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["staff_id"] == staff_id
    assert len(r.json()["pin"]) == 6 and r.json()["pin"].isdigit()
    return r.json()["pin"]


def account_payload(pin, staff_id=STAFF_ID, umbrella="UMB-0001"):
    return {"staff_id": staff_id, "pin": pin, "umbrella_id": umbrella}


def test_staff_borrow_return_with_due_date_and_student_quota_separation(client):
    auth = login(client)
    pin = issue_staff(client, auth)
    accounts = client.get("/api/admin/staff")
    assert accounts.status_code == 200
    assert len(accounts.json()["staff"]) == 1
    assert accounts.json()["staff"][0]["active"] is True
    assert "pin" not in accounts.text.lower() or '"pin":' not in accounts.text
    assert pin not in accounts.text
    assert "pin_hash" not in accounts.text
    assert post(client, "/api/admin/umbrellas/batch", {"count": 3}, auth).status_code == 200

    # Student monthly limit 1 does not constrain the separate staff account.
    assert client.put("/api/admin/policy", json={"monthly_limit": 1}, headers=auth).status_code == 200
    first = post(client, "/api/staff/borrow", account_payload(pin))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "BORROWED"
    due_at = datetime.fromisoformat(first.json()["due_at"])
    assert datetime.now(timezone.utc) + timedelta(days=6) < due_at
    assert datetime.now(timezone.utc) + timedelta(days=8) > due_at
    assert post(client, "/api/staff/borrow",
                account_payload(pin, umbrella="UMB-0002")).status_code == 409
    assert post(client, "/api/return", {
        "grade": 6, "classroom": 4, "number": 16,
        "pin": "482739", "umbrella_id": "UMB-0001"
    }).status_code == 401

    inventory = {x["id"]: x for x in client.get("/api/admin/inventory").json()}
    assert inventory["UMB-0001"]["borrower"] == "STAFF:TEACH01"
    rental = client.get("/api/admin/rentals").json()
    assert rental["total"] == 1
    assert rental["rentals"][0]["student_id"] == "STAFF:TEACH01"
    assert rental["rentals"][0]["due_at"] == first.json()["due_at"]
    with server.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE student_id='6-4-16' AND action='BORROW'"
        ).fetchone()[0]
        staff_event = conn.execute(
            "SELECT * FROM events WHERE student_id='STAFF:TEACH01' AND action='BORROW'"
        ).fetchone()
    assert count == 0
    assert staff_event is not None

    mismatch = post(client, "/api/staff/return", account_payload(pin, umbrella="UMB-0002"))
    assert mismatch.status_code == 409
    returned = post(client, "/api/staff/return", account_payload(pin))
    assert returned.status_code == 200, returned.text
    assert client.get("/api/admin/rentals").json()["total"] == 0
    assert post(client, "/api/staff/return", account_payload(pin)).status_code == 409
    backup = client.get("/api/admin/export").json()
    assert backup["schema_version"] == "3.4"
    assert len(backup["staff_accounts"]) == 1
    assert len(backup["staff_account_events"]) == 1
    assert pin not in str(backup)
    assert "salt" not in str(backup)
    assert "pin_hash" not in str(backup)
    assert backup["rental_terms"][0]["returned_at"] is not None


def test_staff_admin_authorization_input_validation_and_password_reset(client):
    assert client.get("/api/admin/staff").status_code == 401
    assert post(client, "/api/admin/staff", {
        "staff_id": STAFF_ID, "display_name": "컴퓨터 교사",
        "verified_identity": True
    }).status_code == 401
    auth = login(client)
    assert post(client, "/api/admin/staff", {
        "staff_id": STAFF_ID, "display_name": "컴퓨터 교사",
        "verified_identity": True
    }, ORIGIN).status_code == 403
    assert post(client, "/api/admin/staff", {
        "staff_id": STAFF_ID, "display_name": "컴퓨터 교사",
        "verified_identity": False
    }, auth).status_code == 422
    assert post(client, "/api/admin/staff", {
        "staff_id": "ab", "display_name": "컴퓨터 교사",
        "verified_identity": True
    }, auth).status_code == 422
    pin = issue_staff(client, auth)
    assert post(client, "/api/admin/staff", {
        "staff_id": STAFF_ID, "display_name": "다른 이름",
        "verified_identity": True
    }, auth).status_code == 409
    assert post(client, f"/api/admin/staff/{STAFF_ID}/pin-reset", {
        "verified_identity": False
    }, auth).status_code == 422
    assert post(client, f"/api/admin/staff/{STAFF_ID}/pin-reset", {
        "verified_identity": True
    }).status_code == 403

    new = post(client, f"/api/admin/staff/{STAFF_ID}/pin-reset", {
        "verified_identity": True
    }, auth)
    assert new.status_code == 200
    replacement = new.json()["pin"]
    assert replacement != pin or len(pin) == 6  # random new PIN can rarely match
    with server.connect() as conn:
        row = conn.execute(
            "SELECT pin_hash,salt FROM staff_accounts WHERE staff_id=?",
            (STAFF_ID,)
        ).fetchone()
        assert row["pin_hash"] == server.student_pin_hash(replacement, row["salt"])
    assert client.put(f"/api/admin/staff/{STAFF_ID}/status", json={
        "active": False
    }, headers=ORIGIN).status_code == 403
    with server.connect() as conn:
        events = conn.execute(
            "SELECT action FROM staff_account_events WHERE staff_id=? ORDER BY id",
            (STAFF_ID,)
        ).fetchall()
    assert [r["action"] for r in events] == ["CREATED", "PIN_RESET"]


def test_staff_wrong_pin_lockout_disable_return_and_reenable(client):
    auth = login(client)
    pin = issue_staff(client, auth)
    post(client, "/api/admin/umbrellas/batch", {"count": 2}, auth)

    no_origin = client.post("/api/staff/borrow", json=account_payload(pin))
    assert no_origin.status_code == 403
    malformed = post(client, "/api/staff/borrow", account_payload("1x3456"))
    assert malformed.status_code == 422
    malformed_id = post(client, "/api/staff/borrow", account_payload(pin, staff_id="ab"))
    assert malformed_id.status_code == 422
    bad = "999999" if pin != "999999" else "888888"
    for n in range(1,6):
        res = post(client, "/api/staff/borrow", account_payload(bad))
        assert res.status_code == (429 if n == 5 else 401), res.text
    assert post(client, "/api/staff/borrow", account_payload(pin)).status_code == 429
    with server.transaction() as conn:
        conn.execute(
            "UPDATE staff_accounts SET locked_until=0,failed_attempts=0,last_failed_at=0 WHERE staff_id=?",
            (STAFF_ID,)
        )

    borrowed = post(client, "/api/staff/borrow", account_payload(pin))
    assert borrowed.status_code == 200, borrowed.text
    disable = client.put(f"/api/admin/staff/{STAFF_ID}/status", headers=auth,
                         json={"active": False})
    assert disable.status_code == 200
    assert post(client, "/api/staff/borrow",
                account_payload(pin, umbrella="UMB-0002")).status_code == 403
    assert post(client, "/api/staff/return", account_payload(pin)).status_code == 200
    assert client.put(f"/api/admin/staff/{STAFF_ID}/status", headers=auth,
                      json={"active": True}).status_code == 200
    assert post(client, "/api/staff/borrow",
                account_payload(pin, umbrella="UMB-0002")).status_code == 200
    assert client.get("/api/admin/stocktakes").status_code == 200


def test_staff_lending_coexists_with_student_borrow_and_audit(client):
    auth = login(client)
    pin = issue_staff(client, auth)
    post(client, "/api/admin/umbrellas/batch", {"count": 2}, auth)
    student = {"grade": 6, "classroom": 4, "number": 16}
    student_pin = "482739"
    assert post(client, "/api/student-pin/enroll", {
        **student, "pin": student_pin, "pin_confirm": student_pin
    }).status_code == 200
    staff_res = post(client, "/api/staff/borrow", account_payload(pin))
    assert staff_res.status_code == 200
    student_res = post(client, "/api/borrow", {
        **student, "pin": student_pin, "umbrella_id": "UMB-0002"
    })
    assert student_res.status_code == 200
    assert post(client, "/api/staff/return",
                account_payload(pin, umbrella="UMB-0002")).status_code == 403
    assert post(client, "/api/return", {
        **student, "pin": student_pin, "umbrella_id": "UMB-0001"
    }).status_code == 403
    assert post(client, "/api/staff/return", account_payload(pin)).status_code == 200
    assert post(client, "/api/return", {
        **student, "pin": student_pin, "umbrella_id": "UMB-0002"
    }).status_code == 200
    assert client.get("/api/admin/rentals").json()["total"] == 0
