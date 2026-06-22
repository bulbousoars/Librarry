from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from librarry.config import AppConfig
from librarry.db import Database
from librarry.kindle import send_to_kindle
from librarry.metadata import optimize_ebook
from librarry.quality import detect_extension

log = logging.getLogger(__name__)

EBOOK_EXTS = {".epub", ".azw3", ".mobi", ".pdf", ".fb2", ".djvu"}


def _safe_name(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)).strip()[:180]


def _find_ebook_file(path: Path) -> Path | None:
    if path.is_file() and path.suffix.lower() in EBOOK_EXTS:
        return path
    if not path.exists():
        return None
    if path.is_dir():
        for ext in (".epub", ".azw3", ".mobi", ".pdf", ".fb2", ".djvu"):
            matches = sorted(path.rglob(f"*{ext}"))
            if matches:
                return matches[0]
    return None


def _place_file(src: Path, dest: Path, mode: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    if mode == "hardlink":
        try:
            os.link(src, dest)
            return
        except OSError:
            pass
    shutil.copy2(src, dest)


_STOPWORDS = {"the", "a", "an", "of", "and", "or", "to", "in", "for", "on", "vol", "book"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split() if len(t) > 1} - _STOPWORDS


def scan_library(cfg: AppConfig, db: Database, *, title_threshold: float = 0.85) -> dict[str, int]:
    """Mark wanted books that already exist on disk as imported.

    Matches each wanted book against existing ebook files by fuzzy token overlap
    of author + title against the file's relative path, so books already in the
    library are never re-downloaded. Never moves or deletes files.
    """
    library = cfg.library_dir
    index: list[tuple[Path, set[str]]] = []
    if library.exists():
        for f in library.rglob("*"):
            if f.is_file() and f.suffix.lower() in EBOOK_EXTS:
                rel = f.relative_to(library).with_suffix("")
                index.append((f, _tokens(str(rel))))

    matched = 0
    for book in db.list_by_status("wanted"):
        title_toks = _tokens(book.title)
        if not title_toks:
            continue
        author_toks = _tokens(book.author)
        hit: Path | None = None
        for path, toks in index:
            if len(title_toks & toks) / len(title_toks) < title_threshold:
                continue
            author_cov = len(author_toks & toks) / len(author_toks) if author_toks else 1.0
            if author_cov < 0.5:
                continue
            hit = path
            break
        if hit:
            ext = detect_extension(hit.name) or hit.suffix.lstrip(".").lower()
            db.mark_imported(book.id, str(hit), ext)
            try:
                db.set_size(book.id, hit.stat().st_size)
            except OSError:
                pass
            log.info("Already in library: %r -> %s", book.title, hit)
            matched += 1

    # backfill size-on-disk for already-imported books missing it
    backfilled = 0
    for book in db.list_by_status("imported"):
        if book.size_bytes or not book.library_path:
            continue
        p = Path(book.library_path)
        if p.is_file():
            try:
                db.set_size(book.id, p.stat().st_size)
                backfilled += 1
            except OSError:
                pass
    return {"matched": matched, "backfilled": backfilled, "library_files": len(index)}


def import_ready(cfg: AppConfig, db: Database) -> dict[str, int]:
    imported = skipped = failed = 0
    for book in db.list_by_status("snatched"):
        if cfg.max_imports_per_run and imported >= cfg.max_imports_per_run:
            log.info("Reached import cap (%d)", cfg.max_imports_per_run)
            break
        if not book.download_path:
            skipped += 1
            continue
        src_root = Path(book.download_path)
        ebook = _find_ebook_file(src_root)
        if not ebook:
            log.warning("No ebook file yet for %r at %s", book.title, src_root)
            skipped += 1
            continue
        ext = detect_extension(ebook.name) or ebook.suffix.lstrip(".").lower()
        if ext in cfg.quality.reject_extensions:
            db.mark_failed(book.id, f"rejected extension at import: {ext}")
            failed += 1
            continue
        author_dir = _safe_name(book.author)
        title_name = _safe_name(book.title)
        dest_dir = cfg.library_dir / author_dir
        dest_file = dest_dir / f"{title_name}.{ext}"
        try:
            _place_file(ebook, dest_file, cfg.import_file_mode)
            # Embed cover + metadata for clean Kindle/e-reader display.
            # This rewrites the file (new inode), leaving any seeding torrent untouched.
            try:
                result = optimize_ebook(cfg, book, dest_file)
                if result.get("optimized"):
                    log.info("Optimized metadata for %r (cover=%s)", book.title, result.get("cover"))
            except Exception as exc:
                log.warning("Metadata optimize error for %r: %s", book.title, exc)
            for p in (dest_dir, dest_file):
                if hasattr(os, "chown"):
                    try:
                        os.chown(p, 1000, 1000)
                    except (PermissionError, OSError):
                        pass
            db.mark_imported(book.id, str(dest_file), ext)
            log.info("Imported %r -> %s", book.title, dest_file)
            try:
                send_to_kindle(cfg, dest_file, title=book.title, author=book.author)
            except Exception as exc:
                log.error("Kindle send failed for %r: %s", book.title, exc)
            imported += 1
        except Exception as exc:
            log.error("Import failed for %r: %s", book.title, exc)
            db.mark_failed(book.id, str(exc))
            failed += 1
    return {"imported": imported, "skipped": skipped, "failed": failed}
