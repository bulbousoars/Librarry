"""Anna's Archive provider — search + (member API) download.

Search works without auth; fast downloads require a member API key
(providers.annas.api_key). Results are shaped like indexer releases so they
appear alongside Newznab/Torznab/LibGen in the interactive search.
"""

from __future__ import annotations

import logging
import os
import re

from librarry.config import AppConfig
from librarry.db import Database
from librarry.workers.libgen import (
    ACCEPT_EXT,
    JUNK,
    MIN_BYTES,
    PREFERRED_EXT,
    SCORE_MIN,
    SESSION,
    _parse_size,
    _safe_name,
    _tokens,
)

log = logging.getLogger(__name__)

ANNAS_BASE = "https://annas-archive.org"


def _fetch(query: str) -> str | None:
    try:
        r = SESSION.get(f"{ANNAS_BASE}/search", params={"q": query}, timeout=30)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    # Anna's lazy-loads results inside HTML comments — unwrap them.
    return r.text.replace("<!--", "").replace("-->", "")


def _extract(html: str, want: set[str], found: dict[str, dict], *, exact: bool = False) -> None:
    for m in re.finditer(r'href="/md5/([0-9a-fA-F]{32})"(.*?)(?=href="/md5/|\Z)', html, re.S):
        md5 = m.group(1).lower()
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        low = text.lower()
        if any(j in low for j in JUNK):
            continue
        ext = next((e for e in ACCEPT_EXT if re.search(rf"\b{e}\b", low)), None)
        if not ext:
            continue
        score = 1.0 if exact else round(len(want & _tokens(low)) / max(1, len(want)), 3)
        rel_title = (text[:120] or "Anna's result").strip()
        cand = {
            "title": f"{rel_title} [{ext}]",
            "indexer": "Anna's Archive",
            "protocol": "direct",
            "format": ext,
            "size_bytes": _parse_size(text),
            "download_url": f"annas:{md5}:{ext}",
            "score": score,
            "rejected": score < SCORE_MIN,
            "reason": "low title match" if score < SCORE_MIN else None,
            "pub_date": None,
            "seeders": None,
            "leechers": None,
            "grabs": None,
            "category": "annas",
        }
        prev = found.get(md5)
        if not prev or cand["score"] > prev["score"]:
            found[md5] = cand


def annas_search(
    cfg: AppConfig, author: str, title: str, *, isbns: list[str] | None = None, limit: int = 8
) -> list[dict]:
    short = re.split(r"[:—]", title)[0].strip()
    want = _tokens(f"{author} {short}")
    found: dict[str, dict] = {}
    for isbn in [i for i in (isbns or []) if i]:
        html = _fetch(isbn)
        if html:
            _extract(html, want, found, exact=True)
    html = _fetch(f"{author} {short}".strip())
    if html:
        _extract(html, want, found)
    out = list(found.values())
    out.sort(key=lambda c: (c["rejected"], -c["score"]))
    return out[:limit]


def grab_md5(cfg: AppConfig, db: Database, book_id: str, md5: str, ext: str, title: str) -> str:
    if not cfg.annas_api_key:
        raise RuntimeError("Anna's Archive download needs a member API key (Settings → Indexers)")
    resp = SESSION.get(
        f"{ANNAS_BASE}/dyn/api/fast_download.json",
        params={"md5": md5, "key": cfg.annas_api_key},
        timeout=30,
    )
    try:
        info = resp.json()
    except Exception:
        raise RuntimeError("Anna's Archive returned a non-JSON response")
    url = info.get("download_url")
    if not url:
        raise RuntimeError(f"Anna's fast download unavailable: {info.get('error') or 'no url'}")
    data = SESSION.get(url, timeout=180, allow_redirects=True).content
    if len(data) < MIN_BYTES or data[:15].lstrip()[:1] == b"<":
        raise RuntimeError("Anna's download returned no file")
    book = db.get(book_id)
    name = _safe_name(book.author if book else "", title)
    download_root = cfg.download_dir / cfg.download_subdir
    folder = download_root / name
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{name}.{ext}"
    dest.write_bytes(data)
    for p in (folder, dest):
        try:
            os.chown(p, 1000, 1000)
        except (PermissionError, OSError):
            pass
    db.mark_snatched(
        book_id,
        protocol="direct",
        source="annas",
        indexer="annas",
        release_title=name,
        download_id=name,
        file_format=ext,
    )
    db.set_download_path(book_id, str(folder))
    return name
