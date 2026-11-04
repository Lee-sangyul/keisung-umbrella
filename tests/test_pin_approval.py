"""Regression tests for PIN approval and existing checkout/return flows.

Run: pip install -r requirements.txt && pip install pytest httpx
     pytest -q tests/test_pin_approval.py
"""
import os
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Set safe bootstrap admin values BEFORE importing server (which initializes its DB).
os.environ["ADMIN_USERNAME"] = "test-teacher"
os.environ["ADMIN_PASSWORD"] = "test-admin-strong-password-123"
os.environ["SECURE_COOKIES"] = "false"
os.environ["APP_ORIGIN"] = ""
os.environ["DATA_DIR"] = "/tmp/keisung-umbrella-pin-approval-tests"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


ORIGIN = {"origin": "http://localhost"}
STUDENT = {"grade": 6, "classroom": 4, "number": 16}
STUDENT_ID = "6-4-16"
PIN = "482739"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    """Every test gets a fresh independent temporary SQLite DB."""
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "umbrellas.db")
    server.init_db()
    with TestClient(server.app, base_url="http://localhost") as test_client:
        yield test_client


def post(client, path, data, *, headers=None):
    return client.post(path, json=data, headers=headers or ORIGIN)


def admin_headers(csrf):
    return {**ORIGIN, "x-csrf-token": csrf}


def login(client):
    result = post(
        client, "/api/admin/login",
        {"username": "test-teacher", "password": "test-admin-strong-password-123"},
    )
    assert result.status_code == 200, result.text
    return admin_headers(result.json()["csrf"])


def request_pin(client, student=STUDENT, pin=PIN):
    response = post(
        client, "/api/student-pin/enroll",
        {**student, "pin": pin, "pin_confirm": pin},
    )
    assert response.status_code == 200, response.text
    assert response.json()["approval_status"] == "PENDING"
    assert len(response.json()["approval_code"]) == 8
    return response.json()["approval_code"]


def test_pending_requires_staff_approval_and_code(client):
    current = post(client, "/api/student-pin/status", STUDENT)
    assert current.json() == {"has_pin": False, "approval_status": "NOT_REGISTERED"}
    code = request_pin(client)
    assert post(client, "/api/student-pin/status", STUDENT).json()["approval_status"] == "PENDING"
    assert post(client, "/api/student-pin/enroll", {**STUDENT, "pin": PIN, "pin_confirm": PIN}).status_code == 409
    assert client.get("/api/admin/student-pin/requests").status_code == 401
    assert post(client, "/api/borrow", {**STUDENT, "umbrella_id": "UMB-0001", "pin": PIN}).status_code == 403
    assert post(client, "/api/return", {**STUDENT, "umbrella_id": "UMB-0001", "pin": PIN}).status_code == 403

    auth = login(client)
    pending = client.get("/api/admin/student-pin/requests")
    assert pending.status_code == 200
    assert pending.json()["pending_count"] == 1
    assert pending.json()["requests"][0]["student_id"] == STUDENT_ID
    assert "approval_code" not in pending.json()["requests"][0]
    assert "pin_hash" not in pending.text

    # No CSRF, no face-to-face confirmation, or wrong applicant ticket -> deny.
    endpoint = f"/api/admin/student-pin/requests/{STUDENT_ID}/approve"
    assert post(client, endpoint, {"verified_identity": True, "approval_code": code}).status_code == 403
    assert post(client, endpoint, {"verified_identity": False, "approval_code": code},
                headers=auth).status_code == 422
    wrong_code = "ABCDEFGH" if code != "ABCDEFGH" else "HGFEDCBA"
    assert post(client, endpoint, {"verified_identity": True, "approval_code": wrong_code},
                headers=auth).status_code == 403
    assert post(client, "/api/student-pin/status", STUDENT).json()["approval_status"] == "PENDING"

    assert post(client, endpoint, {"verified_identity": True, "approval_code": code},
                headers=auth).status_code == 200
    assert post(client, endpoint, {"verified_identity": True, "approval_code": code},
                headers=auth).status_code == 409
    assert post(client, "/api/student-pin/status", STUDENT).json()["approval_status"] == "APPROVED"

    assert post(client, "/api/admin/umbrellas", {}, headers=auth).status_code == 200
    borrowed = post(client, "/api/borrow", {**STUDENT, "umbrella_id": "UMB-0001", "pin": PIN})
    assert borrowed.status_code == 200, borrowed.text
    assert borrowed.json()["status"] == "BORROWED"
    returned = post(client, "/api/return", {**STUDENT, "umbrella_id": "UMB-0001", "pin": PIN})
    assert returned.status_code == 200, returned.text
    assert returned.json()["status"] == "AVAILABLE"

    exported = client.get("/api/admin/export")
    assert exported.status_code == 200
    assert "student_pin_approvals" in exported.json()
    assert "student_pin_review_log" in exported.json()
    assert code not in exported.text


def test_rejection_blocks_checkout_but_staff_reissue_restores_access(client):
    code = request_pin(client)
    auth = login(client)
    endpoint = f"/api/admin/student-pin/requests/{STUDENT_ID}/reject"
    assert post(client, endpoint, {"reason": "bad"}, headers=auth).status_code == 422
    rejected = post(client, endpoint, {"reason": "본인 확인이 완료되지 않음"}, headers=auth)
    assert rejected.status_code == 200
    assert post(client, "/api/student-pin/status", STUDENT).json()["approval_status"] == "REJECTED"
    assert post(client, "/api/borrow", {**STUDENT, "umbrella_id": "UMB-0001", "pin": PIN}).status_code == 403
    assert post(client, "/api/student-pin/enroll", {**STUDENT, "pin": PIN, "pin_confirm": PIN}).status_code == 409
    assert client.get("/api/admin/student-pin/requests").json()["pending_count"] == 0

    reissue = post(client, "/api/admin/student-pin",
                   {"student_id": STUDENT_ID, "verified_identity": True}, headers=auth)
    assert reissue.status_code == 200
    replacement = reissue.json()["pin"]
    assert len(replacement) == 6 and replacement.isdigit()
    assert post(client, "/api/student-pin/status", STUDENT).json()["approval_status"] == "APPROVED"
    assert post(client, "/api/admin/umbrellas", {}, headers=auth).status_code == 200
    assert post(client, "/api/borrow", {**STUDENT, "umbrella_id": "UMB-0001", "pin": PIN}).status_code == 401
    assert post(client, "/api/borrow", {**STUDENT, "umbrella_id": "UMB-0001", "pin": replacement}).status_code == 200


def test_legacy_self_enrollment_is_pending_and_staff_issued_pin_remains_valid(client):
    teacher_student = "5-2-14"
    salt1, salt2 = secrets.token_bytes(16), secrets.token_bytes(16)
    with server.transaction() as conn:
        conn.execute(
            """INSERT INTO student_pins(student_id,salt,pin_hash,updated_at,updated_by)
               VALUES(?,?,?,?,?)""",
            (STUDENT_ID, salt1, server.student_pin_hash(PIN,salt1),
             "2026-10-01T00:00:00+00:00","self-enrollment"),
        )
        conn.execute(
            """INSERT INTO student_pins(student_id,salt,pin_hash,updated_at,updated_by)
               VALUES(?,?,?,?,?)""",
            (teacher_student,salt2,server.student_pin_hash("572934",salt2),
             "2026-10-01T00:00:00+00:00","test-teacher"),
        )
    server.init_db()  # Same migration path as a deployed existing DB.
    assert post(client,"/api/student-pin/status",STUDENT).json()["approval_status"] == "PENDING"
    assert post(client,"/api/student-pin/status",
                {"grade":5,"classroom":2,"number":14}).json()["approval_status"] == "APPROVED"

    auth = login(client)
    pending = client.get("/api/admin/student-pin/requests").json()["requests"]
    assert len(pending) == 1
    assert pending[0]["has_approval_code"] == 0
    assert post(client, f"/api/admin/student-pin/requests/{STUDENT_ID}/approve",
                {"verified_identity": True, "approval_code": "ABCDEFGH"},headers=auth).status_code == 409

    # Staff-issued legacy account still checks out with its original PIN.
    assert post(client, "/api/admin/umbrellas", {},headers=auth).status_code == 200
    borrower={"grade":5,"classroom":2,"number":14,"umbrella_id":"UMB-0001","pin":"572934"}
    assert post(client,"/api/borrow",borrower).status_code == 200
    assert post(client,"/api/return",borrower).status_code == 200

    # Migrated student can be recovered by staff PIN issuance.
    new_pin = post(client, "/api/admin/student-pin",
                   {"student_id":STUDENT_ID,"verified_identity":True},headers=auth)
    assert new_pin.status_code == 200
    assert post(client, "/api/student-pin/status", STUDENT).json()["approval_status"] == "APPROVED"
    with server.connect() as conn:
        rec = conn.execute("SELECT approval_code_hash,status FROM student_pin_approvals WHERE student_id=?",
                           (STUDENT_ID,)).fetchone()
    assert rec["approval_code_hash"] is None
    assert rec["status"] == "APPROVED"
