from __future__ import annotations

import json
import logging
from pathlib import Path

from librarry import hardcover
from librarry.config import AppConfig
from librarry.db import Database
from librarry.secrets_resolver import warn_if_jwt_expiring

log = logging.getLogger(__name__)

HARDCOVER_QUERY = """
query {
  me {
    user_books(where: {status_id: {_eq: %d}}) {
      id
        book {
        id
        slug
        title
        subtitle
        pages
        rating
        ratings_count
        release_date
        release_year
        description
        image { url }
        cached_tags
        contributions { author { name } }
        book_series { position series { name } }
        default_ebook_edition { isbn_10 isbn_13 pages language { language } publisher { name } }
        default_physical_edition { isbn_10 isbn_13 pages language { language } publisher { name } }
      }
    }
  }
}
"""


def _genres(cached_tags, top: int = 3) -> str | None:
    if not isinstance(cached_tags, dict):
        return None
    names = [t.get("tag") for t in (cached_tags.get("Genre") or [])[:top] if t.get("tag")]
    return ", ".join(names) or None


def _edition_value(book: dict, field: str):
    for key in ("default_ebook_edition", "default_physical_edition"):
        ed = book.get(key) or {}
        val = ed.get(field)
        if val:
            return val
    return None


def _nested_value(book: dict, field: str, sub: str):
    for key in ("default_ebook_edition", "default_physical_edition"):
        ed = book.get(key) or {}
        val = (ed.get(field) or {}).get(sub)
        if val:
            return val
    return None


def _build_metadata(book: dict) -> dict:
    series_list = book.get("book_series") or []
    series_name = series_pos = None
    if series_list:
        first = series_list[0]
        series_name = (first.get("series") or {}).get("name")
        series_pos = first.get("position")
    publisher = _nested_value(book, "publisher", "name")
    desc = (book.get("description") or "").strip() or None
    return {
        "subtitle": book.get("subtitle"),
        "series": series_name,
        "series_position": float(series_pos) if series_pos is not None else None,
        "genres": _genres(book.get("cached_tags")),
        "rating": round(book["rating"], 2) if book.get("rating") else None,
        "ratings_count": book.get("ratings_count"),
        "pages": book.get("pages") or _edition_value(book, "pages"),
        "isbn_10": _edition_value(book, "isbn_10"),
        "isbn_13": _edition_value(book, "isbn_13"),
        "language": _nested_value(book, "language", "language"),
        "publisher": publisher.strip() if publisher else None,
        "release_date": book.get("release_date"),
        "release_year": book.get("release_year"),
        "description": desc[:1500] if desc else None,
        "hardcover_slug": book.get("slug"),
        "cover_url": (book.get("image") or {}).get("url"),
    }


def _state_path(cfg: AppConfig) -> Path:
    return cfg.state_dir / "hardcover_sync.json"


def sync_hardcover(cfg: AppConfig, db: Database) -> dict[str, int]:
    if not cfg.hardcover_token:
        raise RuntimeError(
            "Hardcover token not configured. Run: librarry secrets set hardcover_token"
        )
    warn_if_jwt_expiring(cfg.hardcover_token, "Hardcover token")

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    state_file = _state_path(cfg)
    state = json.loads(state_file.read_text()) if state_file.exists() else {"processed": []}
    processed = set(state.get("processed", []))

    query = HARDCOVER_QUERY % cfg.hardcover_want_status_id
    body = hardcover.request(cfg, query, block=True, timeout=30)
    if "errors" in body:
        raise RuntimeError(body["errors"])

    user_books = body["data"]["me"][0]["user_books"]
    new = skipped = failed = 0

    for ub in user_books:
        ub_id = str(ub["id"])
        book = ub["book"]
        hc_book_id = str(book["id"])
        title = book["title"]
        authors = [c["author"]["name"] for c in book.get("contributions", []) if c.get("author")]
        author = authors[0] if authors else "Unknown"
        meta = _build_metadata(book)

        try:
            if ub_id not in processed:
                added = db.upsert_wanted(
                    book_id=hc_book_id,
                    hardcover_user_book_id=ub_id,
                    hardcover_book_id=hc_book_id,
                    title=title,
                    author=author,
                )
                if added:
                    log.info("Queued wanted: %r by %r", title, author)
                    new += 1
                else:
                    log.info("Already snatched/imported: %r", title)
                processed.add(ub_id)
            else:
                skipped += 1
            # always refresh metadata for books we track (new or existing)
            db.set_metadata(hc_book_id, meta)
        except Exception as exc:
            log.error("Failed %r: %s", title, exc)
            failed += 1

    state["processed"] = sorted(processed)
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {"new": new, "skipped": skipped, "failed": failed}
