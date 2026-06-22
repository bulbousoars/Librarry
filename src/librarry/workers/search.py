from __future__ import annotations

import logging
import re

from librarry.clients.qbittorrent import QBittorrentClient
from librarry.clients.sabnzbd import SabnzbdClient
from librarry.config import AppConfig
from librarry.db import Database
from librarry.indexers import NewznabClient, TorznabClient, search_all_indexers
from librarry.quality import detect_extension, evaluate_release, rank_candidates

log = logging.getLogger(__name__)


def _search_query(title: str, author: str) -> str:
    short = re.split(r"[:—]", title)[0].strip()
    return f"{short} {author}".strip()


def search_releases(cfg: AppConfig, title: str, author: str) -> list[dict]:
    """Search all indexers for a title/author and return scored candidates.

    Unlike the pipeline, this returns *all* releases (including rejected ones,
    with a reason) so the UI can show why a release was skipped.
    """
    newznab = [NewznabClient(i) for i in cfg.newznab_indexers]
    torznab = [TorznabClient(i) for i in cfg.torznab_indexers]
    query = _search_query(title, author)
    candidates = search_all_indexers(
        query,
        newznab,
        torznab,
        limit=cfg.max_results_per_indexer,
        usenet_first=cfg.usenet_before_torrent,
    )
    rank_query = f"{author} {title}".strip()
    out: list[dict] = []
    for c in candidates:
        score, reason = evaluate_release(c.title, rank_query, cfg.quality)
        below = reason is None and score < cfg.fuzz_threshold
        out.append(
            {
                "title": c.title,
                "indexer": c.indexer,
                "protocol": c.protocol,
                "format": detect_extension(c.title),
                "size_bytes": c.size_bytes,
                "download_url": c.download_url,
                "score": round(score, 3),
                "rejected": bool(reason) or below,
                "reason": reason or ("below fuzz threshold" if below else None),
                "pub_date": c.pub_date,
                "seeders": c.seeders,
                "leechers": c.leechers,
                "grabs": c.grabs,
                "category": c.category,
            }
        )
    out.sort(key=lambda r: (r["rejected"], -r["score"]))
    return out


def search_book(cfg: AppConfig, db: Database, book_id: str) -> dict:
    """Search indexers for a single book and auto-grab the best acceptable release."""
    book = db.get(book_id)
    if not book:
        return {"snatched": 0, "error": "unknown book"}
    candidates = search_releases(cfg, book.title, book.author)
    best = next((c for c in candidates if not c["rejected"]), None)
    if not best:
        log.info("Re-search: no acceptable release for %r", book.title)
        return {"snatched": 0, "reason": "no acceptable release"}
    try:
        snatch_release(
            cfg,
            db,
            book_id=book_id,
            title=best["title"],
            download_url=best["download_url"],
            protocol=best["protocol"],
            indexer=best["indexer"],
        )
        return {"snatched": 1, "release": best["title"]}
    except Exception as exc:
        log.error("Re-search snatch failed for %r: %s", book.title, exc)
        db.mark_failed(book_id, str(exc))
        return {"snatched": 0, "error": str(exc)}


def snatch_release(
    cfg: AppConfig,
    db: Database,
    *,
    book_id: str,
    title: str,
    download_url: str,
    protocol: str,
    indexer: str,
) -> str:
    """Send a specific release to the matching download client and mark snatched."""
    book = db.get(book_id)
    if not book:
        raise ValueError(f"unknown book: {book_id}")
    ext = detect_extension(title)
    if protocol == "usenet":
        if not (cfg.sabnzbd and cfg.sabnzbd.enabled):
            raise RuntimeError("SABnzbd is not configured/enabled")
        download_id = SabnzbdClient(cfg.sabnzbd).add_url(download_url, title, cfg.sabnzbd.category)
        source = "sabnzbd"
    else:
        if not (cfg.qbittorrent and cfg.qbittorrent.enabled):
            raise RuntimeError("qBittorrent is not configured/enabled")
        download_id = QBittorrentClient(cfg.qbittorrent).add_torrent(
            download_url,
            category=cfg.qbittorrent.category,
            save_path=cfg.qbittorrent.save_path,
            name=title,
        )
        source = "qbittorrent"
    db.mark_snatched(
        book_id,
        protocol=protocol,
        source=source,
        indexer=indexer,
        release_title=title,
        download_id=download_id,
        file_format=ext,
    )
    log.info("Manually snatched %r via %s (%s)", title, indexer, protocol)
    return download_id


def search_wanted(cfg: AppConfig, db: Database) -> dict[str, int]:
    newznab = [NewznabClient(i) for i in cfg.newznab_indexers]
    torznab = [TorznabClient(i) for i in cfg.torznab_indexers]
    sab = SabnzbdClient(cfg.sabnzbd) if cfg.sabnzbd and cfg.sabnzbd.enabled else None
    qbit = QBittorrentClient(cfg.qbittorrent) if cfg.qbittorrent and cfg.qbittorrent.enabled else None

    snatched = failed = skipped = 0
    for book in db.list_by_status("wanted"):
        if cfg.max_snatches_per_run and snatched >= cfg.max_snatches_per_run:
            log.info("Reached snatch cap (%d)", cfg.max_snatches_per_run)
            break
        query = _search_query(book.title, book.author)
        log.info("Searching: %r (%s)", book.title, query)
        candidates = search_all_indexers(
            query,
            newznab,
            torznab,
            limit=cfg.max_results_per_indexer,
            usenet_first=cfg.usenet_before_torrent,
        )
        ranked = rank_candidates(
            book.title, book.author, candidates, cfg.quality, cfg.fuzz_threshold
        )
        if not ranked:
            log.warning("No acceptable release for %r", book.title)
            skipped += 1
            continue

        pick = ranked[0]
        ext = detect_extension(pick.title)
        try:
            if pick.protocol == "usenet":
                if not sab:
                    log.warning("Usenet hit but SABnzbd disabled: %s", pick.title)
                    skipped += 1
                    continue
                download_id = sab.add_url(pick.download_url, pick.title, cfg.sabnzbd.category)
                db.mark_snatched(
                    book.id,
                    protocol="usenet",
                    source="sabnzbd",
                    indexer=pick.indexer,
                    release_title=pick.title,
                    download_id=download_id,
                    file_format=ext,
                )
            else:
                if not qbit:
                    log.warning("Torrent hit but qBittorrent disabled: %s", pick.title)
                    skipped += 1
                    continue
                download_id = qbit.add_torrent(
                    pick.download_url,
                    category=cfg.qbittorrent.category,
                    save_path=cfg.qbittorrent.save_path,
                    name=pick.title,
                )
                db.mark_snatched(
                    book.id,
                    protocol="torrent",
                    source="qbittorrent",
                    indexer=pick.indexer,
                    release_title=pick.title,
                    download_id=download_id,
                    file_format=ext,
                )
            log.info("Snatched %r via %s (%s)", book.title, pick.indexer, pick.protocol)
            snatched += 1
        except Exception as exc:
            log.error("Snatch failed for %r: %s", book.title, exc)
            db.mark_failed(book.id, str(exc))
            failed += 1

    return {"snatched": snatched, "skipped": skipped, "failed": failed}
