from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    hardcover_user_book_id TEXT UNIQUE,
    hardcover_book_id TEXT,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'wanted',
    format TEXT,
    source TEXT,
    indexer TEXT,
    protocol TEXT,
    release_title TEXT,
    download_id TEXT,
    download_path TEXT,
    library_path TEXT,
    size_bytes INTEGER,
    last_error TEXT,
    wanted_at TEXT NOT NULL,
    snatched_at TEXT,
    imported_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_books_status ON books(status);
CREATE INDEX IF NOT EXISTS idx_books_download_id ON books(download_id);
CREATE INDEX IF NOT EXISTS idx_books_author ON books(author);

CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    client TEXT NOT NULL,
    download_id TEXT,
    release_title TEXT,
    status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(book_id) REFERENCES books(id)
);

CREATE TABLE IF NOT EXISTS book_extras (
    book_id TEXT PRIMARY KEY,
    notes TEXT,
    tags TEXT,
    cover_override_url TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS author_profiles (
    author TEXT PRIMARY KEY,
    profile TEXT,
    notes TEXT,
    tags TEXT,
    image_url TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS author_bibliography (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT NOT NULL,
    title TEXT NOT NULL,
    series TEXT,
    release_date TEXT,
    release_year INTEGER,
    category TEXT,
    genre TEXT,
    source TEXT,
    source_id TEXT,
    source_url TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(author, title, source)
);

CREATE INDEX IF NOT EXISTS idx_author_bibliography_author ON author_bibliography(author);

CREATE TABLE IF NOT EXISTS kindle_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT,
    title TEXT NOT NULL,
    author TEXT,
    kindle_to TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    source TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kindle_sends_created ON kindle_sends(created_at);
"""


# Optional metadata columns added after the initial release. Applied as
# idempotent ALTER TABLE … ADD COLUMN migrations in Database.init().
_EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("subtitle", "TEXT"),
    ("series", "TEXT"),
    ("series_position", "REAL"),
    ("genres", "TEXT"),
    ("rating", "REAL"),
    ("ratings_count", "INTEGER"),
    ("pages", "INTEGER"),
    ("isbn_10", "TEXT"),
    ("isbn_13", "TEXT"),
    ("language", "TEXT"),
    ("publisher", "TEXT"),
    ("release_date", "TEXT"),
    ("release_year", "INTEGER"),
    ("description", "TEXT"),
    ("hardcover_slug", "TEXT"),
    ("cover_url", "TEXT"),
]
_METADATA_FIELDS = {c for c, _ in _EXTRA_COLUMNS}

_AUTHOR_PROFILE_EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("total_books_written", "INTEGER"),
    ("nationality", "TEXT"),
    ("hometown", "TEXT"),
    ("source_url", "TEXT"),
    ("hardcover_url", "TEXT"),
]

_AUTHOR_BIBLIOGRAPHY_EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("series", "TEXT"),
    ("date_added", "TEXT"),
]


@dataclass
class Book:
    id: str
    hardcover_user_book_id: str | None
    hardcover_book_id: str | None
    title: str
    author: str
    status: str
    format: str | None = None
    source: str | None = None
    indexer: str | None = None
    protocol: str | None = None
    release_title: str | None = None
    download_id: str | None = None
    download_path: str | None = None
    library_path: str | None = None
    size_bytes: int | None = None
    last_error: str | None = None
    wanted_at: str | None = None
    snatched_at: str | None = None
    imported_at: str | None = None
    updated_at: str | None = None
    # metadata (from Hardcover)
    subtitle: str | None = None
    series: str | None = None
    series_position: float | None = None
    genres: str | None = None
    rating: float | None = None
    ratings_count: int | None = None
    pages: int | None = None
    isbn_10: str | None = None
    isbn_13: str | None = None
    language: str | None = None
    publisher: str | None = None
    release_date: str | None = None
    release_year: int | None = None
    description: str | None = None
    hardcover_slug: str | None = None
    cover_url: str | None = None


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        if readonly:
            uri = f"file:{self.path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=60)
        else:
            conn = sqlite3.connect(self.path, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=60000")
        try:
            yield conn
            if not readonly:
                conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
            for col, coltype in _EXTRA_COLUMNS:
                if col not in existing:
                    conn.execute(f"ALTER TABLE books ADD COLUMN {col} {coltype}")
            existing_profile = {row["name"] for row in conn.execute("PRAGMA table_info(author_profiles)")}
            for col, coltype in _AUTHOR_PROFILE_EXTRA_COLUMNS:
                if col not in existing_profile:
                    conn.execute(f"ALTER TABLE author_profiles ADD COLUMN {col} {coltype}")
            existing_bib = {row["name"] for row in conn.execute("PRAGMA table_info(author_bibliography)")}
            for col, coltype in _AUTHOR_BIBLIOGRAPHY_EXTRA_COLUMNS:
                if col not in existing_bib:
                    conn.execute(f"ALTER TABLE author_bibliography ADD COLUMN {col} {coltype}")
            # Backfill date_added for rows that predate the column (best-effort:
            # use their last-updated timestamp as the first-seen date).
            conn.execute(
                "UPDATE author_bibliography SET date_added=updated_at WHERE date_added IS NULL"
            )

    def upsert_wanted(
        self,
        *,
        book_id: str,
        hardcover_user_book_id: str,
        hardcover_book_id: str | None,
        title: str,
        author: str,
    ) -> bool:
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM books WHERE id=?", (book_id,)).fetchone()
            if row and row["status"] in ("snatched", "imported"):
                conn.execute(
                    "UPDATE books SET title=?, author=?, updated_at=? WHERE id=?",
                    (title, author, now, book_id),
                )
                return False
            conn.execute(
                """
                INSERT INTO books (
                    id, hardcover_user_book_id, hardcover_book_id, title, author,
                    status, wanted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'wanted', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    author=excluded.author,
                    hardcover_user_book_id=excluded.hardcover_user_book_id,
                    hardcover_book_id=excluded.hardcover_book_id,
                    status=CASE
                        WHEN books.status='imported' THEN books.status
                        WHEN books.status='snatched' THEN books.status
                        ELSE 'wanted'
                    END,
                    updated_at=excluded.updated_at
                """,
                (book_id, hardcover_user_book_id, hardcover_book_id, title, author, now, now),
            )
            return True

    def add_manual(self, title: str, author: str) -> str:
        """Add a book to the wanted queue by hand (not via Hardcover)."""
        title = title.strip()
        author = (author or "").strip() or "Unknown"
        if not title:
            raise ValueError("title is required")
        digest = hashlib.sha1(f"{title}|{author}".lower().encode()).hexdigest()[:16]
        book_id = f"manual:{digest}"
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO books (
                    id, hardcover_user_book_id, hardcover_book_id, title, author,
                    status, source, wanted_at, updated_at
                ) VALUES (?, NULL, NULL, ?, ?, 'wanted', 'manual', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    author=excluded.author,
                    updated_at=excluded.updated_at,
                    status=CASE
                        WHEN books.status IN ('imported', 'snatched') THEN books.status
                        ELSE 'wanted'
                    END
                """,
                (book_id, title, author, now, now),
            )
        return book_id

    def add_hardcover(self, hardcover_book_id: str, title: str, author: str) -> str:
        """Add a book by Hardcover book id (from search, not the Want-to-Read list)."""
        book_id = str(hardcover_book_id)
        title = title.strip()
        author = (author or "").strip() or "Unknown"
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO books (
                    id, hardcover_user_book_id, hardcover_book_id, title, author,
                    status, source, wanted_at, updated_at
                ) VALUES (?, NULL, ?, ?, ?, 'wanted', 'hardcover', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    author=excluded.author,
                    hardcover_book_id=excluded.hardcover_book_id,
                    updated_at=excluded.updated_at,
                    status=CASE
                        WHEN books.status IN ('imported', 'snatched') THEN books.status
                        ELSE 'wanted'
                    END
                """,
                (book_id, book_id, title, author, now, now),
            )
        return book_id

    def set_metadata(self, book_id: str, meta: dict) -> int:
        """Update metadata columns for a book (only known fields, skips None)."""
        cols = [(k, v) for k, v in meta.items() if k in _METADATA_FIELDS and v is not None]
        if not cols:
            return 0
        set_clause = ", ".join(f"{k}=?" for k, _ in cols)
        values = [v for _, v in cols] + [utcnow(), book_id]
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE books SET {set_clause}, updated_at=? WHERE id=?", values
            )
            return cur.rowcount

    def get_book_extras(self, book_id: str) -> dict:
        with self.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM book_extras WHERE book_id=?", (book_id,)).fetchone()
        return dict(row) if row else {
            "book_id": book_id,
            "notes": "",
            "tags": "",
            "cover_override_url": "",
            "updated_at": None,
        }

    def set_book_extras(self, book_id: str, *, notes: str = "", tags: str = "", cover_override_url: str = "") -> dict:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO book_extras (book_id, notes, tags, cover_override_url, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(book_id) DO UPDATE SET
                    notes=excluded.notes,
                    tags=excluded.tags,
                    cover_override_url=excluded.cover_override_url,
                    updated_at=excluded.updated_at
                """,
                (book_id, notes, tags, cover_override_url, now),
            )
        return self.get_book_extras(book_id)

    def get_author_profile(self, author: str) -> dict:
        with self.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM author_profiles WHERE author=?", (author,)).fetchone()
        return dict(row) if row else {
            "author": author,
            "profile": "",
            "notes": "",
            "tags": "",
            "image_url": "",
            "total_books_written": None,
            "nationality": "",
            "hometown": "",
            "source_url": "",
            "hardcover_url": "",
            "updated_at": None,
        }

    def set_author_profile(
        self,
        author: str,
        *,
        profile: str = "",
        notes: str = "",
        tags: str = "",
        image_url: str = "",
        total_books_written: int | None = None,
        nationality: str = "",
        hometown: str = "",
        source_url: str = "",
        hardcover_url: str = "",
    ) -> dict:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO author_profiles (
                    author, profile, notes, tags, image_url, total_books_written,
                    nationality, hometown, source_url, hardcover_url, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(author) DO UPDATE SET
                    profile=excluded.profile,
                    notes=excluded.notes,
                    tags=excluded.tags,
                    image_url=excluded.image_url,
                    total_books_written=excluded.total_books_written,
                    nationality=excluded.nationality,
                    hometown=excluded.hometown,
                    source_url=excluded.source_url,
                    hardcover_url=excluded.hardcover_url,
                    updated_at=excluded.updated_at
                """,
                (
                    author,
                    profile,
                    notes,
                    tags,
                    image_url,
                    total_books_written,
                    nationality,
                    hometown,
                    source_url,
                    hardcover_url,
                    now,
                ),
            )
        return self.get_author_profile(author)

    def list_authors(self) -> list[dict]:
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT
                    b.author,
                    COUNT(*) AS book_count,
                    SUM(CASE WHEN b.status='imported' THEN 1 ELSE 0 END) AS owned_count,
                    SUM(CASE WHEN b.status='wanted' THEN 1 ELSE 0 END) AS wanted_count,
                    SUM(CASE WHEN b.status='snatched' THEN 1 ELSE 0 END) AS in_progress_count,
                    AVG(b.rating) AS average_rating,
                    ap.profile,
                    ap.notes,
                    ap.tags,
                    ap.image_url,
                    ap.total_books_written,
                    ap.nationality,
                    ap.hometown,
                    ap.source_url,
                    ap.updated_at
                FROM books b
                LEFT JOIN author_profiles ap ON ap.author=b.author
                GROUP BY b.author
                ORDER BY b.author COLLATE NOCASE
                """
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["book_count"] = int(d.get("book_count") or 0)
            d["owned_count"] = int(d.get("owned_count") or 0)
            d["wanted_count"] = int(d.get("wanted_count") or 0)
            d["in_progress_count"] = int(d.get("in_progress_count") or 0)
            d["average_rating"] = (
                round(float(d["average_rating"]), 2)
                if d.get("average_rating") is not None
                else None
            )
            d["profile"] = d.get("profile") or ""
            d["notes"] = d.get("notes") or ""
            d["tags"] = d.get("tags") or ""
            d["image_url"] = d.get("image_url") or ""
            d["nationality"] = d.get("nationality") or ""
            d["hometown"] = d.get("hometown") or ""
            d["source_url"] = d.get("source_url") or ""
            out.append(d)
        return out

    def list_author_bibliography(self, author: str) -> list[dict]:
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM author_bibliography
                WHERE author=?
                ORDER BY COALESCE(release_year, 0) DESC, title COLLATE NOCASE
                """,
                (author,),
            ).fetchall()
        return [dict(r) for r in rows]

    def replace_author_bibliography(self, author: str, rows: list[dict]) -> list[dict]:
        now = utcnow()
        with self.connect() as conn:
            # Preserve each entry's first-seen date across the delete-then-insert
            # below, keyed the same way as the UNIQUE(author, title, source).
            prior_added = {
                (r["title"], r["source"] or ""): r["date_added"]
                for r in conn.execute(
                    "SELECT title, source, date_added FROM author_bibliography WHERE author=?",
                    (author,),
                )
            }
            # True replace: drop the author's prior rows so a re-poll removes
            # entries that have since been filtered out (e.g. foreign-only works).
            conn.execute("DELETE FROM author_bibliography WHERE author=?", (author,))
            for row in rows:
                date_added = prior_added.get((row.get("title", ""), row.get("source", "") or "")) or now
                conn.execute(
                    """
                    INSERT INTO author_bibliography (
                        author, title, series, release_date, release_year, category, genre,
                        source, source_id, source_url, date_added, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(author, title, source) DO UPDATE SET
                        series=excluded.series,
                        release_date=excluded.release_date,
                        release_year=excluded.release_year,
                        category=excluded.category,
                        genre=excluded.genre,
                        source_id=excluded.source_id,
                        source_url=excluded.source_url,
                        updated_at=excluded.updated_at
                    """,
                    (
                        author,
                        row.get("title", ""),
                        row.get("series", ""),
                        row.get("release_date"),
                        row.get("release_year"),
                        row.get("category", ""),
                        row.get("genre", ""),
                        row.get("source", ""),
                        row.get("source_id", ""),
                        row.get("source_url", ""),
                        date_added,
                        now,
                    ),
                )
        return self.list_author_bibliography(author)

    def log_kindle_send(
        self,
        *,
        title: str,
        author: str = "",
        book_id: str | None = None,
        kindle_to: str = "",
        status: str,
        detail: str = "",
        source: str = "",
    ) -> None:
        """Record a Send-to-Kindle attempt (sent / failed / skipped_*)."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO kindle_sends (
                    book_id, title, author, kindle_to, status, detail, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (book_id, title, author, kindle_to, status, detail, source, utcnow()),
            )

    def list_kindle_sends(self, limit: int = 100) -> list[dict]:
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM kindle_sends ORDER BY created_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_author_bibliography_item(self, author: str, title: str) -> dict | None:
        with self.connect(readonly=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM author_bibliography
                WHERE author=? AND title=?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (author, title),
            ).fetchone()
        return dict(row) if row else None

    def clear_file(self, book_id: str) -> None:
        """Delete-file-but-keep-request: reset to wanted, drop file/download state,
        keep the row, metadata, and Hardcover ids so it can be re-searched."""
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE books SET
                    status='wanted',
                    library_path=NULL,
                    size_bytes=NULL,
                    imported_at=NULL,
                    format=NULL,
                    download_path=NULL,
                    download_id=NULL,
                    release_title=NULL,
                    snatched_at=NULL,
                    last_error=NULL,
                    updated_at=?
                WHERE id=?
                """,
                (now, book_id),
            )

    def set_size(self, book_id: str, size_bytes: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE books SET size_bytes=? WHERE id=?", (size_bytes, book_id)
            )

    def delete(self, book_id: str) -> int:
        with self.connect() as conn:
            conn.execute("DELETE FROM download_history WHERE book_id=?", (book_id,))
            cur = conn.execute("DELETE FROM books WHERE id=?", (book_id,))
            return cur.rowcount

    def list_by_status(self, status: str) -> list[Book]:
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM books WHERE status=? ORDER BY wanted_at",
                (status,),
            ).fetchall()
        return [_row_to_book(r) for r in rows]

    def list_by_author(self, author: str) -> list[Book]:
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM books WHERE author=? ORDER BY release_year, title",
                (author,),
            ).fetchall()
        return [_row_to_book(r) for r in rows]

    def get_by_author_title(self, author: str, title: str) -> Book | None:
        with self.connect(readonly=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM books
                WHERE lower(author)=lower(?) AND lower(title)=lower(?)
                ORDER BY
                    CASE status
                        WHEN 'imported' THEN 0
                        WHEN 'snatched' THEN 1
                        WHEN 'wanted' THEN 2
                        ELSE 3
                    END,
                    updated_at DESC
                LIMIT 1
                """,
                (author, title),
            ).fetchone()
        return _row_to_book(row) if row else None

    def mark_snatched(
        self,
        book_id: str,
        *,
        protocol: str,
        source: str,
        indexer: str,
        release_title: str,
        download_id: str,
        file_format: str | None = None,
    ) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE books SET
                    status='snatched',
                    protocol=?,
                    source=?,
                    indexer=?,
                    release_title=?,
                    download_id=?,
                    format=?,
                    snatched_at=?,
                    updated_at=?,
                    last_error=NULL
                WHERE id=?
                """,
                (protocol, source, indexer, release_title, download_id, file_format, now, now, book_id),
            )
            conn.execute(
                """
                INSERT INTO download_history
                (book_id, protocol, client, download_id, release_title, status, message, created_at)
                VALUES (?, ?, ?, ?, ?, 'snatched', '', ?)
                """,
                (book_id, protocol, source, download_id, release_title, now),
            )

    def mark_imported(self, book_id: str, library_path: str, file_format: str) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE books SET
                    status='imported',
                    library_path=?,
                    format=?,
                    imported_at=?,
                    updated_at=?,
                    last_error=NULL
                WHERE id=?
                """,
                (library_path, file_format, now, now, book_id),
            )

    def mark_failed(self, book_id: str, error: str) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                "UPDATE books SET status='failed', last_error=?, updated_at=? WHERE id=?",
                (error[:2000], now, book_id),
            )

    def reset_to_wanted(self, book_id: str) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE books SET
                    status='wanted',
                    download_id=NULL,
                    download_path=NULL,
                    release_title=NULL,
                    snatched_at=NULL,
                    last_error=NULL,
                    updated_at=?
                WHERE id=?
                """,
                (now, book_id),
            )

    def set_download_path(self, book_id: str, download_path: str) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                "UPDATE books SET download_path=?, updated_at=? WHERE id=?",
                (download_path, now, book_id),
            )

    def counts(self) -> dict[str, int]:
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM books GROUP BY status"
            ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    def get(self, book_id: str) -> Book | None:
        with self.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        return _row_to_book(row) if row else None

    def list_all(self, *, limit: int = 100) -> list[Book]:
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM books ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_book(r) for r in rows]

    def retry_failed(self) -> int:
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE books SET
                    status='wanted',
                    download_id=NULL,
                    download_path=NULL,
                    release_title=NULL,
                    snatched_at=NULL,
                    last_error=NULL,
                    updated_at=?
                WHERE status='failed'
                """,
                (now,),
            )
            return cur.rowcount


def _row_to_book(row: sqlite3.Row) -> Book:
    return Book(**dict(row))
