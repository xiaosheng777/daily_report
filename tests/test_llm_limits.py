from io import BytesIO

from src.llm.openai_compatible import OpenAICompatibleJudge
from src.worker import TASK_TIMEOUT_SECONDS


def test_llm_request_has_no_per_request_timeout(monkeypatch):
    open_kwargs = []

    class FakeOpener:
        def open(self, _request, **kwargs):
            open_kwargs.append(kwargs)
            return BytesIO(b'{"ok": true}')

    monkeypatch.setattr("src.llm.openai_compatible.urllib.request.build_opener", lambda *_args: FakeOpener())
    judge = OpenAICompatibleJudge({"base_url": "http://model", "chat_path": "/v1/chat/completions"})

    assert judge._post({"model": "test"}) == {"ok": True}
    assert open_kwargs == [{}]


def test_duplicate_task_has_one_fixed_one_hour_timeout():
    assert TASK_TIMEOUT_SECONDS == 60 * 60
