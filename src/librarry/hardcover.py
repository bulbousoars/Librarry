"""Shared Hardcover API client with a polite, process-wide rate limiter.

All Hardcover requests (sync and the web search) go through `request()` so a
single token-bucket throttle protects their server and keeps us under any
rate limit / ban threshold. Limits are configurable under `hardcover:` in
config.yaml (rate_limit_per_minute, min_interval_seconds).
"""

from __future__ import annotations

import threading
import time

import requests


class HardcoverRateLimited(RuntimeError):
    """Raised when a non-blocking request can't get a token in time."""


class RateLimiter:
    """Token bucket with an additional minimum interval between calls."""

    def __init__(self, per_minute: float, min_interval: float = 0.0):
        self.rate = max(per_minute, 1) / 60.0  # tokens per second
        self.capacity = max(1.0, float(int(per_minute) or 1))
        self.min_interval = max(0.0, min_interval)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.last_call = 0.0
        self.lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def acquire(self, *, block: bool = True, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                self._refill()
                now = time.monotonic()
                since_last = now - self.last_call
                if self.tokens >= 1.0 and since_last >= self.min_interval:
                    self.tokens -= 1.0
                    self.last_call = now
                    return True
                wait_token = (1.0 - self.tokens) / self.rate if self.tokens < 1.0 else 0.0
                wait_interval = self.min_interval - since_last
                wait = max(wait_token, wait_interval, 0.0)
            if not block:
                return False
            if time.monotonic() + wait > deadline:
                return False
            time.sleep(min(max(wait, 0.02), 0.5))


_limiter: RateLimiter | None = None
_limiter_key: tuple[int, float] | None = None
_limiter_lock = threading.Lock()


def get_limiter(per_minute: int, min_interval: float) -> RateLimiter:
    """Return the shared limiter, rebuilding it if the configured limits change."""
    global _limiter, _limiter_key
    key = (int(per_minute), float(min_interval))
    with _limiter_lock:
        if _limiter is None or _limiter_key != key:
            _limiter = RateLimiter(per_minute=key[0], min_interval=key[1])
            _limiter_key = key
        return _limiter


def request(cfg, query: str, variables: dict | None = None, *, block: bool = True, timeout: int = 30) -> dict:
    """POST a GraphQL query to Hardcover through the shared rate limiter.

    `block=True` waits for a slot (use for background sync); `block=False` raises
    HardcoverRateLimited immediately if over the limit (use for UI search).
    """
    limiter = get_limiter(cfg.hardcover_rate_limit_per_minute, cfg.hardcover_min_interval_seconds)
    if not limiter.acquire(block=block, timeout=timeout):
        raise HardcoverRateLimited(
            "Hardcover request throttled to protect their API — wait a moment and retry."
        )
    resp = requests.post(
        cfg.hardcover_api_url,
        json={"query": query, "variables": variables or {}},
        headers={
            "Authorization": f"Bearer {cfg.hardcover_token}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


_AUTHOR_QUERY = (
    "query($name:String!){ authors(where:{name:{_ilike:$name}}, limit:5)"
    "{ name slug books_count } }"
)


def find_author_profile_url(cfg, name: str, *, block: bool = False, timeout: int = 20) -> str:
    """Return the author's Hardcover profile URL, or "" if it can't be found.

    Only a case-insensitive *exact* name match is surfaced — a near-but-wrong
    match is never guessed (so we don't repeat the wrong-author-link problem).
    Returns "" when the token is unset, the request is throttled/fails, or no
    confident match exists. On multiple exact matches, the most prolific author
    (highest books_count) wins.
    """
    name = (name or "").strip()
    if not name or not getattr(cfg, "hardcover_token", ""):
        return ""
    try:
        body = request(cfg, _AUTHOR_QUERY, {"name": name}, block=block, timeout=timeout)
    except Exception:
        return ""
    if not isinstance(body, dict) or body.get("errors"):
        return ""
    authors = ((body.get("data") or {}).get("authors")) or []
    target = name.lower()
    matches = [
        a for a in authors
        if str(a.get("name") or "").strip().lower() == target and a.get("slug")
    ]
    if not matches:
        return ""
    best = max(matches, key=lambda a: a.get("books_count") or 0)
    return f"https://hardcover.app/authors/{best['slug']}"
