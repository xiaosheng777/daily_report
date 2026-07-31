from __future__ import annotations

import json
from datetime import timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from docx import Document
from openpyxl import Workbook
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from src.core.schemas import CheckResult, Finding, Report, ReviewCase, User, highest_risk
from src.core.text import low_information, normalize_text, stable_hash
from src.core.testcase_excel import (
    DEFAULT_COMPARE_FIELDS,
    case_key,
    compare_fields_equal,
    differing_fields,
    load_daily_sheet_rows,
)
from src.db.base import DatabaseBackend
from src.llm.base import LLMJudge
from src.storage.file_store import FileStorage
from src.utils.dates import days_between, now_iso, parse_date


class DailyReportPipeline:
    def __init__(self, config: dict[str, Any], db: DatabaseBackend, storage: FileStorage, llm_judge: LLMJudge | None = None) -> None:
        self.config = config
        self.db = db
        self.storage = storage
        self.llm_judge = llm_judge
        self.daily_cfg = config.get("daily_duplicate", {})
        self.checks = config.get("checks", {})

    def run(self, user: User | None = None, scope: dict[str, Any] | None = None, task_id: str = "", result_dir: str = "") -> dict[str, Any]:
        scope = scope or {}
        # Candidate data must stay inside the initiator's organisation scope.
        reports_all = self.db.list_reports(user) if user else self.db.list_reports(None)
        current_reports = self._filter_current_reports(reports_all, user, scope)
        cases = self._build_cases(current_reports, reports_all)
        missing_reports = self._find_missing_reports(current_reports, user, scope)
        payload = {
            "task_id": task_id,
            "generated_at": now_iso(),
            "summary": {
                "report_count": len(cases),
                "high_risk_count": sum(1 for c in cases if c.overall_risk == "high"),
                "medium_risk_count": sum(1 for c in cases if c.overall_risk == "medium"),
                "unknown_risk_count": sum(1 for c in cases if c.overall_risk == "unknown"),
                "triggered_duplicate_checks": sum(1 for c in cases if "report_duplicate_check" in c.triggered_checks),
                "triggered_testcase_checks": sum(1 for c in cases if "testcase_check" in c.triggered_checks),
                "triggered_code_checks": sum(1 for c in cases if "code_check" in c.triggered_checks),
                "triggered_document_checks": sum(1 for c in cases if "document_check" in c.triggered_checks),
                "missing_report_count": len(missing_reports),
            },
            "report_cases": [case.to_dict() for case in cases],
            "missing_reports": missing_reports,
        }
        if task_id and result_dir:
            self._persist_task_outputs(payload, Path(result_dir), user)
        return payload

    def _persist_task_outputs(self, payload: dict[str, Any], result_dir: Path, user: User | None) -> None:
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "review_cases.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.export_review_xlsx(payload["report_cases"], result_dir / "review_cases.xlsx")
        self.export_review_docx(payload["report_cases"], result_dir / "review_cases.docx")
        self.export_missing_reports_xlsx(payload["missing_reports"], result_dir / "missing_reports.xlsx")
        (result_dir / "run_metadata.json").write_text(json.dumps({"generated_at": payload.get("generated_at"), "config": self.config, "user": user.to_dict() if user else None}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _filter_current_reports(self, reports: list[Report], user: User | None, scope: dict[str, Any]) -> list[Report]:
        start = str(scope.get("report_date_start") or "")
        end = str(scope.get("report_date_end") or "")
        department_scope = str(scope.get("department_scope") or "")
        employee_scope = str(scope.get("employee_scope") or "")
        out = []
        for r in reports:
            if user and user.role == "employee" and r.owner_user_id != user.user_id:
                continue
            if user and user.role == "group_leader" and r.owner_user_id != user.user_id and r.group_id != user.group_id:
                continue
            if user and user.role == "director" and r.department_id != user.department_id:
                continue
            if user and user.role == "admin":
                continue
            if start and r.report_date < parse_date(start):
                continue
            if end and r.report_date > parse_date(end):
                continue
            if department_scope and department_scope not in {r.department_id, r.department_name}:
                continue
            if employee_scope and employee_scope not in {r.owner_user_id, r.employee_name}:
                continue
            out.append(r)
        return sorted(out, key=lambda r: (r.report_date, r.employee_name))

    def _find_missing_reports(self, reports: list[Report], user: User | None, scope: dict[str, Any]) -> list[dict[str, Any]]:
        start, end = str(scope.get("report_date_start") or ""), str(scope.get("report_date_end") or "")
        if not user or not start or not end:
            return []
        start_date = parse_date(start)
        end_date = parse_date(end)
        if start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")
        submitters = self.db.list_required_submitters(user)
        department_scope, employee_scope = str(scope.get("department_scope") or ""), str(scope.get("employee_scope") or "")
        submitters = [s for s in submitters if (not department_scope or department_scope in {s.get("department_id"), s.get("department_name")}) and (not employee_scope or employee_scope in {s.get("user_id"), s.get("display_name")})]
        submitted = {(r.owner_user_id, r.report_date) for r in reports}
        days, cursor = [], parse_date(start_date)
        from datetime import datetime
        current = datetime.strptime(cursor, "%Y-%m-%d").date()
        final = datetime.strptime(end_date, "%Y-%m-%d").date()
        while current <= final:
            if current.weekday() < 5:
                days.append(current.isoformat())
            current += timedelta(days=1)
        missing = []
        for submitter in submitters:
            created_date = str(submitter.get("created_at") or "")[:10]
            for report_date in days:
                if created_date and created_date > report_date:
                    continue
                if (submitter["user_id"], report_date) not in submitted:
                    missing.append({"report_date": report_date, "user_id": submitter["user_id"], "employee_name": submitter["display_name"], "role": submitter["role"], "department_name": submitter.get("department_name", ""), "group_name": submitter.get("group_name", "")})
        return missing

    def _build_cases(self, current_reports: list[Report], all_reports: list[Report]) -> list[ReviewCase]:
        texts = [normalize_text(r.title_summary, r.work_description) for r in all_reports]
        matrix = None
        try:
            if any(texts):
                vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=1)
                matrix = vectorizer.fit_transform(texts)
        except Exception:
            matrix = None
        index_by_id = {r.report_id: i for i, r in enumerate(all_reports)}
        group_extras_cache: dict[tuple[str, str], list[Finding]] = {}
        cases: list[ReviewCase] = []
        for report in current_reports:
            checks: list[CheckResult] = [
                self._quality_check(report),
                self._daily_duplicate_check(report, all_reports, index_by_id, matrix),
                self._testcase_check(report, all_reports, group_extras_cache),
            ]
            if self.checks.get("enable_code_check", True):
                code = self._simple_artifact_check(report, "code")
                if code.triggered:
                    checks.append(code)
            if self.checks.get("enable_document_check", True):
                doc = self._simple_artifact_check(report, "document")
                if doc.triggered:
                    checks.append(doc)
            triggered = [c.checker_id for c in checks if c.triggered]
            risk = highest_risk(c.risk_level for c in checks if c.triggered) if triggered else "normal"
            cases.append(ReviewCase(report.report_id, report.employee_name, report.owner_user_id, report.department_id, report.department_name, report.report_date, report.title_summary, risk, triggered, checks))
        return cases

    def _quality_check(self, report: Report) -> CheckResult:
        issues = []
        if not report.title_summary.strip():
            issues.append("标题缺失")
        if not report.work_description.strip():
            issues.append("工作描述缺失")
        elif len(report.work_description.replace(" ", "")) < int(self.checks.get("description_too_short_length", 12)):
            issues.append("工作描述过短")
        if low_information(report.title_summary, report.work_description, int(self.checks.get("low_information_min_length", 10))):
            issues.append("信息量偏低")
        if not issues:
            return CheckResult("quality_check", False, "success", "normal", "日报信息量正常")
        risk = "high" if "工作描述缺失" in issues else "medium"
        return CheckResult("quality_check", True, "success", risk, "日报质量需关注：" + "、".join(issues), [Finding("quality_check", risk, "quality", report.title_summary, reason="、".join(issues))])

    def _daily_duplicate_check(self, current: Report, reports: list[Report], index_by_id: dict[str, int], matrix) -> CheckResult:
        candidates = self._collect_candidates(current, reports, index_by_id, matrix)
        threshold = float(self.daily_cfg.get("no_candidate_threshold", 0.55))
        if not candidates or candidates[0]["candidate_score"] < threshold:
            return CheckResult("report_duplicate_check", True, "success", "normal", "未找到足够相似的历史日报")
        top_n = max(1, min(10, int(self.daily_cfg.get("llm_candidate_top_n", 3))))
        llm_threshold = float(self.daily_cfg.get("low_info_candidate_score_threshold", 0.82) if low_information(current.title_summary, current.work_description) else self.daily_cfg.get("llm_candidate_score_threshold", 0.72))
        to_judge = [c for c in candidates[:top_n] if c["candidate_score"] >= llm_threshold or c["hash_same"]]
        if not to_judge:
            to_judge = candidates[:1]
        findings: list[Finding] = []
        for cand in to_judge:
            other: Report = cand["report"]
            risk = "high" if cand["hash_same"] else "medium" if cand["candidate_score"] >= 0.75 else "low"
            reason = "标准化后文本完全一致" if cand["hash_same"] else f"候选相似度 {cand['candidate_score']:.2f}，建议复核"
            details = self._candidate_details(cand)
            if self.llm_judge and not cand["hash_same"] and cand["candidate_score"] >= llm_threshold:
                result = self.llm_judge.judge_duplicate(current.to_dict(), other.to_dict(), details)
                details["llm_called"] = True
                if result.ok:
                    risk = result.duplicate_risk
                    reason = result.decision_summary or reason
                    details["llm_confidence"] = result.confidence
                    details["llm_raw"] = result.raw
                else:
                    risk = "unknown"
                    reason = f"大模型调用失败：{result.error}"
                    details["llm_error"] = result.error
            else:
                details["llm_called"] = False
            findings.append(Finding("report_duplicate_check", risk, "matched", current.title_summary, f"{other.employee_name}-{other.report_date}-{other.title_summary}", cand["candidate_score"], reason, details))
        risk = highest_risk(f.risk_level for f in findings)
        summary = "；".join(f.reason for f in findings[:3])
        return CheckResult("report_duplicate_check", True, "success", risk, summary, findings)

    def _collect_candidates(self, current: Report, reports: list[Report], index_by_id: dict[str, int], matrix) -> list[dict[str, Any]]:
        self_days = int(self.daily_cfg.get("self_history_days", 30))
        cross_days = int(self.daily_cfg.get("cross_user_days", 3))
        cross_scope = str(self.daily_cfg.get("cross_user_scope", "department"))
        max_candidates = max(1, int(self.daily_cfg.get("max_candidates", 20)))
        weights = self.daily_cfg.get("score_weights", {})
        wt_text, wt_sem, wt_rec = float(weights.get("text", 0.45)), float(weights.get("semantic", 0.45)), float(weights.get("recency", 0.10))
        current_text = normalize_text(current.title_summary, current.work_description)
        current_hash = stable_hash(current_text)
        candidates = []
        for other in reports:
            if other.report_id == current.report_id or other.status == "withdrawn":
                continue
            if other.report_date > current.report_date:
                continue
            gap = days_between(current.report_date, other.report_date)
            same_user = other.owner_user_id == current.owner_user_id
            if same_user:
                if gap > self_days:
                    continue
                match_type = "self_repeat"
                horizon = max(1, self_days)
            else:
                if gap > cross_days:
                    continue
                if cross_scope != "all" and other.department_id != current.department_id:
                    continue
                match_type = "cross_user_repeat"
                horizon = max(1, cross_days)
            text = normalize_text(other.title_summary, other.work_description)
            seq = SequenceMatcher(None, current_text, text).ratio()
            sem = 0.0
            if matrix is not None and current.report_id in index_by_id and other.report_id in index_by_id:
                sem = float(cosine_similarity(matrix[index_by_id[current.report_id]], matrix[index_by_id[other.report_id]])[0, 0])
            recency = max(0.0, 1.0 - min(gap, horizon) / horizon)
            score = round(min(1.0, seq * wt_text + sem * wt_sem + recency * wt_rec), 4)
            candidates.append({"report": other, "candidate_score": score, "text_similarity": round(seq, 4), "semantic_similarity": round(sem, 4), "recency_score": round(recency, 4), "match_type": match_type, "days_gap": gap, "hash_same": current_hash == stable_hash(text)})
        candidates.sort(key=lambda x: (x["hash_same"], x["candidate_score"]), reverse=True)
        return candidates[:max_candidates]

    @staticmethod
    def _candidate_details(cand: dict[str, Any]) -> dict[str, Any]:
        other: Report = cand["report"]
        return {"matched_report_id": other.report_id, "matched_report_date": other.report_date, "matched_owner_user_id": other.owner_user_id, "match_type": cand["match_type"], "days_gap": cand["days_gap"], "candidate_score": cand["candidate_score"], "text_similarity": cand["text_similarity"], "semantic_similarity": cand["semantic_similarity"], "recency_score": cand["recency_score"], "hash_same": cand["hash_same"]}

    def _testcase_check(
        self,
        report: Report,
        all_reports: list[Report],
        group_extras_cache: dict[tuple[str, str], list[Finding]],
    ) -> CheckResult:
        items = [x for x in report.testcase_items if any(str(v or "").strip() for v in x.values())]
        if not items:
            return CheckResult("testcase_check", False, "skipped", "normal", "未上传测试用例 Excel")
        tc_cfg = self.config.get("testcase_verification", {})
        if not tc_cfg.get("enabled", True):
            return CheckResult("testcase_check", True, "skipped", "unknown", "测试用例核验未启用")
        compare_fields = list(tc_cfg.get("compare_fields") or DEFAULT_COMPARE_FIELDS)
        findings: list[Finding] = []
        findings.extend(self._testcase_self_history_findings(report, items, all_reports, tc_cfg, compare_fields))
        findings.extend(self._testcase_group_consistency_findings(report, items, all_reports, compare_fields, group_extras_cache))
        if not findings:
            return CheckResult("testcase_check", True, "success", "normal", "测试用例核验通过，未发现异常")
        risk = highest_risk(f.risk_level for f in findings)
        summary = "；".join(dict.fromkeys(f.reason for f in findings[:4]))
        return CheckResult("testcase_check", True, "success", risk, summary or "测试用例核验完成", findings)

    def _testcase_self_history_findings(
        self,
        report: Report,
        items: list[dict[str, Any]],
        all_reports: list[Report],
        tc_cfg: dict[str, Any],
        compare_fields: list[str],
    ) -> list[Finding]:
        history_days = int(tc_cfg.get("self_history_days", 7))
        high_rate = float(tc_cfg.get("self_history_high_rate", 0.50))
        medium_rate = float(tc_cfg.get("self_history_medium_rate", 0.20))
        history_rows: dict[str, tuple[dict[str, Any], str]] = {}
        for other in all_reports:
            if other.owner_user_id != report.owner_user_id or other.report_id == report.report_id:
                continue
            if other.status == "withdrawn":
                continue
            if other.report_date >= report.report_date:
                continue
            if days_between(report.report_date, other.report_date) > history_days:
                continue
            for item in other.testcase_items or []:
                cid = case_key(item) or str(item.get("case_id") or "").strip()
                if not cid:
                    continue
                # Prefer the nearest historical day for the same case id.
                prev = history_rows.get(cid)
                if prev is None or other.report_date > prev[1]:
                    history_rows[cid] = (item, other.report_date)

        duplicates: list[Finding] = []
        for item in items:
            cid = case_key(item) or str(item.get("case_id") or "").strip()
            if not cid or cid not in history_rows:
                continue
            hist_item, hist_date = history_rows[cid]
            if not compare_fields_equal(item, hist_item, compare_fields):
                continue
            duplicates.append(
                Finding(
                    "testcase_check",
                    "medium",
                    "self_history_duplicate",
                    cid,
                    f"{hist_date}:{cid}",
                    score=1.0,
                    reason=f"用例 {cid} 与本人 {hist_date} 的用例关键字段一致",
                    details={"case_id": cid, "history_date": hist_date, "match_type": "self_history_duplicate"},
                )
            )

        total = len([x for x in items if case_key(x) or str(x.get("case_id") or "").strip()])
        duplicate_count = len(duplicates)
        rate = (duplicate_count / total) if total else 0.0
        if rate >= high_rate:
            risk = "high"
        elif rate >= medium_rate:
            risk = "medium"
        elif duplicate_count:
            risk = "low"
        else:
            return []
        for finding in duplicates:
            finding.risk_level = risk
            finding.score = round(rate, 4)
            finding.details["duplicate_rate"] = round(rate, 4)
            finding.details["duplicate_count"] = duplicate_count
            finding.details["today_count"] = total
            finding.details["high_rate"] = high_rate
            finding.details["medium_rate"] = medium_rate
        summary_finding = Finding(
            "testcase_check",
            risk,
            "self_history_rate",
            report.employee_name,
            score=round(rate, 4),
            reason=f"本人近 {history_days} 天测试用例查重率 {rate:.0%}（重复 {duplicate_count}/{total}）",
            details={
                "duplicate_rate": round(rate, 4),
                "duplicate_count": duplicate_count,
                "today_count": total,
                "self_history_days": history_days,
                "high_rate": high_rate,
                "medium_rate": medium_rate,
            },
        )
        return [summary_finding, *duplicates]

    def _testcase_group_consistency_findings(
        self,
        report: Report,
        items: list[dict[str, Any]],
        all_reports: list[Report],
        compare_fields: list[str],
        group_extras_cache: dict[tuple[str, str], list[Finding]],
    ) -> list[Finding]:
        if not report.group_id:
            return [
                Finding(
                    "testcase_check",
                    "unknown",
                    "group_missing",
                    report.employee_name,
                    reason="用户未绑定小组，跳过组长汇总一致性检查",
                )
            ]
        snapshot = self.db.find_testcase_snapshot(report.group_id, report.report_date)
        if not snapshot:
            return [
                Finding(
                    "testcase_check",
                    "medium",
                    "snapshot_missing",
                    report.group_name or report.group_id,
                    reason="该小组当天未上传测试用例汇总表",
                    details={"group_id": report.group_id, "report_date": report.report_date},
                )
            ]
        try:
            leader_rows = self._load_testcase_rows(snapshot["stored_path"])
        except ValueError as exc:
            return [
                Finding(
                    "testcase_check",
                    "high",
                    "sheet_missing",
                    report.group_name or report.group_id,
                    reason=str(exc),
                    details={"snapshot_id": snapshot.get("snapshot_id")},
                )
            ]

        findings: list[Finding] = []
        for item in items:
            cid = case_key(item) or str(item.get("case_id") or "").strip()
            if not cid:
                continue
            leader_row = leader_rows.get(cid)
            if not leader_row:
                findings.append(
                    Finding(
                        "testcase_check",
                        "high",
                        "missing_in_leader",
                        cid,
                        score=0.0,
                        reason=f"组员用例 {cid} 未出现在组长当天汇总表中",
                        details={"case_id": cid, "snapshot_id": snapshot.get("snapshot_id")},
                    )
                )
                continue
            diffs = differing_fields(item, leader_row, compare_fields)
            if diffs:
                findings.append(
                    Finding(
                        "testcase_check",
                        "high",
                        "content_mismatch",
                        cid,
                        score=0.0,
                        reason=f"用例 {cid} 与组长汇总不一致：{('、'.join(diffs))}",
                        details={"case_id": cid, "diff_fields": diffs, "snapshot_id": snapshot.get("snapshot_id")},
                    )
                )

        cache_key = (report.group_id, report.report_date)
        if cache_key not in group_extras_cache:
            member_cases: dict[str, dict[str, Any]] = {}
            for other in all_reports:
                if other.group_id != report.group_id or other.report_date != report.report_date:
                    continue
                if other.status == "withdrawn":
                    continue
                for item in other.testcase_items or []:
                    cid = case_key(item) or str(item.get("case_id") or "").strip()
                    if cid:
                        member_cases[cid] = item
            extras = []
            for cid, leader_row in leader_rows.items():
                if cid.startswith("row_"):
                    continue
                if cid in member_cases:
                    continue
                extras.append(
                    Finding(
                        "testcase_check",
                        "high",
                        "extra_in_leader",
                        cid,
                        score=0.0,
                        reason=f"组长汇总存在组员未提交的用例 {cid}",
                        details={"case_id": cid, "snapshot_id": snapshot.get("snapshot_id"), "group_id": report.group_id},
                    )
                )
            group_extras_cache[cache_key] = extras
        findings.extend(group_extras_cache.get(cache_key, []))
        return findings

    def _load_testcase_rows(self, stored_path: str) -> dict[str, dict[str, Any]]:
        path = self.storage.resolve(stored_path)
        return load_daily_sheet_rows(path)

    @staticmethod
    def _case_key(row: dict[str, Any]) -> str:
        return case_key(row)

    @staticmethod
    def _row_hash(row: dict[str, Any]) -> str:
        return stable_hash(json.dumps(row, ensure_ascii=False, sort_keys=True))

    def _simple_artifact_check(self, report: Report, artifact_type: str) -> CheckResult:
        checker = "code_check" if artifact_type == "code" else "document_check"
        artifacts = [a for a in self.db.list_artifacts_for_report(report.report_id) if a.get("artifact_type") == artifact_type]
        items = report.code_items if artifact_type == "code" else report.document_items
        if not artifacts and not any(any(str(v or "").strip() for v in item.values()) for item in items):
            return CheckResult(checker, False, "skipped", "normal", "未填写专项条目")
        if artifact_type == "code":
            jplag_path = Path(str(self.config.get("jplag", {}).get("jar_path", "")))
            details = {"jplag_enabled": bool(self.config.get("jplag", {}).get("enabled", True)), "jplag_available": jplag_path.exists(), "artifact_count": len(artifacts), "scope": "self_history_only"}
            summary = "已记录代码附件；JPlag 可用" if jplag_path.exists() else "已记录代码附件；JPlag jar 未提供，正式代码查重会降级"
        else:
            details = {"artifact_count": len(artifacts), "scope": "self_history_only", "docx_tables_supported": True}
            summary = "已记录文档附件，文档查重保持本人历史范围"
        return CheckResult(checker, True, "success", "normal", summary, [Finding(checker, "normal", "recorded", report.title_summary, score=0, reason=summary, details=details)])

    @staticmethod
    def build_report_id(report: Report) -> str:
        return "R_" + stable_hash(f"{report.owner_user_id}|{report.report_date}|{report.title_summary}|{report.work_description}")[:16]

    @staticmethod
    def export_review_xlsx(cases: list[dict[str, Any]], path: Path) -> None:
        wb = Workbook()
        ws = wb.active
        ws.title = "review_cases"
        ws.append(["日期", "员工", "部门", "标题", "总体风险", "触发检查", "摘要"])
        for c in cases:
            summary = "；".join(check.get("summary", "") for check in c.get("checks", []) if check.get("triggered"))
            ws.append([c.get("report_date"), c.get("employee_name"), c.get("department_name"), c.get("title_summary"), c.get("overall_risk"), ",".join(c.get("triggered_checks", [])), summary])
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)

    @staticmethod
    def export_review_docx(cases: list[dict[str, Any]], path: Path) -> None:
        doc = Document()
        doc.add_heading("查重任务报告", level=1)
        doc.add_paragraph(f"共 {len(cases)} 份日报")
        for c in cases:
            doc.add_heading(f"{c.get('report_date')} · {c.get('employee_name')} · {c.get('overall_risk')}", level=2)
            doc.add_paragraph(str(c.get("title_summary") or ""))
            for check in c.get("checks", []):
                if check.get("triggered"):
                    doc.add_paragraph(f"{check.get('checker_id')}: {check.get('summary')}")
                    for f in check.get("findings", []):
                        doc.add_paragraph(f"- {f.get('risk_level')} | {f.get('reason')}")
        path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(path)

    @staticmethod
    def export_missing_reports_xlsx(rows: list[dict[str, Any]], path: Path) -> None:
        wb = Workbook()
        ws = wb.active
        ws.title = "missing_reports"
        ws.append(["日期", "姓名", "职务", "部门", "小组"])
        for row in rows:
            ws.append([row.get("report_date"), row.get("employee_name"), row.get("role"), row.get("department_name"), row.get("group_name")])
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
