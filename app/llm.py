"""Thin LLM layer over Gemini 2.5 Flash (Vertex AI or AI Studio key).

Every AI feature in this repo is written as *two* implementations:
  * a Gemini implementation (structured JSON output against a Pydantic schema), and
  * a deterministic fallback (regex / rules / templates).

`run_with_fallback` tries Gemini first and silently degrades to the deterministic path
if no credentials are configured, the call fails, or the output fails validation.
That keeps the hospital workflow alive during an LLM outage and gives evals a baseline.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from .config import settings
from .db import get_db, now

log = logging.getLogger(__name__)
T = TypeVar("T")

_client = None
_client_error: str | None = None

# Retry policy for transient Gemini errors (429 quota / 5xx overload). The budget caps the *total*
# time one call may spend waiting, so an interactive request never hangs for minutes; batch jobs
# (evals) can raise it. Exhausting the budget re-raises, and run_with_fallback degrades as usual.
RETRYABLE_CODES = {429, 500, 503, 504}
MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
RETRY_BUDGET_S = float(os.getenv("LLM_RETRY_BUDGET_S", "30"))

# Per-process outcome counters: {"<task>:gemini" | "<task>:fallback" | "<task>:retry": n}.
# Lets evals (and the dashboard) tell a genuine Gemini result from a silent fallback.
stats: Counter = Counter()

_RETRY_DELAY_RE = re.compile(r"(?:retry in |retryDelay['\"]?:\s*['\"])([\d.]+)s", re.I)


def _status_code(exc: Exception) -> int | None:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _retry_delay(exc: Exception, attempt: int) -> float:
    """Server-suggested delay (Gemini puts it in the error body), else exponential backoff."""
    m = _RETRY_DELAY_RE.search(str(exc))
    return float(m.group(1)) + 0.5 if m else min(2.0 * 2 ** attempt, 20.0)


def _call_with_retry(fn: Callable[[], T], *, task: str, sleep: Callable[[float], None] = time.sleep) -> T:
    waited = 0.0
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            if _status_code(exc) not in RETRYABLE_CODES or attempt == MAX_RETRIES:
                raise
            delay = _retry_delay(exc, attempt)
            if waited + delay > RETRY_BUDGET_S:
                raise
            stats[f"{task}:retry"] += 1
            log.info("LLM task %s got %s, retrying in %.1fs (attempt %d)", task, _status_code(exc), delay, attempt + 1)
            sleep(delay)
            waited += delay
    raise AssertionError("unreachable")


def _get_client():
    global _client, _client_error
    if _client is not None or _client_error is not None:
        return _client
    if settings.llm_disabled:
        _client_error = "LLM_DISABLED=1"
        return None
    try:
        from google import genai

        if settings.use_vertex and settings.gcp_project:
            _client = genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gcp_location)
        elif settings.gemini_api_key:
            _client = genai.Client(api_key=settings.gemini_api_key)
        else:
            _client_error = "no GEMINI_API_KEY / Vertex project configured"
    except Exception as exc:  # pragma: no cover - import/auth problems
        _client_error = f"client init failed: {exc}"
    return _client


def engine_info() -> dict:
    client = _get_client()
    if client is None:
        return {"engine": "deterministic", "model": None, "reason": _client_error}
    mode = "vertex-ai" if settings.use_vertex else "ai-studio"
    return {"engine": "gemini", "model": settings.gemini_model, "mode": mode}


def generate_json(prompt: str, schema: type[BaseModel], *, task: str, system: str | None = None,
                  files: list[tuple[bytes, str]] | None = None, temperature: float = 0.1) -> dict:
    """Call Gemini with a response schema and return a validated dict. Raises on failure."""
    from google.genai import types

    client = _get_client()
    if client is None:
        raise RuntimeError(_client_error or "LLM unavailable")
    contents: list[Any] = [types.Part.from_bytes(data=b, mime_type=m) for b, m in (files or [])]
    contents.append(prompt)
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    t0 = time.perf_counter()
    resp = _call_with_retry(
        lambda: client.models.generate_content(model=settings.gemini_model, contents=contents, config=cfg), task=task)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    usage = getattr(resp, "usage_metadata", None)
    _log_call(task, latency_ms, usage)
    data = resp.parsed.model_dump() if isinstance(resp.parsed, BaseModel) else json.loads(resp.text)
    return schema.model_validate(data).model_dump()


def _log_call(task: str, latency_ms: int, usage) -> None:
    try:
        get_db().llm_calls.insert_one({
            "task": task, "model": settings.gemini_model, "latency_ms": latency_ms, "at": now(),
            "input_tokens": getattr(usage, "prompt_token_count", None),
            "output_tokens": getattr(usage, "candidates_token_count", None),
        })
    except Exception:  # observability must never break the workflow
        pass


def run_with_fallback(task: str, llm_fn: Callable[[], T], fallback_fn: Callable[[], T]) -> tuple[T, str]:
    """Return (result, engine_label)."""
    if _get_client() is not None:
        try:
            result = llm_fn()
            stats[f"{task}:gemini"] += 1
            return result, settings.gemini_model
        except Exception as exc:
            stats[f"{task}:fallback"] += 1
            log.warning("LLM task %s failed, using deterministic fallback: %s", task, exc)
            return fallback_fn(), "deterministic (llm-error fallback)"
    return fallback_fn(), "deterministic"
