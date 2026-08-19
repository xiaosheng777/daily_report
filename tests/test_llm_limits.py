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


def test_global_llm_limit_can_resize_up_and_down_safely(monkeypatch):
    server = GlobalLLMPermitServer(1)
    server.start()
    try:
        for key, value in server.child_environment().items():
            monkeypatch.setenv(key, value)
        entered, release = threading.Event(), threading.Event()

        def hold(task_id):
            with LLMPermitClient(task_id, 4).acquire():
                entered.set()
                release.wait(1)

        first = threading.Thread(target=hold, args=("task-a",))
        first.start()
        assert entered.wait(1)
        second_entered = threading.Event()

        def second():
            with LLMPermitClient("task-b", 4).acquire():
                second_entered.set()

        waiting = threading.Thread(target=second)
        waiting.start()
        time.sleep(.03)
        assert not second_entered.is_set()
        server.resize(2)
        assert second_entered.wait(1)
        assert server.snapshot()["active_total"] <= 2

        # Existing holders are not revoked when the limit shrinks.
        server.resize(1)
        assert server.snapshot()["global_limit"] == 1
        release.set()
        first.join(1)
        waiting.join(1)
        assert server.snapshot()["active_total"] == 0
    finally:
        server.stop()


def test_permit_is_released_when_client_connection_dies(monkeypatch):
    server = GlobalLLMPermitServer(1)
    server.start()
    try:
        for key, value in server.child_environment().items():
            monkeypatch.setenv(key, value)
        permit = LLMPermitClient("task-a", 1).acquire()
        assert server.snapshot()["active_total"] == 1
        # Closing the IPC socket simulates abrupt Task-process death.  The
        # server owns release in its connection-finally block.
        permit._connection.close()
        deadline = time.monotonic() + 1
        while server.snapshot()["active_total"] and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.snapshot()["active_total"] == 0
    finally:
        server.stop()


def test_global_llm_limit_can_resize_down_safely(monkeypatch):
    server = GlobalLLMPermitServer(2)
    server.start()
    try:
        for key, value in server.child_environment().items():
            monkeypatch.setenv(key, value)
        both_active = threading.Barrier(3)
        release_first, release_second = threading.Event(), threading.Event()

        def hold(task_id, release):
            with LLMPermitClient(task_id, 2).acquire():
                both_active.wait()
                release.wait(1)

        first = threading.Thread(target=hold, args=("task-a", release_first))
        second = threading.Thread(target=hold, args=("task-b", release_second))
        first.start(); second.start()
        both_active.wait(timeout=1)
        server.resize(1)
        third_entered = threading.Event()

        def third_task():
            with LLMPermitClient("task-c", 1).acquire():
                third_entered.set()

        third = threading.Thread(target=third_task)
        third.start()
        time.sleep(.03)
        assert not third_entered.is_set()
        release_first.set()
        first.join(1)
        time.sleep(.03)
        assert not third_entered.is_set()
        release_second.set()
        second.join(1)
        assert third_entered.wait(1)
        third.join(1)
    finally:
        server.stop()


def test_llm_telemetry_starts_http_only_after_a_permit_is_acquired(monkeypatch):
    events, permit_events = [], []

    class FakePermit:
        def acquire(self):
            permit_events.append("acquire")
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            permit_events.append("release")

        @staticmethod
        def snapshot():
            return {"global_limit": 2, "active_total": 1, "waiting_total": 0, "active_by_task": {"task-a": 1}, "waiting_by_task": {}}

    judge = OpenAICompatibleJudge(
        {"base_url": "http://model", "chat_path": "/v1/chat/completions", "api_key": "key"},
        telemetry=lambda event, payload: events.append((event, payload)), task_id="task-a", permit_client=FakePermit(),
    )
    monkeypatch.setattr(judge, "_post", lambda _payload: {"choices": [{"message": {"content": '{"duplicate_risk":"normal"}'}}]})

    judge.judge_duplicate({"report_id": "a"}, {"report_id": "b"}, {})

    assert [event for event, _payload in events] == ["queued", "permit_acquired", "http_start", "http_finish"]
    assert permit_events == ["acquire", "release"]
    assert events[2][1]["permit_snapshot"]["active_total"] == 1
