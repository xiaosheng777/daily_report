from io import BytesIO

import json
import os
import threading
import time

from src.llm.limiter import GlobalLLMPermitServer, LLMPermitClient
from src.llm.openai_compatible import OpenAICompatibleJudge


def test_llm_request_uses_configured_per_request_timeout(monkeypatch):
    open_kwargs = []

    class FakeOpener:
        def open(self, _request, **kwargs):
            open_kwargs.append(kwargs)
            return BytesIO(b'{"ok": true}')

    monkeypatch.setattr("src.llm.openai_compatible.urllib.request.build_opener", lambda *_args: FakeOpener())
    judge = OpenAICompatibleJudge({"base_url": "http://model", "chat_path": "/v1/chat/completions"})

    assert judge._post({"model": "test"}) == {"ok": True}
    assert open_kwargs == [{"timeout": 180}]


def test_llm_prompt_only_contains_duplicate_relevant_report_fields():
    judge = OpenAICompatibleJudge({"base_url": "http://model", "chat_path": "/v1/chat/completions"})
    payload = judge._build_payload(
        {"report_id": "r1", "employee_name": "张三", "report_date": "2026-08-11", "title_summary": "标题", "work_description": "正文", "code_items": ["large"]},
        {"report_id": "r2", "employee_name": "李四", "report_date": "2026-08-10", "title_summary": "历史", "work_description": "历史正文", "stored_path": "secret"},
        {"candidate_score": 0.9},
    )
    user_payload = json.loads(payload["messages"][1]["content"])

    assert set(user_payload["current_report"]) == {"report_id", "employee_name", "report_date", "title_summary", "work_description"}
    assert "code_items" not in user_payload["current_report"]
    assert "stored_path" not in user_payload["matched_report"]


def test_global_llm_permit_service_caps_concurrency_across_task_clients(monkeypatch):
    server = GlobalLLMPermitServer(2)
    server.start()
    try:
        for key, value in server.child_environment().items():
            monkeypatch.setenv(key, value)
        active = 0
        maximum = 0
        lock = threading.Lock()

        def call(task_id):
            nonlocal active, maximum
            client = LLMPermitClient(task_id, 2)
            with client.acquire():
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with lock:
                    active -= 1

        threads = [threading.Thread(target=call, args=("task-a" if index % 2 else "task-b",)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert maximum == 2
    finally:
        server.stop()
