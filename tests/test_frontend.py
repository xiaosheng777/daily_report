from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_auth_and_role_navigation_guards_are_present():
    styles = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert ".authenticated .login-panel { display: none; }" in styles
    assert ".authenticated .shell { display: grid; }" in styles
    assert ".can-manage .tab.manager-only" in styles
    assert ".can-export-reports .tab.exporter-only" in styles
    assert ".is-admin .tab.admin-only" in styles
    assert "function canAccessPanel(id)" in script
    assert "function canManage(){return ['group_leader','director','minister'].includes(state.user?.role)}" in script
    assert "function canExportReports()" in script
    assert "daily_report_submission.rollover_time':'日报归属切换时间'" in script
    assert "type=\"${isTime?'time':'text'}\"" in script
    assert "日报自动查重（${format(t.task_date)} 执行）" in script
    assert "if(!canAccessPanel(id)||!document.getElementById(id))return false" in script
    assert "function renderMatchDetails(f)" in script
    assert "Token 查重" in script
    assert "function refreshShanghaiClock()" in script
    assert "timeZone:'Asia/Shanghai'" in script
    assert ".report-form > .submission-time-field" in styles
    assert ".report-form > .manager-field" in styles


def test_mobile_keeps_logout_available():
    styles = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")

    mobile_rules = styles[styles.index("@media (max-width: 900px)") :]
    assert ".status-pill, .logout-button { display: none; }" not in mobile_rules
    assert ".logout-button { width: auto;" in mobile_rules
