"""One shared AsyncGroq + retry wrapper — SPEC §9.7.

Retry: 429 and 5xx, honour `retry-after` when present, else exponential
back-off (1, 2, 4, 8s), max 4 attempts. On a 429 the caller falls back once
to a secondary model (wired in llm.py / stt.py).
"""

import logging

import groq
import tenacity
from groq import AsyncGroq

from phathom.config import settings

log = logging.getLogger("phathom.ai")

_client: AsyncGroq | None = None


def get_client() -> AsyncGroq:
    global _client
    if _client is None:
        if not settings.GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY is missing — put it in .env")
        # max_retries=0: SPEC retry/fallback below is the only retry layer.
        _client = AsyncGroq(api_key=settings.GROQ_API_KEY, max_retries=0)
    return _client


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, groq.RateLimitError):
        return True
    if isinstance(exc, groq.APIStatusError):
        code = getattr(exc, "status_code", 0) or 0
        return code == 429 or code >= 500
    return False


def _wait(retry_state: tenacity.RetryCallState) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if exc is not None:
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None) or {}
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(str(retry_after).split(",")[0]))
            except ValueError:
                pass
    # exponential back-off 1, 2, 4, 8s
    return min(8.0, 2.0 ** max(0, retry_state.attempt_number - 1))


async def with_retry(fn, *, what: str):
    """Run `fn` (a no-arg async callable) with the SPEC retry policy."""
    async for attempt in tenacity.AsyncRetrying(
        retry=tenacity.retry_if_exception(_is_retryable),
        wait=_wait,
        stop=tenacity.stop_after_attempt(4),
        reraise=True,
        before_sleep=lambda s: log.warning(
            "%s attempt %d failed (%r); retrying", what, s.attempt_number, s.outcome.exception()
        ),
    ):
        with attempt:
            return await fn()
    raise AssertionError("unreachable")  # tenacity re-raises instead
