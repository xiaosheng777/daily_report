from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class User:
    user_id: str
    username: str
    display_name: str
    department_id: str
    department_name: str
    role: str
    manager_user_id: str = ""
    enabled: bool = True
    group_id: str = ""
    group_name: str = ""
    manager_display_name: str = ""
    is_test_account: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Report:
    report_id: str
    owner_user_id: str
    employee_name: str
    department_id: str
    department_name: str
    report_date: str
    manager: str
    title_summary: str
    work_description: str
    source_type: str = "web"
    original_filename: str = ""
    stored_path: str = ""
    file_size: int = 0
    content_hash: str = ""
    code_items: list[dict[str, Any]] = field(default_factory=list)
    document_items: list[dict[str, Any]] = field(default_factory=list)
    testcase_items: list[dict[str, Any]] = field(default_factory=list)
    status: str = "active"
    locked_by_task_id: str = ""
    revision_of_report_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    withdrawn_at: str = ""
    group_id: str = ""
    group_name: str = ""
    submitter_role: str = "employee"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Finding:
    checker_id: str
    risk_level: str
    status: str
    target_name: str = ""
    matched_target_name: str = ""
    score: float = 0.0
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CheckResult:
    checker_id: str
    triggered: bool
    status: str
    risk_level: str
    summary: str
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["findings"] = [finding.to_dict() for finding in self.findings]
        return data


@dataclass(slots=True)
class ReviewCase:
    report_id: str
    employee_name: str
    owner_user_id: str
    department_id: str
    department_name: str
    group_id: str
    group_name: str
    report_date: str
    submitted_at: str
    title_summary: str
    overall_risk: str
    triggered_checks: list[str]
    checks: list[CheckResult]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checks"] = [check.to_dict() for check in self.checks]
        return data


RISK_RANK = {"high": 0, "medium": 1, "low": 2, "normal": 3, "unknown": 4}


def highest_risk(levels) -> str:
    picked = None
    for level in levels:
        if picked is None or RISK_RANK.get(level, 99) < RISK_RANK.get(picked, 99):
            picked = level
    return picked or "normal"


def duplicate_usage_summary(cases: list[dict[str, Any]]) -> dict[str, int]:
    local_checked = 0
    llm_reviewed = 0
    degraded_local = 0
    for case in cases:
        duplicate_check = next(
            (check for check in case.get("checks", []) if check.get("checker_id") == "report_duplicate_check"),
            None,
        )
        if not duplicate_check:
            continue
        local_checked += 1
        for finding in duplicate_check.get("findings", []):
            details = finding.get("details") or {}
            if details.get("llm_reviewed"):
                llm_reviewed += 1
            if details.get("degraded_to_local"):
                degraded_local += 1
    return {
        "local_duplicate_checked_count": local_checked,
        "local_duplicate_total_count": len(cases),
        "llm_review_count": llm_reviewed,
        "degraded_local_count": degraded_local,
    }
