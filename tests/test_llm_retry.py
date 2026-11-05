"""Retry policy for transient Gemini errors - no network, fake exceptions only."""
import pytest

from app import llm


class FakeAPIError(Exception):
    def __init__(self, code: int, msg: str = ""):
        super().__init__(f"{code} {msg}")
        self.code = code


def _flaky(failures: list[Exception], result="ok"):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if failures:
            raise failures.pop(0)
        return result
    return fn, calls


def test_retries_429_using_server_suggested_delay(monkeypatch):
    monkeypatch.setattr(llm, "MAX_RETRIES", 3)
    monkeypatch.setattr(llm, "RETRY_BUDGET_S", 60)
    slept = []
    fn, calls = _flaky([FakeAPIError(429, "Quota exceeded. Please retry in 15.3s.")])
    assert llm._call_with_retry(fn, task="t", sleep=slept.append) == "ok"
    assert calls["n"] == 2 and slept == [pytest.approx(15.8)]


def test_non_retryable_error_raises_immediately():
    fn, calls = _flaky([FakeAPIError(400, "bad request")])
    with pytest.raises(FakeAPIError):
        llm._call_with_retry(fn, task="t", sleep=lambda s: None)
    assert calls["n"] == 1


def test_gives_up_when_wait_exceeds_budget(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_BUDGET_S", 30)
    slept = []
    fn, calls = _flaky([FakeAPIError(429, "retry in 59s")])
    with pytest.raises(FakeAPIError):
        llm._call_with_retry(fn, task="t", sleep=slept.append)
    assert calls["n"] == 1 and slept == []


def test_fallback_is_counted(monkeypatch):
    monkeypatch.setattr(llm, "_get_client", lambda: object())
    llm.stats.clear()

    def boom():
        raise FakeAPIError(400)
    result, engine = llm.run_with_fallback("x", boom, lambda: "rules")
    assert (result, engine) == ("rules", "deterministic (llm-error fallback)")
    assert llm.stats["x:fallback"] == 1
