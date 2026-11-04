"""Production guardrails and SQLite online backup tests."""
import os
import sqlite3
import sys
from pathlib import Path

os.environ["ADMIN_USERNAME"] = "test-teacher"
os.environ["ADMIN_PASSWORD"] = "test-admin-strong-password-123"
os.environ["SECURE_COOKIES"] = "false"
os.environ["APP_ORIGIN"] = ""
os.environ["DEPLOYMENT_MODE"] = "development"
os.environ["DATA_DIR"] = "/tmp/keisung-umbrella-deployment-tests"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
import backup_db
import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "umbrellas.db")
    server.init_db()
    with TestClient(server.app, base_url="http://localhost") as c:
        yield c


def test_health_and_security_headers(client):
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/config").json()["self_enrollment"] is True


def test_production_requires_https_and_secure_cookie():
    with pytest.raises(RuntimeError):
        server.validate_production_settings("production", "http://school.example", True)
    with pytest.raises(RuntimeError):
        server.validate_production_settings("production", "https://school.example", False)
    with pytest.raises(RuntimeError):
        server.validate_production_settings("production", "https://school.example/private", True)
    server.validate_production_settings("production", "https://school.example", True)


def test_production_disallows_unverified_student_pin_registration(client, monkeypatch):
    monkeypatch.setattr(server, "DEPLOYMENT_MODE", "production")
    monkeypatch.setattr(server, "ALLOW_STUDENT_SELF_ENROLLMENT", False)
    monkeypatch.setattr(server, "APP_ORIGIN", "http://localhost")
    response = client.post("/api/student-pin/enroll",
        headers={"origin": "http://localhost"},
        json={"grade": 6, "classroom": 4, "number": 16,
              "pin": "482739", "pin_confirm": "482739"})
    assert response.status_code == 403
    assert client.get("/api/config").json()["self_enrollment"] is False


def test_admin_login_rate_limit_persists(client):
    headers = {"origin": "http://localhost"}
    for _ in range(5):
        response = client.post("/api/admin/login", headers=headers,
            json={"username": "test-teacher", "password": "not-correct"})
        assert response.status_code == 401
    blocked = client.post("/api/admin/login", headers=headers,
        json={"username": "test-teacher", "password": "test-admin-strong-password-123"})
    assert blocked.status_code == 429
    with server.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM admin_login_limits").fetchone()[0] >= 1


def test_online_backup_includes_committed_wal_data(tmp_path):
    original = tmp_path / "umbrellas.db"
    writer = sqlite3.connect(original)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        writer.execute("INSERT INTO records VALUES (1, 'committed')")
        writer.commit()
        snapshot = backup_db.create_backup(original, tmp_path / "secure-backups")
        assert snapshot.exists() and original.exists()
        with sqlite3.connect(snapshot) as conn:
            assert conn.execute("SELECT value FROM records").fetchone()[0] == "committed"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        writer.close()
