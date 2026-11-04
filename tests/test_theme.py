"""Static checks for the alternate pastel theme edition."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_views_load_self_hosted_theme():
    for page in ("kiosk.html", "index.html", "print.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert '<link rel="stylesheet" href="/assets/chiikawa-theme.css">' in html
        assert 'class="chiikawa-edition ' in html


def test_theme_is_self_contained_and_accessible():
    css = (ROOT / "assets/chiikawa-theme.css").read_text(encoding="utf-8")
    assert "http://" not in css and "https://" not in css
    assert "focus-visible" in css
    assert "prefers-reduced-motion" in css
    assert "@media print" in css


def test_key_kiosk_workflow_is_unchanged():
    html = (ROOT / "kiosk.html").read_text(encoding="utf-8")
    for marker in ('id="roleChooser"', 'id="actionChooser"', 'id="studentPin"',
                   '"/api/student-pin/status"', '"/api/umbrellas"'):
        assert marker in html
