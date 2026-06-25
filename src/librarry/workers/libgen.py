from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from librarry.config import AppConfig
from librarry.db import Database
from librarry.quality import detect_extension

log = logging.getLogger(__name__)

WIKI_API = "https://en.wikipedia.org/w/api.php"
FALLBACK_DOMAINS = [
    "libgen.li", "libgen.vg", "libgen.bz", "libgen.gl",
    "libgen.la", "libgen.is", "libgen.rs", "libgen.st",
]
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PREFERRED_EXT = ["epub", "azw3", "mobi"]
ACCEPT_EXT = PREFERRED_EXT + ["pdf", "fb2", "djvu"]
SCORE_MIN = 0.60
MIN_BYTES = 20_000
JUNK = [
    "study guide", "summary", "analysis", "workbook", "cliffnotes",
    "cliff notes", "key takeaways", "guide to", "boxed set", "box set",
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})


def _state_path(cfg: AppConfig) -> Path:
    return cfg.state_dir / "libgen_fetch.json"


def _candidate_domains() -> list[str]:
    domains: list[str] = []
    try:
        r = SESSION.get(
            WIKI_API,
            params={
                "action": "parse", "page": "Library_Genesis",
                "prop": "text", "format": "json", "formatversion": 2,
            },
            timeout=20,
        )
        html = r.json()["parse"]["text"]
        m = re.search(r">URL<.*?</tr>", html, re.S)
        seg = m.group(0) if m else ""
        domains = list(dict.fromkeys(re.findall(r"libgen\.[a-z]{2,}", seg)))
    except Exception as exc:
        log.warning("Wikipedia domain lookup failed: %s", exc)
    for d in FALLBACK_DOMAINS:
        if d not in domains:
            domains.append(d)
    return domains


def _pick_host(domains: list[str]) -> str | None:
    for d in domains:
        try:
            r = SESSION.get(
                f"https://{d}/index.php",
                params={"req": "harry potter", "f_lang": "All", "f_columns": 0, "f_ext": "All"},
                timeout=30,
            )
            if r.status_code == 200 and "ads.php?md5=" in r.text:
                return d
        except Exception:
            continue
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower())


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).split() if len(t) > 1}


def _fetch_search(req: str, *, retries: int = 3) -> tuple[str, str] | None:
    """Run a LibGen search, rotating host on transient errors / 500s.

    LibGen mirrors intermittently return HTTP 500 under load; on a bad response
    we drop the cached host and re-probe so the next attempt hits a live mirror.
    """
    for _ in range(retries):
        host = get_host()
        if not host:
            return None
        try:
            r = SESSION.get(
                f"https://{host}/index.php",
                params={"req": req, "f_lang": "All", "f_columns": 0, "f_ext": "All"},
                timeout=30,
            )
        except Exception:
            _host_cache["host"] = None
            continue
        if r.status_code == 200 and "ads.php?md5=" in r.text:
            return host, r.text
        _host_cache["host"] = None  # 500 / blocked / empty → try a different mirror
        time.sleep(1)
    return None


def _extract_candidates(html: str, want: set[str], host: str, *, exact: bool = False) -> list[dict]:
    """Parse search-result rows into candidate dicts. `exact=True` (ISBN match)
    forces a perfect score since the result is the right edition by definition."""
    out: list[dict] = []
    seen: set[str] = set()
    for row in re.split(r"(?i)<tr[ >]", html):
        m = re.search(r"ads\.php\?md5=([0-9a-fA-F]{32})", row)
        if not m:
            continue
        md5 = m.group(1).lower()
        if md5 in seen:
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)).strip()
        low = text.lower()
        if any(j in low for j in JUNK):
            continue
        ext = next((e for e in ACCEPT_EXT if re.search(rf"\b{e}\b", low)), None)
        if not ext:
            continue
        seen.add(md5)
        # Title is the edition.php anchor text; it may be followed by tags
        # (e.g. "<i></i></a>"), so capture up to the next tag rather than </a>.
        tm = re.search(r'edition\.php\?id=\d+">\s*([^<]+?)\s*<', row)
        rel_title = (tm.group(1).strip() if tm else "")[:160] or "LibGen result"
        score = 1.0 if exact else round(len(want & _tokens(low)) / max(1, len(want)), 3)
        out.append(
            {
                "md5": md5,
                "ext": ext,
                "title": rel_title,
                "size_bytes": _parse_size(text),
                "score": score,
                "host": host,
            }
        )
    return out


def _collect(author: str, title: str, isbns: list[str] | None = None) -> list[dict]:
    """Gather LibGen candidates: exact ISBN matches first, then title + author."""
    short = re.split(r"[:—]", title)[0].strip()
    want = _tokens(f"{author} {short}")
    found: dict[str, dict] = {}
    for isbn in [i for i in (isbns or []) if i]:
        res = _fetch_search(isbn)
        if not res:
            continue
        host, html = res
        for c in _extract_candidates(html, want, host, exact=True):
            prev = found.get(c["md5"])
            if not prev or c["score"] > prev["score"]:
                found[c["md5"]] = c
    res = _fetch_search(f"{author} {short}".strip())
    if res:
        host, html = res
        for c in _extract_candidates(html, want, host):
            found.setdefault(c["md5"], c)
    return list(found.values())


def _search(author: str, title: str, isbns: list[str] | None = None) -> tuple[str, str, str] | None:
    """Best (md5, ext, host) for the pipeline auto-fetch; ISBN-first."""
    cands = [c for c in _collect(author, title, isbns) if c["score"] >= SCORE_MIN]
    if not cands:
        return None
    cands.sort(key=lambda c: (-c["score"], PREFERRED_EXT.index(c["ext"]) if c["ext"] in PREFERRED_EXT else 99))
    best = cands[0]
    return best["md5"], best["ext"], best["host"]


def _download(host: str, md5: str) -> bytes | None:
    ads_url = f"https://{host}/ads.php?md5={md5}"
    ads = SESSION.get(ads_url, timeout=30).text
    m = (
        re.search(rf'href="((?:https?://[^"]+/)?get\.php\?md5={md5}[^"]*)"', ads, re.I)
        or re.search(r'href="(/?get\.php\?[^"]+)"', ads, re.I)
    )
    if not m:
        return None
    get_url = urljoin(f"https://{host}/", m.group(1).replace("&amp;", "&"))
    r = SESSION.get(get_url, headers={"Referer": ads_url}, timeout=180, allow_redirects=True)
    if not r.ok:
        return None
    data = r.content
    if len(data) < MIN_BYTES or data[:15].lstrip()[:1] == b"<":
        return None
    return data


def _safe_name(author: str, title: str) -> str:
    name = re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", f"{author} - {title}".strip(" -")))
    return name[:180].strip()


_host_cache: dict[str, object] = {"host": None, "ts": 0.0}


def get_host(ttl: int = 600) -> str | None:
    """Return a working LibGen host, cached to avoid re-probing on every search."""
    now = time.time()
    if _host_cache["host"] and now - float(_host_cache["ts"]) < ttl:  # type: ignore[arg-type]
        return _host_cache["host"]  # type: ignore[return-value]
    host = _pick_host(_candidate_domains())
    if host:
        _host_cache["host"] = host
        _host_cache["ts"] = now
    return host


# Ebooks are always KB–GB; a bare "B" would mis-match stray digits (e.g. ISBNs).
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB)\b", re.I)
_UNIT = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}


def _parse_size(text: str) -> int | None:
    m = _SIZE_RE.search(text)
    if not m:
        return None
    return int(float(m.group(1)) * _UNIT[m.group(2).upper()])


def libgen_search(
    cfg: AppConfig, author: str, title: str, *, isbns: list[str] | None = None, limit: int = 8
) -> list[dict]:
    """Search LibGen (ISBN-first, then title+author) and return candidate dicts
    shaped like indexer releases, so they list alongside Newznab/Torznab."""
    if not get_host():
        return []
    out: list[dict] = []
    for c in _collect(author, title, isbns):
        rejected = c["score"] < SCORE_MIN
        out.append(
            {
                "title": f"{c['title']} [{c['ext']}]",
                "indexer": "LibGen",
                "protocol": "direct",
                "format": c["ext"],
                "size_bytes": c["size_bytes"],
                "download_url": f"libgen:{c['host']}:{c['md5']}:{c['ext']}",
                "score": c["score"],
                "rejected": rejected,
                "reason": "low title match" if rejected else None,
                "pub_date": None,
                "seeders": None,
                "leechers": None,
                "grabs": None,
                "category": "libgen",
            }
        )
    out.sort(key=lambda c: (c["rejected"], -c["score"]))
    return out[:limit]


def grab_md5(cfg: AppConfig, db: Database, book_id: str, host: str, md5: str, ext: str, title: str) -> str:
    """Download a specific LibGen md5 and mark the book snatched (pipeline imports it)."""
    data = _download(host, md5)
    if not data:
        raise RuntimeError("LibGen download failed (no file or dead link)")
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
        source="libgen",
        indexer="libgen",
        release_title=name,
        download_id=name,
        file_format=ext,
    )
    db.set_download_path(book_id, str(folder))
    return name


def fetch_libgen(cfg: AppConfig, db: Database) -> dict[str, int]:
    if not cfg.libgen_enabled:
        return {"fetched": 0, "missed": 0}

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    state_file = _state_path(cfg)
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    now = time.time()
    requeue = cfg.libgen_requeue_after_hours * 3600

    if not get_host():
        log.error("No working LibGen host")
        return {"fetched": 0, "missed": 0}

    got = missed = failed = 0
    download_root = cfg.download_dir / cfg.download_subdir
    download_root.mkdir(parents=True, exist_ok=True)

    for book in db.list_by_status("wanted"):
        if got >= cfg.libgen_max_per_run:
            break
        last = state.get(book.id, 0)
        if now - last < requeue:
            continue
        # Isolate each book: one failure (network, bad file, a vanished book row,
        # etc.) must never abort the whole pipeline — `import` runs after libgen,
        # so an unhandled error here would silently strand completed downloads.
        try:
            log.info("LibGen: %r by %r", book.title, book.author)
            hit = _search(book.author, book.title, isbns=[book.isbn_13, book.isbn_10])
            if not hit:
                state[book.id] = now
                missed += 1
                continue
            md5, ext, host = hit
            data = _download(host, md5)
            if not data:
                state[book.id] = now
                missed += 1
                continue
            name = _safe_name(book.author, book.title)
            folder = download_root / name
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f"{name}.{ext}"
            dest.write_bytes(data)
            if hasattr(os, "chown"):
                for p in (folder, dest):
                    try:
                        os.chown(p, 1000, 1000)
                    except (PermissionError, OSError):
                        pass
            db.mark_snatched(
                book.id,
                protocol="direct",
                source="libgen",
                indexer="libgen",
                release_title=name,
                download_id=name,
                file_format=ext,
            )
            db.set_download_path(book.id, str(folder))
            state[book.id] = now
            got += 1
            time.sleep(3)
        except Exception as exc:
            # Back off this book so it doesn't re-fail on every run.
            state[book.id] = now
            failed += 1
            log.error("LibGen failed for %r (%s): %s", book.title, book.id, exc)

    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {"fetched": got, "missed": missed, "failed": failed}
