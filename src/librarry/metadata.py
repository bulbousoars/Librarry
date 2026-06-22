"""Ebook metadata optimization for clean display on Kindle / e-readers.

After a book is imported we embed the rich Hardcover metadata (title, author
with sort name, series + index, publisher, language, date, ISBN, genres,
description) and a cover image into the file so Send-to-Kindle and other
readers show everything correctly.

EPUB is handled in pure Python (rewriting the OPF + embedding the cover);
other formats (azw3/mobi) use Calibre's `ebook-meta` if it is on PATH.

The file is always rewritten to a temp file and atomically replaced, which
breaks any hardlink — so a still-seeding torrent's copy is never modified.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import requests

log = logging.getLogger(__name__)

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

UA = "Mozilla/5.0 (X11; Linux x86_64) librarry/0.1"


def _author_sort(author: str) -> str:
    parts = author.strip().split()
    return f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) >= 2 else author


def fetch_cover(isbns: list[str], session: requests.Session | None = None) -> bytes | None:
    sess = session or requests
    for isbn in [i for i in isbns if i]:
        try:
            r = sess.get(
                f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg",
                params={"default": "false"},
                headers={"User-Agent": UA},
                timeout=20,
            )
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and ctype.startswith("image") and len(r.content) > 2000:
                return r.content
        except Exception:
            continue
    return None


def _opf_path(zf: zipfile.ZipFile) -> str | None:
    try:
        root = ET.fromstring(zf.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError):
        return None
    rf = root.find(f".//{{{CONTAINER_NS}}}rootfile")
    return rf.get("full-path") if rf is not None else None


def optimize_epub(path: Path, book, cover: bytes | None) -> bool:
    """Rewrite an EPUB's OPF with full metadata + embed a cover. Returns True on change."""
    ET.register_namespace("opf", OPF_NS)
    ET.register_namespace("dc", DC_NS)
    raw = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as zin:
        opf_path = _opf_path(zin)
        if not opf_path or opf_path not in zin.namelist():
            return False
        try:
            tree = ET.fromstring(zin.read(opf_path))
        except ET.ParseError:
            return False
        meta = tree.find(f"{{{OPF_NS}}}metadata")
        manifest = tree.find(f"{{{OPF_NS}}}manifest")
        if meta is None or manifest is None:
            return False

        def _drop(ns: str, tag: str) -> None:
            for e in meta.findall(f"{{{ns}}}{tag}"):
                meta.remove(e)

        def _dc(tag: str, text, attrib: dict | None = None) -> None:
            if text in (None, ""):
                return
            el = ET.SubElement(meta, f"{{{DC_NS}}}{tag}")
            el.text = str(text)
            for k, v in (attrib or {}).items():
                el.set(k, v)

        _drop(DC_NS, "title")
        _dc("title", book.title)
        _drop(DC_NS, "creator")
        if book.author:
            cre = ET.SubElement(meta, f"{{{DC_NS}}}creator")
            cre.text = book.author
            cre.set(f"{{{OPF_NS}}}role", "aut")
            cre.set(f"{{{OPF_NS}}}file-as", _author_sort(book.author))
        _drop(DC_NS, "language")
        _dc("language", book.language or "en")
        if book.publisher:
            _drop(DC_NS, "publisher")
            _dc("publisher", book.publisher)
        if book.release_date:
            _drop(DC_NS, "date")
            _dc("date", book.release_date)
        if book.description:
            _drop(DC_NS, "description")
            _dc("description", book.description)
        if book.isbn_13 or book.isbn_10:
            _dc("identifier", book.isbn_13 or book.isbn_10, {f"{{{OPF_NS}}}scheme": "ISBN"})
        for g in [x.strip() for x in (book.genres or "").split(",") if x.strip()]:
            _dc("subject", g)

        # series (Calibre convention, read by Kindle tooling)
        for m in meta.findall(f"{{{OPF_NS}}}meta"):
            if m.get("name") in ("calibre:series", "calibre:series_index", "cover"):
                meta.remove(m)
        if book.series:
            sm = ET.SubElement(meta, f"{{{OPF_NS}}}meta")
            sm.set("name", "calibre:series")
            sm.set("content", str(book.series))
            if book.series_position is not None:
                idx = book.series_position
                idx = int(idx) if float(idx).is_integer() else idx
                si = ET.SubElement(meta, f"{{{OPF_NS}}}meta")
                si.set("name", "calibre:series_index")
                si.set("content", str(idx))

        cover_arcname = None
        if cover:
            opf_dir = os.path.dirname(opf_path)
            cover_name = "librarry-cover.jpg"
            cover_arcname = f"{opf_dir}/{cover_name}" if opf_dir else cover_name
            # remove any prior librarry cover item
            for it in list(manifest.findall(f"{{{OPF_NS}}}item")):
                if it.get("id") == "librarry-cover":
                    manifest.remove(it)
            item = ET.SubElement(manifest, f"{{{OPF_NS}}}item")
            item.set("id", "librarry-cover")
            item.set("href", cover_name)
            item.set("media-type", "image/jpeg")
            item.set("properties", "cover-image")  # EPUB3
            cm = ET.SubElement(meta, f"{{{OPF_NS}}}meta")  # EPUB2 (Kindle reads this)
            cm.set("name", "cover")
            cm.set("content", "librarry-cover")

        new_opf = ET.tostring(tree, encoding="utf-8", xml_declaration=True)

        tmp = path.with_name(path.name + ".tmp")
        with zipfile.ZipFile(tmp, "w") as zout:
            zout.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            for info in zin.infolist():
                if info.filename in ("mimetype", cover_arcname):
                    continue
                if info.filename == opf_path:
                    zout.writestr(info.filename, new_opf, compress_type=zipfile.ZIP_DEFLATED)
                else:
                    zout.writestr(info.filename, zin.read(info.filename))
            if cover and cover_arcname:
                zout.writestr(cover_arcname, cover, compress_type=zipfile.ZIP_DEFLATED)

    os.replace(tmp, path)  # atomic; breaks hardlink so a seeding torrent is untouched
    return True


def _optimize_with_calibre(path: Path, book, cover_file: Path | None) -> bool:
    exe = shutil.which("ebook-meta")
    if not exe:
        return False
    args = [exe, str(path), "--title", book.title, "--authors", book.author or "Unknown"]
    if book.series:
        args += ["--series", str(book.series)]
        if book.series_position is not None:
            args += ["--index", str(book.series_position)]
    if book.publisher:
        args += ["--publisher", book.publisher]
    if book.language:
        args += ["--language", book.language]
    if book.isbn_13 or book.isbn_10:
        args += ["--isbn", book.isbn_13 or book.isbn_10]
    if book.genres:
        args += ["--tags", book.genres]
    if book.description:
        args += ["--comments", book.description]
    if cover_file:
        args += ["--cover", str(cover_file)]
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=120)
        return True
    except Exception as exc:
        log.warning("ebook-meta failed for %s: %s", path.name, exc)
        return False


def optimize_ebook(cfg, book, path: Path) -> dict:
    """Embed metadata + cover for a freshly-imported book. Best-effort, never raises."""
    if not cfg.raw.get("import", {}).get("optimize_metadata", True):
        return {"optimized": False, "reason": "disabled"}
    ext = path.suffix.lower().lstrip(".")
    cover = fetch_cover([book.isbn_13, book.isbn_10])
    try:
        if ext == "epub":
            changed = optimize_epub(path, book, cover)
            return {"optimized": changed, "cover": bool(cover), "method": "epub"}
        # azw3/mobi/etc → Calibre if available
        cover_file = None
        if cover:
            cf = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            cf.write(cover)
            cf.close()
            cover_file = Path(cf.name)
        try:
            changed = _optimize_with_calibre(path, book, cover_file)
        finally:
            if cover_file:
                cover_file.unlink(missing_ok=True)
        return {"optimized": changed, "cover": bool(cover) and changed, "method": "ebook-meta" if changed else "skipped"}
    except Exception as exc:
        log.warning("Metadata optimization failed for %s: %s", path.name, exc)
        return {"optimized": False, "error": str(exc)}
