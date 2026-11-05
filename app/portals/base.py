"""Shared plumbing for portal bots: typed failure modes, retry policy, snapshot shape.

Failure taxonomy drives what happens next:
  TransientPortalError / Playwright timeout -> retry with exponential backoff + jitter
  SessionExpired                            -> re-login, then retry
  LoginFailed                               -> stop, critical alert (credentials rotated?)
  OtpUnavailable                            -> stop, route to manual review (no OTP within window)
  LayoutChanged                             -> stop, screenshot + alert (portal redesigned; fix selectors)
"""
from __future__ import annotations

import random
import re
import time
from typing import Callable, TypeVar

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

T = TypeVar("T")


class PortalError(Exception):
    pass


class TransientPortalError(PortalError):
    pass


class SessionExpired(PortalError):
    pass


class LoginFailed(PortalError):
    pass


class OtpUnavailable(PortalError):
    pass


class LayoutChanged(PortalError):
    pass


def parse_amount(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d.]", "", text)
    if not digits or digits == ".":
        return None
    return int(float(digits))


def with_retries(fn: Callable[[], T], *, log: Callable[[str], None], what: str, attempts: int = 4,
                 base_delay: float = 0.6, on_session_expired: Callable[[], None] | None = None) -> T:
    for i in range(1, attempts + 1):
        try:
            return fn()
        except SessionExpired:
            if not on_session_expired or i == attempts:
                raise
            log(f"{what}: session expired - logging in again (attempt {i}/{attempts})")
            on_session_expired()
        except (TransientPortalError, PlaywrightTimeout) as exc:
            if i == attempts:
                raise TransientPortalError(f"{what} failed after {attempts} attempts: {exc}") from exc
            delay = base_delay * 2 ** (i - 1) + random.uniform(0, 0.3)
            log(f"{what}: transient error ({type(exc).__name__}: {str(exc)[:80]}) - retry in {delay:.1f}s")
            time.sleep(delay)
    raise AssertionError("unreachable")


class PortalAdapter:
    key: str = ""
    name: str = ""

    def __init__(self, base_url: str, creds: dict, otp, log: Callable[[str], None]):
        self.base = base_url.rstrip("/")
        self.creds = creds
        self.otp = otp
        self.log = log

    def login(self, page: Page) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def fetch(self, page: Page, refs: list[str]) -> list[dict]:  # pragma: no cover - interface
        """Return snapshots: {portal_ref, raw_status, approved_amount, query_text, patient_name, policy_number}"""
        raise NotImplementedError
