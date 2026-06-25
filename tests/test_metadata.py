import io
import types
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from librarry.metadata import _author_sort, _language_code, optimize_epub

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"

MINIMAL_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:identifier id="bookid">urn:uuid:1234</dc:identifier>
    <dc:title>Old Title</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def _make_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", MINIMAL_OPF)
        z.writestr("OEBPS/ch1.xhtml", "<html><body>hi</body></html>")


def _book(**kw):
    base = dict(
        title="The Way of Kings", author="Brandon Sanderson", language="English",
        publisher="Tor Books", release_date="2010-08-31", description="Epic fantasy.",
        isbn_13="9780765326355", isbn_10="0765326353", genres="Fantasy, Fiction",
        series="The Stormlight Archive", series_position=1.0,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_optimize_epub_embeds_metadata_and_cover(tmp_path):
    epub = tmp_path / "book.epub"
    _make_epub(epub)
    cover = b"\xff\xd8\xff" + b"x" * 3000  # fake jpeg bytes

    assert optimize_epub(epub, _book(), cover) is True

    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        assert names[0] == "mimetype"  # still first
        assert "OEBPS/librarry-cover.jpg" in names
        assert z.read("OEBPS/librarry-cover.jpg") == cover
        opf = ET.fromstring(z.read("OEBPS/content.opf"))

    meta = opf.find(f"{{{OPF_NS}}}metadata")
    assert meta.find(f"{{{DC_NS}}}title").text == "The Way of Kings"
    creator = meta.find(f"{{{DC_NS}}}creator")
    assert creator.text == "Brandon Sanderson"
    assert creator.get(f"{{{OPF_NS}}}file-as") == "Sanderson, Brandon"
    assert meta.find(f"{{{DC_NS}}}publisher").text == "Tor Books"
    subjects = {e.text for e in meta.findall(f"{{{DC_NS}}}subject")}
    assert "Fantasy" in subjects and "Fiction" in subjects
    metas = {(m.get("name"), m.get("content")) for m in meta.findall(f"{{{OPF_NS}}}meta")}
    assert ("calibre:series", "The Stormlight Archive") in metas
    assert ("calibre:series_index", "1") in metas
    assert ("cover", "librarry-cover") in metas


def test_optimize_epub_preserves_existing_cover_when_no_new_cover(tmp_path):
    # An EPUB that already carries its own cover pointer must not lose it when
    # we re-optimize and have no replacement cover (e.g. backfill, no ISBN hit).
    opf = MINIMAL_OPF.replace(
        "  </metadata>",
        '    <meta name="cover" content="origcover"/>\n  </metadata>',
    ).replace(
        '<item id="ch1"',
        '<item id="origcover" href="orig.jpg" media-type="image/jpeg"/>\n    <item id="ch1"',
    )
    epub = tmp_path / "book.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/ch1.xhtml", "<html><body>hi</body></html>")
        z.writestr("OEBPS/orig.jpg", b"\xff\xd8\xff" + b"o" * 100)

    optimize_epub(epub, _book(), None)  # no new cover provided

    with zipfile.ZipFile(epub) as z:
        opf_out = ET.fromstring(z.read("OEBPS/content.opf"))
        assert "OEBPS/orig.jpg" in z.namelist()
    metas = {(m.get("name"), m.get("content")) for m in opf_out.iter(f"{{{OPF_NS}}}meta")}
    assert ("cover", "origcover") in metas  # original cover pointer preserved


def test_optimize_epub_honors_cover_mime(tmp_path):
    epub = tmp_path / "b.epub"
    _make_epub(epub)
    optimize_epub(epub, _book(), b"\x89PNG\r\n" + b"x" * 3000, "image/png")
    with zipfile.ZipFile(epub) as z:
        assert "OEBPS/librarry-cover.png" in z.namelist()
        opf = ET.fromstring(z.read("OEBPS/content.opf"))
    item = next(i for i in opf.iter(f"{{{OPF_NS}}}item") if i.get("id") == "librarry-cover")
    assert item.get("media-type") == "image/png"
    assert item.get("href") == "librarry-cover.png"


def test_optimize_ebook_prefers_hardcover_cover_url(tmp_path, monkeypatch):
    import librarry.metadata as m
    calls = {"url": 0, "isbn": 0}

    def fake_url(url, session=None):
        calls["url"] += 1
        return (b"\x89PNG\r\n" + b"x" * 3000, "image/png")

    def fake_isbn(isbns, session=None):
        calls["isbn"] += 1
        return b"\xff\xd8\xff" + b"x" * 3000

    monkeypatch.setattr(m, "fetch_cover_url", fake_url)
    monkeypatch.setattr(m, "fetch_cover", fake_isbn)
    epub = tmp_path / "b.epub"
    _make_epub(epub)
    cfg = types.SimpleNamespace(raw={})
    res = m.optimize_ebook(cfg, _book(cover_url="https://hc/cover.png"), epub)
    assert res["optimized"] and res["cover"]
    assert calls["url"] == 1 and calls["isbn"] == 0  # cover_url wins; no OL fallback
    with zipfile.ZipFile(epub) as z:
        assert "OEBPS/librarry-cover.png" in z.namelist()


def test_optimize_ebook_falls_back_to_isbn_cover(tmp_path, monkeypatch):
    import librarry.metadata as m
    monkeypatch.setattr(m, "fetch_cover_url", lambda *a, **k: None)
    monkeypatch.setattr(m, "fetch_cover", lambda *a, **k: b"\xff\xd8\xff" + b"x" * 3000)
    epub = tmp_path / "b.epub"
    _make_epub(epub)
    cfg = types.SimpleNamespace(raw={})
    res = m.optimize_ebook(cfg, _book(cover_url=""), epub)
    assert res["cover"]
    with zipfile.ZipFile(epub) as z:
        assert "OEBPS/librarry-cover.jpg" in z.namelist()


def test_optimize_epub_no_cover_still_sets_metadata(tmp_path):
    epub = tmp_path / "book.epub"
    _make_epub(epub)
    assert optimize_epub(epub, _book(series=None, series_position=None), None) is True
    with zipfile.ZipFile(epub) as z:
        assert "OEBPS/librarry-cover.jpg" not in z.namelist()
        opf = ET.fromstring(z.read("OEBPS/content.opf"))
    meta = opf.find(f"{{{OPF_NS}}}metadata")
    assert meta.find(f"{{{DC_NS}}}title").text == "The Way of Kings"


def test_author_sort():
    assert _author_sort("Brandon Sanderson") == "Sanderson, Brandon"
    assert _author_sort("Plato") == "Plato"
    assert _author_sort("Ursula K Le Guin") == "Guin, Ursula K Le"


def test_language_code_normalizes_names_and_passes_codes():
    assert _language_code("English") == "en"
    assert _language_code("german") == "de"
    assert _language_code("French") == "fr"
    assert _language_code("") == "en"          # default
    assert _language_code(None) == "en"
    assert _language_code("en") == "en"         # already a code
    assert _language_code("eng") == "eng"       # ISO-639-2 passthrough
    assert _language_code("en-US") == "en-US"   # BCP-47 passthrough


def test_optimize_epub_writes_iso_language_code(tmp_path):
    epub = tmp_path / "book.epub"
    _make_epub(epub)
    optimize_epub(epub, _book(language="English"), None)
    with zipfile.ZipFile(epub) as z:
        opf = ET.fromstring(z.read("OEBPS/content.opf"))
    lang = opf.find(f"{{{OPF_NS}}}metadata").find(f"{{{DC_NS}}}language")
    assert lang.text == "en"   # not "English"

    epub2 = tmp_path / "book2.epub"
    _make_epub(epub2)
    optimize_epub(epub2, _book(language="German"), None)
    with zipfile.ZipFile(epub2) as z:
        opf = ET.fromstring(z.read("OEBPS/content.opf"))
    lang = opf.find(f"{{{OPF_NS}}}metadata").find(f"{{{DC_NS}}}language")
    assert lang.text == "de"


def _make_pdf(path, pages=2):
    import fitz
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=400, height=600)
    doc.save(str(path))
    doc.close()


def _png_cover():
    import fitz
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 60))
    pix.clear_with(180)
    return pix.tobytes("png")


def test_optimize_pdf_prepends_cover_and_sets_metadata(tmp_path):
    import fitz
    from librarry.metadata import optimize_pdf

    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf, pages=2)

    assert optimize_pdf(pdf, _book(), _png_cover()) is True

    doc = fitz.open(pdf)
    try:
        assert doc.page_count == 3  # original 2 + prepended cover page
        assert doc.metadata.get("title") == "The Way of Kings"
        assert doc.metadata.get("author") == "Brandon Sanderson"
        # the new first page actually carries an image (the cover)
        assert doc[0].get_images()
    finally:
        doc.close()


def test_optimize_pdf_is_idempotent(tmp_path):
    import fitz
    from librarry.metadata import optimize_pdf

    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf, pages=1)

    assert optimize_pdf(pdf, _book(), _png_cover()) is True
    # Second run must NOT add a second cover page (we stamped it).
    optimize_pdf(pdf, _book(), _png_cover())
    doc = fitz.open(pdf)
    try:
        assert doc.page_count == 2  # 1 original + exactly 1 cover, not 3
    finally:
        doc.close()


def test_optimize_pdf_without_cover_still_sets_metadata(tmp_path):
    import fitz
    from librarry.metadata import optimize_pdf

    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf, pages=1)
    assert optimize_pdf(pdf, _book(), None) is True
    doc = fitz.open(pdf)
    try:
        assert doc.page_count == 1  # no cover page added
        assert doc.metadata.get("title") == "The Way of Kings"
    finally:
        doc.close()


def test_optimize_ebook_routes_pdf(tmp_path, monkeypatch):
    import librarry.metadata as m

    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf, pages=1)
    monkeypatch.setattr(m, "fetch_cover_url", lambda *a, **k: (_png_cover(), "image/png"))
    monkeypatch.setattr(m, "fetch_cover", lambda *a, **k: None)
    cfg = types.SimpleNamespace(raw={})
    res = m.optimize_ebook(cfg, _book(cover_url="https://hc/cover.png"), pdf)
    assert res["method"] == "pdf" and res["optimized"] and res["cover"]
