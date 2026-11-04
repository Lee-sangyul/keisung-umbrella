"""Admin stocktake tests. These use temporary SQLite databases only."""
import os
import sys
from pathlib import Path

os.environ["ADMIN_USERNAME"] = "test-teacher"
os.environ["ADMIN_PASSWORD"] = "test-admin-strong-password-123"
os.environ["SECURE_COOKIES"] = "false"
os.environ["APP_ORIGIN"] = ""
os.environ["DATA_DIR"] = "/tmp/keisung-umbrella-stocktake-tests"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
import server


ORIGIN = {"origin": "http://localhost"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "umbrellas.db")
    server.init_db()
    with TestClient(server.app, base_url="http://localhost") as test_client:
        yield test_client


def login(client):
    res = client.post("/api/admin/login", json={
        "username": "test-teacher", "password": "test-admin-strong-password-123"
    }, headers=ORIGIN)
    assert res.status_code == 200, res.text
    return {**ORIGIN, "x-csrf-token": res.json()["csrf"]}


def post(client, url, body, headers):
    return client.post(url, json=body, headers=headers)


def test_scoped_stocktake_batch_duplicates_export_and_no_inventory_mutation(client):
    headers = login(client)
    created = post(client, "/api/admin/umbrellas/batch", {"count": 4}, headers)
    assert created.status_code == 200
    assert post(client, "/api/admin/inventory/locations", {
        "start_id": "UMB-0001", "end_id": "UMB-0002", "location": "본관 1층"
    }, headers).json()["count"] == 2
    assert client.put("/api/admin/inventory/locations", headers=headers,
                      json={"start_id": "UMB-0003", "end_id": "UMB-0003",
                            "location": "별관 1층"}).status_code == 200
    inventory_before = client.get("/api/admin/inventory").json()
    assert {x["id"]: x["location"] for x in inventory_before}["UMB-0001"] == "본관 1층"

    res = post(client, "/api/admin/stocktakes", {
        "title": "11월 본관 정기 실사", "location": "본관 1층"
    }, headers)
    assert res.status_code == 200, res.text
    audit = res.json()["session"]
    assert audit["expected"] == 2 and audit["verified"] == 0 and audit["missing"] == 2
    sid = audit["id"]
    scan = post(client, f"/api/admin/stocktakes/{sid}/scan-batch", {
        "umbrella_ids": ["UMB-0001", "UMB-0001", "UMB-0003", "UMB-9999"]
    }, headers)
    assert scan.status_code == 200, scan.text
    assert [x["result"] for x in scan.json()["results"]] == [
        "VERIFIED", "DUPLICATE", "OUT_OF_SCOPE", "UNREGISTERED"
    ]
    assert scan.json()["session"]["missing"] == 1
    assert scan.json()["session"]["unexpected"] == 1

    detail = client.get(f"/api/admin/stocktakes/{sid}")
    assert detail.status_code == 200
    items = detail.json()["items"]
    missing = [x["umbrella_id"] for x in items if x["expected"] and not x["seen_at"]]
    assert missing == ["UMB-0002"]
    csv = client.get(f"/api/admin/stocktakes/{sid}/export")
    assert csv.status_code == 200
    assert csv.text.startswith("\ufeff")
    assert "MISSING" in csv.text
    assert "OUT_OF_SCOPE" in csv.text
    assert "UMB-0002" in csv.text

    closed = post(client, f"/api/admin/stocktakes/{sid}/complete", {}, headers)
    assert closed.status_code == 200
    assert closed.json()["session"]["status"] == "COMPLETED"
    assert post(client, f"/api/admin/stocktakes/{sid}/scan-batch", {
        "umbrella_ids": ["UMB-0002"]
    }, headers).status_code == 409
    inventory_after = client.get("/api/admin/inventory").json()
    assert inventory_before == inventory_after
    assert client.get("/api/admin/stocktakes").json()["sessions"][0]["id"] == sid
    backup = client.get("/api/admin/export").json()
    assert backup["schema_version"] == "3.2"
    assert len(backup["umbrella_locations"]) == 3
    assert len(backup["stocktake_sessions"]) == 1
    assert len(backup["stocktake_items"]) == 3


def test_schoolwide_snapshot_excludes_borrowed_lost_disposed_and_preserves_active_borrow(client):
    headers = login(client)
    assert post(client, "/api/admin/umbrellas/batch", {"count": 5}, headers).status_code == 200
    issued = post(client, "/api/admin/student-pin", {
        "student_id": "6-4-16", "verified_identity": True
    }, headers)
    assert issued.status_code == 200
    pin = issued.json()["pin"]
    borrowed = post(client, "/api/borrow", {
        "umbrella_id": "UMB-0001", "grade": 6, "classroom": 4,
        "number": 16, "pin": pin
    }, ORIGIN)
    assert borrowed.status_code == 200
    for num, status in [(2, "LOST"), (3, "DAMAGED"), (4, "DISPOSED")]:
        res = client.patch(f"/api/admin/umbrellas/UMB-{num:04d}", headers=headers,
                           json={"status": status, "reason": "재고 실사 테스트"})
        assert res.status_code == 200, res.text
    created = post(client, "/api/admin/stocktakes", {
        "title": "학교 전체 점검", "location": "ALL"
    }, headers)
    assert created.status_code == 200
    assert created.json()["session"]["expected"] == 2  # damaged(3) + available(5)
    sid = created.json()["session"]["id"]
    detail = client.get(f"/api/admin/stocktakes/{sid}").json()
    expected = {x["umbrella_id"] for x in detail["items"]}
    assert expected == {"UMB-0003", "UMB-0005"}
    scan = post(client, f"/api/admin/stocktakes/{sid}/scan-batch", {
        "umbrella_ids": ["UMB-0001", "UMB-0003"]
    }, headers)
    assert [x["result"] for x in scan.json()["results"]] == ["OUT_OF_SCOPE", "VERIFIED"]
    assert scan.json()["session"]["missing"] == 1
    after = {x["id"]: x for x in client.get("/api/admin/inventory").json()}
    assert after["UMB-0001"]["status"] == "BORROWED"
    assert after["UMB-0001"]["borrower"] == "6-4-16"
    assert after["UMB-0002"]["status"] == "LOST"
    assert after["UMB-0003"]["status"] == "DAMAGED"
    assert after["UMB-0004"]["status"] == "DISPOSED"


def test_admin_csrf_input_limits_location_snapshot_and_scope(client):
    assert client.get("/api/admin/stocktakes").status_code == 401
    assert client.get("/api/admin/stocktakes/1/export").status_code == 401
    assert client.post("/api/admin/stocktakes", json={
        "title": "무권한 실사", "location": "ALL"
    }, headers=ORIGIN).status_code == 401
    headers = login(client)
    assert post(client, "/api/admin/umbrellas/batch", {"count": 3}, headers).status_code == 200

    assert post(client, "/api/admin/stocktakes", {
        "title": "권한 불일치 테스트", "location": "ALL"
    }, ORIGIN).status_code == 403
    assert post(client, "/api/admin/stocktakes", {
        "title": "미배정 구역", "location": "없는 위치"
    }, headers).status_code == 422
    assert client.put("/api/admin/inventory/locations", headers=headers,json={
        "start_id": "UMB-0001", "end_id": "UMB-0003",
        "location": "ALL"
    }).status_code == 422
    assert client.put("/api/admin/inventory/locations", headers=headers,json={
        "start_id": "UMB-0001", "end_id": "UMB-0005",
        "location": "본관"
    }).status_code == 404
    assert client.put("/api/admin/inventory/locations", headers=headers,json={
        "start_id": "UMB-0001", "end_id": "UMB-0002",
        "location": "동관"
    }).status_code == 200

    res = post(client, "/api/admin/stocktakes", {
        "title": "동관 정기 실사", "location": "동관"
    }, headers)
    assert res.status_code == 200
    sid = res.json()["session"]["id"]
    assert res.json()["session"]["expected"] == 2
    # Assignment after creation cannot alter the saved audit snapshot.
    assert client.put("/api/admin/inventory/locations", headers=headers,json={
        "start_id": "UMB-0002", "end_id": "UMB-0002",
        "location": "서관"
    }).status_code == 200
    assert client.get(f"/api/admin/stocktakes/{sid}").json()["session"]["expected"] == 2
    assert post(client, f"/api/admin/stocktakes/{sid}/scan-batch", {
        "umbrella_ids": ["INVALID"]
    }, headers).status_code == 422
    assert post(client, f"/api/admin/stocktakes/{sid}/scan-batch", {
        "umbrella_ids": [f"UMB-{x:04d}" for x in range(1,502)]
    }, headers).status_code == 422
    assert post(client, f"/api/admin/stocktakes/{sid}/scan-batch", {
        "umbrella_ids": ["UMB-0001"]
    }, ORIGIN).status_code == 403
    assert post(client, f"/api/admin/stocktakes/{sid}/scan-batch", {
        "umbrella_ids": ["UMB-0002"]
    }, headers).json()["results"][0]["result"] == "VERIFIED"
