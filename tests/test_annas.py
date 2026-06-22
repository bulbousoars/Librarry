import librarry.workers.annas as an

MD5 = "b" * 32
# Anna's wraps results in HTML comments; include them to prove we unwrap.
SAMPLE_HTML = (
    "<div><!-- lazy -->"
    f'<a href="/md5/{MD5}"><div><h3>Dune</h3>'
    "<div>Frank Herbert, Penguin, 2020, English, epub, 2.3MB</div></div></a>"
    "<!-- end -->"
    "</div>"
)


class _Resp:
    status_code = 200
    text = SAMPLE_HTML


def test_annas_search_unwraps_comments_and_parses(monkeypatch):
    monkeypatch.setattr(an.SESSION, "get", lambda *a, **k: _Resp())
    out = an.annas_search(None, "Frank Herbert", "Dune")
    assert out, "expected a candidate"
    c = out[0]
    assert c["indexer"] == "Anna's Archive"
    assert c["protocol"] == "direct"
    assert c["download_url"] == f"annas:{MD5}:epub"
    assert c["size_bytes"] == int(2.3 * 1024 * 1024)
    assert c["rejected"] is False


def test_annas_search_network_error_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(an.SESSION, "get", boom)
    assert an.annas_search(None, "x", "y") == []


def test_annas_grab_requires_api_key():
    class Cfg:
        annas_api_key = ""
    try:
        an.grab_md5(Cfg(), None, "id", MD5, "epub", "Dune")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "API key" in str(exc)
