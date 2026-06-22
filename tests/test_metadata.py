import io
import types
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from librarry.metadata import _author_sort, optimize_epub

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
