import types

import pytest

from librarry.clients.sabnzbd import SabnzbdClient
from librarry.config import SabnzbdConfig

VALID_NZB = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" '
    b'"http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">\n'
    b'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><head></head></nzb>'
)


def _cfg():
    return SabnzbdConfig(
        host="sab.local", port=8081, username="", password="",
        api_key="KEY", category="books",
    )


class _Resp:
    def __init__(self, *, content=b"", json_data=None, status=200, headers=None):
        self.content = content
        self._json = json_data
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, get_resp, post_resp):
        self._get_resp = get_resp
        self._post_resp = post_resp
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, timeout=None):
        self.get_calls.append({"url": url, "params": params})
        return self._get_resp

    def post(self, url, data=None, files=None, timeout=None):
        self.post_calls.append({"url": url, "data": data, "files": files})
        return self._post_resp


def test_add_url_fetches_nzb_and_uploads_via_addfile():
    get_resp = _Resp(content=VALID_NZB, headers={"content-type": "application/x-nzb"})
    post_resp = _Resp(json_data={"status": True, "nzo_ids": ["nzo-123"]})
    session = _FakeSession(get_resp, post_resp)
    client = SabnzbdClient(_cfg(), session=session)

    nzo = client.add_url("https://indexer/api?t=get&id=x&apikey=K", "Some Book", "books")

    assert nzo == "nzo-123"
    # Fetched the indexer URL itself...
    assert session.get_calls[0]["url"].startswith("https://indexer/api")
    # ...and submitted the bytes via addfile (not a SAB-side URL fetch).
    post = session.post_calls[0]
    assert post["data"]["mode"] == "addfile"
    assert post["data"]["cat"] == "books"
    assert post["files"]["name"][1] == VALID_NZB


def test_add_url_raises_clear_error_when_indexer_returns_non_nzb():
    # e.g. NZBGeek returns an HTML/limit page instead of an NZB
    junk = b"<html><body>API rate limit reached</body></html>"
    session = _FakeSession(_Resp(content=junk, headers={"content-type": "text/html"}), _Resp())
    client = SabnzbdClient(_cfg(), session=session)

    with pytest.raises(RuntimeError) as exc:
        client.add_url("https://indexer/api?t=get&id=x", "Book", "books")
    assert "NZB" in str(exc.value)
    assert session.post_calls == []  # never submitted junk to SAB


def test_add_url_raises_when_addfile_reports_failure():
    get_resp = _Resp(content=VALID_NZB)
    post_resp = _Resp(json_data={"status": False, "nzo_ids": []})
    session = _FakeSession(get_resp, post_resp)
    client = SabnzbdClient(_cfg(), session=session)

    with pytest.raises(RuntimeError) as exc:
        client.add_url("https://indexer/api?t=get&id=x", "Book", "books")
    assert "addfile" in str(exc.value)
