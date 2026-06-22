import librarry.workers.libgen as lg

MD5 = "a" * 32
SAMPLE_HTML = (
    "<table>"
    f'<tr><td><a href="edition.php?id=6270247">Dune Saga Collection</a></td>'
    f'<td>Frank Herbert 2020 English 3 MB epub '
    f'<a href="ads.php?md5={MD5}">[get]</a></td></tr>'
    "</table>"
)


class _Resp:
    status_code = 200
    text = SAMPLE_HTML


def test_libgen_search_parses_candidates(monkeypatch):
    monkeypatch.setattr(lg, "get_host", lambda ttl=600: "libgen.vg")
    monkeypatch.setattr(lg.SESSION, "get", lambda *a, **k: _Resp())

    out = lg.libgen_search(None, "Frank Herbert", "Dune")
    assert out, "expected at least one candidate"
    c = out[0]
    assert c["indexer"] == "LibGen"
    assert c["protocol"] == "direct"
    assert c["download_url"] == f"libgen:libgen.vg:{MD5}:epub"
    assert c["size_bytes"] == 3 * 1024 * 1024
    assert c["rejected"] is False  # full title-token match


def test_libgen_search_no_host_returns_empty(monkeypatch):
    monkeypatch.setattr(lg, "get_host", lambda ttl=600: None)
    assert lg.libgen_search(None, "x", "y") == []


def test_libgen_isbn_search_is_exact(monkeypatch):
    # ISBN query returns a book whose title barely overlaps the search terms,
    # but an ISBN match must score 1.0 and not be rejected.
    # realistic libgen row: title anchor has a trailing <i></i> before </a>
    isbn_html = (
        "<table><tr><td>"
        '<a href="edition.php?id=9">Once Was Willem <i></i></a></td>'
        f'<td>x 1 MB epub <a href="ads.php?md5={"c"*32}">g</a></td></tr></table>'
    )
    calls = []

    def fake_fetch(req, retries=3):
        calls.append(req)
        return ("libgen.li", isbn_html) if req == "9780316505123" else None

    monkeypatch.setattr(lg, "get_host", lambda ttl=600: "libgen.li")
    monkeypatch.setattr(lg, "_fetch_search", fake_fetch)

    out = lg.libgen_search(None, "M.R. Carey", "Once Was Willem", isbns=["9780316505123"])
    assert len(out) == 1
    assert out[0]["score"] == 1.0 and out[0]["rejected"] is False
    assert out[0]["title"] == "Once Was Willem [epub]"  # real title, not "LibGen result"
    assert out[0]["download_url"] == f"libgen:libgen.li:{'c'*32}:epub"
    assert "9780316505123" in calls  # ISBN was searched first


def test_parse_size():
    assert lg._parse_size("foo 3 MB epub") == 3 * 1024 * 1024
    assert lg._parse_size("700 KB") == 700 * 1024
    assert lg._parse_size("no size here") is None
