"""Student PIN regression tests after removing staff approval.

Run: pytest -q tests/test_student_pin.py
"""
import os
import sys
from pathlib import Path

os.environ["ADMIN_USERNAME"] = "test-teacher"
os.environ["ADMIN_PASSWORD"] = "test-admin-strong-password-123"
os.environ["SECURE_COOKIES"] = "false"
os.environ["APP_ORIGIN"] = ""
os.environ["DATA_DIR"] = "/tmp/keisung-umbrella-student-pin-tests"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
import server

ORIGIN = {"origin": "http://localhost"}
STUDENT = {"grade": 6, "classroom": 4, "number": 16}
PIN = "482739"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "umbrellas.db")
    server.init_db()
    with TestClient(server.app, base_url="http://localhost") as c:
        yield c


def post(client, url, body, headers=None):
    return client.post(url, json=body, headers=headers or ORIGIN)


def login(client):
    result = post(client, "/api/admin/login", {
        "username": "test-teacher",
        "password": "test-admin-strong-password-123",
    })
    assert result.status_code == 200, result.text
    return {**ORIGIN, "x-csrf-token": result.json()["csrf"]}


def test_immediate_pin_enrollment_borrow_return_and_limits(client):
    status = post(client, "/api/student-pin/status", STUDENT)
    assert status.status_code == 200 and status.json() == {"has_pin": False}
    weak = post(client, "/api/student-pin/enroll", {
        **STUDENT, "pin": "111111", "pin_confirm": "111111",
    })
    assert weak.status_code == 422
    mismatched = post(client, "/api/student-pin/enroll", {
        **STUDENT, "pin": PIN, "pin_confirm": "583492",
    })
    assert mismatched.status_code == 422

    registered = post(client, "/api/student-pin/enroll", {
        **STUDENT, "pin": PIN, "pin_confirm": PIN,
    })
    assert registered.status_code == 200, registered.text
    assert registered.json() == {"ok": True, "student_id": "6-4-16"}
    assert post(client, "/api/student-pin/status", STUDENT).json() == {"has_pin": True}
    duplicate = post(client, "/api/student-pin/enroll", {
        **STUDENT, "pin": "928345", "pin_confirm": "928345",
    })
    assert duplicate.status_code == 409
    assert client.get("/api/admin/stocktakes").status_code == 401
    auth = login(client)
    assert post(client, "/api/admin/umbrellas", {}, auth).status_code == 200
    bad_borrow = post(client, "/api/borrow", {
        **STUDENT, "umbrella_id": "UMB-0001", "pin": "999999",
    })
    assert bad_borrow.status_code == 401
    checked_out = post(client, "/api/borrow", {
        **STUDENT, "umbrella_id": "UMB-0001", "pin": PIN,
    })
    assert checked_out.status_code == 200, checked_out.text
    assert checked_out.json()["status"] == "BORROWED"
    assert checked_out.json()["due_at"]
    assert post(client, "/api/borrow", {
        **STUDENT, "umbrella_id": "UMB-0001", "pin": PIN,
    }).status_code == 409
    checked_in = post(client, "/api/return", {
        **STUDENT, "umbrella_id": "UMB-0001", "pin": PIN,
    })
    assert checked_in.status_code == 200
    assert checked_in.json()["status"] == "AVAILABLE"


def test_admin_pin_reissue_requires_identity_confirmation_and_csrf(client):
    post(client, "/api/student-pin/enroll", {**STUDENT, "pin": PIN, "pin_confirm": PIN})
    auth = login(client)
    endpoint = "/api/admin/student-pin"
    assert post(client, endpoint, {"student_id": "6-4-16",
                                   "verified_identity": True}).status_code == 403
    assert post(client, endpoint, {"student_id": "6-4-16",
                                   "verified_identity": False}, auth).status_code == 422
    issued = post(client, endpoint, {"student_id": "6-4-16",
                                    "verified_identity": True}, auth)
    assert issued.status_code == 200
    new_pin = issued.json()["pin"]
    assert len(new_pin) == 6 and new_pin.isdigit()
    assert post(client, "/api/admin/umbrellas", {}, auth).status_code == 200
    assert post(client, "/api/borrow", {
        **STUDENT, "umbrella_id": "UMB-0001", "pin": PIN,
    }).status_code == 401
    assert post(client, "/api/borrow", {
        **STUDENT, "umbrella_id": "UMB-0001", "pin": new_pin,
    }).status_code == 200


def test_old_pending_and_rejected_approvals_no_longer_block_existing_pin(client):
    """Legacy approval metadata is ignored, but credentials remain intact."""
    import secrets
    users=[("6-4-16","PENDING"),("5-2-14","REJECTED")]
    with server.transaction() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_pin_approvals (
                student_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                approval_code_hash TEXT,
                requested_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by TEXT,
                review_reason TEXT
            );
        """)
        for student, old_state in users:
            salt=secrets.token_bytes(16)
            conn.execute(
                """INSERT INTO student_pins(student_id,salt,pin_hash,updated_at,updated_by)
                   VALUES(?,?,?,?,?)""",
                (student,salt,server.student_pin_hash(PIN,salt),server.now(),"self-enrollment"),
            )
            conn.execute(
                """INSERT INTO student_pin_approvals(student_id,status,requested_at)
                   VALUES(?,?,?)""", (student,old_state,server.now()),
            )
    # Re-running a DB migration keeps old accounts instead of deleting anything.
    server.init_db()
    for grades,clazz,num in [(6,4,16),(5,2,14)]:
        status=post(client, "/api/student-pin/status", {
            "grade":grades,"classroom":clazz,"number":num,
        })
        assert status.json() == {"has_pin":True}
    auth=login(client)
    assert post(client,"/api/admin/umbrellas",{},auth).status_code == 200
    assert post(client,"/api/borrow",{
        **STUDENT,"umbrella_id":"UMB-0001","pin":PIN,
    }).status_code == 200
    assert post(client,"/api/return",{
        **STUDENT,"umbrella_id":"UMB-0001","pin":PIN,
    }).status_code == 200
    export=client.get("/api/admin/export").json()
    assert "student_pin_approvals" not in export
    assert "rental_terms" in export
