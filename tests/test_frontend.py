from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_auth_and_role_navigation_guards_are_present():
    styles = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert ".authenticated .login-panel { display: none; }" in styles
    assert ".authenticated .shell { display: grid; }" in styles
    assert ".can-manage .manager-only" in styles
    assert ".is-admin .admin-only" in styles
    assert "function canAccessPanel(id)" in script
    assert "if(!canAccessPanel(id)||!document.getElementById(id))return false" in script


def test_mobile_keeps_logout_available():
    styles = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")

    mobile_rules = styles[styles.index("@media (max-width: 820px)") :]
    assert ".status-pill { display: none; }" in mobile_rules
    assert ".status-pill, .logout-button { display: none; }" not in mobile_rules
