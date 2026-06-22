import io
import types
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from librarry.db import Database
from librarry import metadata
from librarry.workers import import_book

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


def test_import_ready_optimizes_imported_epub_without_mutating_download(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads" / "book"
    download_dir.mkdir(parents=True)
    src = download_dir / "download.epub"
    _make_epub(src)
    before = src.read_bytes()

    db = Database(tmp_path / "state" / "librarry.db")
    db.init()
    book_id = db.add_manual("The Way of Kings", "Brandon Sanderson")
    db.set_metadata(book_id, {
        "language": "en",
        "publisher": "Tor Books",
        "isbn_13": "9780765326355",
        "series": "The Stormlight Archive",
        "series_position": 1.0,
        "genres": "Fantasy, Fiction",
    })
    db.mark_snatched(
        book_id,
        protocol="direct",
        source="libgen",
        indexer="LibGen",
        release_title="The Way of Kings [epub]",
        download_id="local",
        file_format="epub",
    )
    with db.connect() as conn:
        conn.execute("UPDATE books SET download_path=? WHERE id=?", (str(download_dir), book_id))

    cfg = types.SimpleNamespace(
        max_imports_per_run=10,
        quality=types.SimpleNamespace(reject_extensions=[]),
        library_dir=tmp_path / "library",
        import_file_mode="hardlink",
        raw={"import": {"optimize_metadata": True}},
        send_kindle=False,
    )
    monkeypatch.setattr(metadata, "fetch_cover", lambda isbns: b"\xff\xd8\xff" + b"x" * 3000)
    monkeypatch.setattr(import_book, "send_to_kindle", lambda *a, **k: None)

    assert import_book.import_ready(cfg, db) == {"imported": 1, "skipped": 0, "failed": 0}

    imported = db.list_by_status("imported")[0]
    dest = Path(imported.library_path)
    assert dest.read_bytes() != before
    assert src.read_bytes() == before

    with zipfile.ZipFile(dest) as z:
        assert "OEBPS/librarry-cover.jpg" in z.namelist()
        opf = ET.fromstring(z.read("OEBPS/content.opf"))
    meta = opf.find(f"{{{OPF_NS}}}metadata")
    assert meta.find(f"{{{DC_NS}}}title").text == "The Way of Kings"
    assert meta.find(f"{{{DC_NS}}}publisher").text == "Tor Books"
