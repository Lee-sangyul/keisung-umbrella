"""Installable PWA plumbing. Device installation requires browser checks too."""
import os
from pathlib import Path

os.environ.setdefault("ADMIN_USERNAME", "test-teacher")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-strong-password-123")
os.environ.setdefault("DEPLOYMENT_MODE", "development")
os.environ.setdefault("APP_ORIGIN", "")
os.environ.setdefault("SECURE_COOKIES", "false")
os.environ.setdefault("DATA_DIR", "/tmp/keisung-pwa-tests")
from fastapi.testclient import TestClient
import server

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_and_service_worker_routes():
    with TestClient(server.app) as client:
        response = client.get("/manifest.webmanifest")
        assert response.status_code == 200
        assert "manifest+json" in response.headers["content-type"]
        manifest = response.json()
        assert manifest["start_url"] == "/"
        assert manifest["scope"] == "/"
        assert manifest["display"] == "standalone"
        assert {x["sizes"] for x in manifest["icons"]} == {"192x192", "512x512"}
        worker = client.get("/sw.js")
        assert worker.status_code == 200
        assert worker.headers["service-worker-allowed"] == "/"
        assert "javascript" in worker.headers["content-type"]
        assert "fetch" not in worker.text
        assert "caches.open" not in worker.text


def test_all_pages_link_same_origin_manifest():
    for path in ("kiosk.html", "index.html", "print.html"):
        markup = (ROOT / path).read_text(encoding="utf-8")
        assert 'rel="manifest" href="/manifest.webmanifest"' in markup
        assert '/assets/pwa-install.js' in markup
    assert (ROOT / "assets" / "pwa-install.js").is_file()


def test_valid_png_dimensions():
    for size in (192, 512):
        image = (ROOT / "assets" / f"pwa-icon-{size}.png").read_bytes()
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        assert int.from_bytes(image[16:20], "big") == size
        assert int.from_bytes(image[20:24], "big") == size


def test_docker_and_host_deployment_files():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "manifest.webmanifest sw.js" in docker
    assert "COPY assets ./assets" in docker
    example = (ROOT / ".env.production.example").read_text(encoding="utf-8")
    assert "APP_ORIGIN=https://keisungschool.tech" in example
    assert "keisungschool.tech" in (ROOT / "Caddyfile.example").read_text()
