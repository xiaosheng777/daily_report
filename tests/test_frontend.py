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
    assert "daily_duplicate.report_worker_count':'日报检查并行数（1-8）'" in script
    assert "本地查重：${Number(checked)}/${Number(total)}" in script
    assert "LLM复核：${Number(summary.llm_review_count||0)}" in script
    assert "降级本地判断：${Number(summary.degraded_local_count||0)}" in script
    assert "type=\"${meta.type||'text'}\"" in script
    assert "日报自动查重（${format(t.task_date)} 执行）" in script
    assert "if(!canAccessPanel(id)||!document.getElementById(id))return false" in script
    assert "function renderMatchDetails(f)" in script
    assert "Token 查重" in script
    assert "function refreshShanghaiClock()" in script
    assert "function waitForCheckTask(taskId,startedAt)" in script
    assert "/status`" in script
    assert "data-retry-task" in script
    assert "data-cancel-task" in script
    assert "timeZone:'Asia/Shanghai'" in script
    assert ".report-form > .submission-time-field" in styles
    assert ".report-form > .manager-field" in styles


def test_mobile_keeps_logout_available():
    styles = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")

    mobile_rules = styles[styles.index("@media (max-width: 900px)") :]
    assert ".status-pill, .logout-button { display: none; }" not in mobile_rules
    assert ".logout-button { width: auto;" in mobile_rules


def test_admin_audit_log_navigation_filters_details_and_export_are_present():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert 'data-panel="auditLogs"' in html
    assert '<span class="tab-index">10</span><span>审查日志</span>' in html
    assert all(item in html for item in ('id="auditStart"', 'id="auditEnd"', 'id="auditCategory"', 'id="auditSeverity"', 'id="auditOutcome"', 'id="auditQuery"'))
    assert all(item in html for item in ('id="auditTotal"', 'id="auditFailed"', 'id="auditCritical"', 'id="auditLoginFailed"'))
    assert "/api/admin/audit-logs?" in script
    assert "/api/admin/audit-logs.csv?" in script
    assert "state.auditCursor" in script
    assert "6*86400000" in script


def test_admin_runtime_log_stream_and_filters_are_present():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")

    assert 'data-panel="runtimeLogs"' in html
    assert all(item in html for item in ('id="runtimeLogFilterForm"', 'id="runtimeLogList"', 'id="runtimeLogFollow"', 'id="runtimeStreamState"'))
    assert "/api/admin/runtime-logs?" in script
    assert "/api/admin/runtime-logs/stream?" in script
    assert "new EventSource" in script and "stopRuntimeLogStream" in script
    assert ".runtime-terminal" in styles and ".runtime-log-table" in styles


def test_admin_task_console_has_progress_controls_polling_and_accessibility():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")

    assert 'data-panel="taskConsole"' in html
    assert '<span class="tab-index">07</span><span>任务管理台</span>' in html
    assert all(item in html for item in ('id="taskWorkerState"', 'id="consoleRunningCount"', 'id="consoleQueuedCount"', 'id="consolePausedCount"', 'id="taskConsoleList"'))
    assert "/api/admin/task-console?" in script
    assert "/api/admin/tasks/${encodeURIComponent(taskId)}/${endpoint}" in script
    assert "confirmation_task_id" in script
    assert "scheduleTaskConsolePolling" in script and "},2000)" in script
    assert 'role="progressbar"' in script and 'aria-valuenow="${percent}"' in script


def test_admin_resource_monitor_has_diagnostics_history_and_visibility_controls():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")

    assert '<span class="tab-index">08</span><span>资源监控</span>' in html
    assert all(item in html for item in ('id="resourceVerdict"', 'id="resourceExecutionChain"', 'id="resourceDiagnosisList"', 'id="resourceContainerList"'))
    assert all(value in html for value in ('data-monitor-range="1h"', 'data-monitor-range="6h"', 'data-monitor-range="24h"', 'data-monitor-range="7d"'))
    assert "/api/admin/resource-monitor/snapshot" in script
    assert "/api/admin/resource-monitor/history?range=" in script
    assert "stopResourceMonitorPolling" in script and "10000" in script and "60000" in script
    assert ".system-verdict" in styles and ".execution-chain" in styles and ".resource-sparkline" in styles
    assert ".task-queue::before" in styles
    assert ".task-progress-track" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_task_creator_sees_live_progress_bar_and_can_stop_then_delete():
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")

    assert "function taskProgressMarkup(task)" in script
    assert 'class="user-task-progress"' in script
    assert 'role="progressbar"' in script
    assert "function scheduleTaskListPolling()" in script
    assert "data-delete-active" in script
    assert "停止并删除" in script
    assert ".user-task-progress" in styles
