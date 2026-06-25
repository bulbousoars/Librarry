import time

import librarry.hardcover as hc


def test_rate_limiter_min_interval_blocks_then_allows():
    lim = hc.RateLimiter(per_minute=600, min_interval=0.2)
    assert lim.acquire(block=False) is True            # first token free
    # second immediate call is blocked by the min interval
    assert lim.acquire(block=False) is False
    # blocking call should succeed after waiting out the interval
    start = time.monotonic()
    assert lim.acquire(block=True, timeout=2) is True
    assert time.monotonic() - start >= 0.15


def test_rate_limiter_capacity_caps_burst():
    lim = hc.RateLimiter(per_minute=3, min_interval=0.0)
    granted = sum(1 for _ in range(10) if lim.acquire(block=False))
    assert granted == 3            # only the 3-token burst is allowed
    assert lim.acquire(block=False) is False


def test_get_limiter_rebuilds_on_config_change():
    a = hc.get_limiter(60, 1.0)
    b = hc.get_limiter(60, 1.0)
    assert a is b                  # same config -> same instance
    c = hc.get_limiter(30, 0.5)
    assert c is not a              # changed config -> new instance


def test_request_raises_when_throttled(monkeypatch):
    class Cfg:
        hardcover_api_url = "https://example/graphql"
        hardcover_token = "t"
        hardcover_rate_limit_per_minute = 1
        hardcover_min_interval_seconds = 0.0

    posted = []
    monkeypatch.setattr(hc.requests, "post", lambda *a, **k: posted.append(1))
    # force a fresh limiter with capacity 1
    hc._limiter = None
    hc._limiter_key = None
    # exhaust the single token directly so the next request is throttled
    hc.get_limiter(1, 0.0).acquire(block=False)
    try:
        hc.request(Cfg(), "query{}", block=False)
        assert False, "expected HardcoverRateLimited"
    except hc.HardcoverRateLimited:
        pass
    assert posted == []            # never hit the network when throttled


class _Cfg:
    hardcover_api_url = "https://example/graphql"
    hardcover_token = "t"
    hardcover_rate_limit_per_minute = 60
    hardcover_min_interval_seconds = 0.0


def test_add_to_status_returns_user_book_id(monkeypatch):
    captured = {}

    def fake_request(cfg, query, variables=None, *, block=True, timeout=30):
        captured["query"] = query
        captured["variables"] = variables
        return {"data": {"insert_user_book": {"id": 4242, "error": None, "user_book": {"id": 4242}}}}

    monkeypatch.setattr(hc, "request", fake_request)
    ub_id = hc.add_to_status(_Cfg(), "12345", status_id=1)
    assert ub_id == "4242"
    assert captured["variables"] == {"book_id": 12345, "status_id": 1}
    assert "insert_user_book" in captured["query"]


def test_add_to_status_raises_on_graphql_errors(monkeypatch):
    monkeypatch.setattr(hc, "request", lambda *a, **k: {"errors": [{"message": "nope"}]})
    try:
        hc.add_to_status(_Cfg(), "1")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "nope" in str(exc)


def test_add_to_status_raises_on_payload_error(monkeypatch):
    monkeypatch.setattr(
        hc, "request",
        lambda *a, **k: {"data": {"insert_user_book": {"id": None, "error": "already exists", "user_book": None}}},
    )
    try:
        hc.add_to_status(_Cfg(), "1")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "already exists" in str(exc)


def test_add_to_status_rejects_non_numeric_id():
    try:
        hc.add_to_status(_Cfg(), "not-an-int")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "invalid" in str(exc).lower()


def test_find_author_profile_url_exact_match(monkeypatch):
    def fake_request(cfg, query, variables=None, *, block=True, timeout=30):
        return {"data": {"authors": [
            {"name": "Christopher Buehlman", "slug": "christopher-buehlman", "books_count": 12},
        ]}}

    monkeypatch.setattr(hc, "request", fake_request)
    url = hc.find_author_profile_url(_Cfg(), "Christopher Buehlman")
    assert url == "https://hardcover.app/authors/christopher-buehlman"


def test_find_author_profile_url_no_exact_match_returns_empty(monkeypatch):
    # A near-but-wrong match (the Joshua Yaffa problem) must not be surfaced.
    def fake_request(cfg, query, variables=None, *, block=True, timeout=30):
        return {"data": {"authors": [
            {"name": "Joshua Yaffa", "slug": "joshua-yaffa", "books_count": 3},
        ]}}

    monkeypatch.setattr(hc, "request", fake_request)
    assert hc.find_author_profile_url(_Cfg(), "Christopher Buehlman") == ""


def test_find_author_profile_url_no_token_returns_empty():
    class NoToken(_Cfg):
        hardcover_token = ""

    assert hc.find_author_profile_url(NoToken(), "Anyone") == ""


def test_find_author_profile_url_picks_most_prolific_on_ties(monkeypatch):
    def fake_request(cfg, query, variables=None, *, block=True, timeout=30):
        return {"data": {"authors": [
            {"name": "John Smith", "slug": "john-smith-2", "books_count": 1},
            {"name": "john smith", "slug": "john-smith", "books_count": 40},
        ]}}

    monkeypatch.setattr(hc, "request", fake_request)
    assert hc.find_author_profile_url(_Cfg(), "John Smith") == "https://hardcover.app/authors/john-smith"
