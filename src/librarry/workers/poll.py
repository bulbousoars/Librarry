from __future__ import annotations

import logging
import re
from pathlib import Path

from librarry.clients.qbittorrent import QBittorrentClient
from librarry.clients.sabnzbd import SabnzbdClient
from librarry.config import AppConfig
from librarry.db import Database

log = logging.getLogger(__name__)


def _norm_token(s: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", s.lower()).split() if len(t) > 2]


def _human_speed(bps: float) -> str:
    bps = float(bps or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if bps < 1024:
            return f"{bps:.0f} {unit}/s"
        bps /= 1024
    return f"{bps:.0f} TB/s"


def _human_eta(secs: int) -> str:
    secs = int(secs or 0)
    if secs <= 0 or secs >= 8640000:  # qBittorrent uses 8640000 for unknown
        return "?"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _resolve_sab_path(cfg: AppConfig, storage: str) -> Path | None:
    if not storage:
        return None
    candidates = [
        Path(storage),
        cfg.download_dir / storage,
        cfg.download_dir / cfg.download_subdir / Path(storage).name,
        cfg.download_dir / cfg.download_subdir / storage,
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _find_by_release_title(cfg: AppConfig, release_title: str | None) -> Path | None:
    if not release_title:
        return None
    books_root = cfg.download_dir / cfg.download_subdir
    if not books_root.is_dir():
        return None
    want = {t for t in _norm_token(release_title) if len(t) > 2}
    if not want:
        return None
    best: tuple[float, Path] | None = None
    for child in books_root.iterdir():
        if not child.is_dir():
            continue
        got = {t for t in _norm_token(child.name) if len(t) > 2}
        if not got:
            continue
        score = len(want & got) / len(want)
        if score >= 0.5 and (best is None or score > best[0]):
            best = (score, child)
    return best[1] if best else None


def poll_downloads(cfg: AppConfig, db: Database) -> dict[str, int]:
    sab = SabnzbdClient(cfg.sabnzbd) if cfg.sabnzbd and cfg.sabnzbd.enabled else None
    qbit = QBittorrentClient(cfg.qbittorrent) if cfg.qbittorrent and cfg.qbittorrent.enabled else None

    ready = waiting = failed = 0
    for book in db.list_by_status("snatched"):
        if book.download_path:
            ready += 1
            continue
        try:
            if book.protocol == "usenet" and sab:
                path = None
                if book.download_id:
                    item = sab.get_history_item(book.download_id)
                    if item:
                        if item.status.lower() in ("failed", "failure"):
                            db.mark_failed(book.id, f"SABnzbd failed: {item.name}")
                            failed += 1
                            continue
                        if item.status.lower() in ("completed", "complete"):
                            path = _resolve_sab_path(cfg, item.storage)
                if not path:
                    path = _find_by_release_title(cfg, book.release_title)
                if path and path.exists():
                    db.set_download_path(book.id, str(path))
                    log.info("SAB complete: %s -> %s", book.title, path)
                    ready += 1
                else:
                    qi = sab.get_queue_item(book.download_id) if book.download_id else None
                    if qi:
                        log.info(
                            "Downloading (SAB) %r — %.0f%% (%.0f/%.0f MB left, ETA %s)",
                            book.title, qi.percentage, qi.mb_left, qi.mb, qi.timeleft or "?",
                        )
                    else:
                        log.info("Waiting (SAB) %r — not yet visible in queue/history", book.title)
                    waiting += 1
            elif book.protocol == "torrent" and qbit:
                torrent = qbit.find_by_name(book.release_title or book.title)
                if not torrent:
                    log.info("Waiting (qBit) %r — torrent not found yet", book.title)
                    waiting += 1
                    continue
                if not qbit.is_complete(torrent):
                    log.info(
                        "Downloading (qBit) %r — %.0f%% @ %s, ETA %s [%s]",
                        book.title, torrent.progress * 100, _human_speed(torrent.dlspeed),
                        _human_eta(torrent.eta), torrent.state,
                    )
                    waiting += 1
                    continue
                content = Path(torrent.save_path) / torrent.name
                db.set_download_path(book.id, str(content))
                log.info("qBit complete: %s -> %s", book.title, content)
                ready += 1
            elif book.protocol == "direct":
                # LibGen sets download_path at snatch time
                if book.download_path and Path(book.download_path).exists():
                    ready += 1
                else:
                    waiting += 1
            else:
                waiting += 1
        except Exception as exc:
            log.error("Poll error for %r: %s", book.title, exc)
            db.mark_failed(book.id, str(exc))
            failed += 1

    return {"ready": ready, "waiting": waiting, "failed": failed}
