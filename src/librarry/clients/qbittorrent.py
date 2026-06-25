from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from librarry.config import QBittorrentConfig


@dataclass
class TorrentInfo:
    hash: str
    name: str
    state: str
    progress: float
    save_path: str
    category: str
    dlspeed: int = 0  # bytes/sec
    eta: int = 0  # seconds (8640000 == unknown/infinite in qBittorrent)


class QBittorrentClient:
    """qBittorrent Web API v2 client."""

    def __init__(self, config: QBittorrentConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.base = f"http://{config.host}:{config.port}"
        self._logged_in = False

    def login(self) -> None:
        if self._logged_in:
            return
        resp = self.session.post(
            f"{self.base}/api/v2/auth/login",
            data={"username": self.config.username, "password": self.config.password},
            timeout=30,
        )
        resp.raise_for_status()
        if resp.text.strip().lower() not in ("ok", "fail."):
            # Some versions return Ok without period
            if "fail" in resp.text.lower():
                raise RuntimeError(f"qBittorrent login failed: {resp.text}")
        self._logged_in = True

    def add_torrent(
        self,
        url: str,
        *,
        category: str | None = None,
        save_path: str | None = None,
        name: str | None = None,
    ) -> str:
        self.login()
        data: dict[str, Any] = {"urls": url}
        cat = category or self.config.category
        if cat:
            data["category"] = cat
        sp = save_path or self.config.save_path
        if sp:
            data["savepath"] = sp
        if name:
            data["rename"] = name
        resp = self.session.post(
            f"{self.base}/api/v2/torrents/add",
            data=data,
            timeout=60,
        )
        resp.raise_for_status()
        if resp.text.strip().lower() == "fail.":
            raise RuntimeError(f"qBittorrent add failed for {name or url}")
        return url

    def list_torrents(self, category: str | None = None) -> list[TorrentInfo]:
        self.login()
        params: dict[str, str] = {}
        if category:
            params["category"] = category
        resp = self.session.get(
            f"{self.base}/api/v2/torrents/info",
            params=params,
            timeout=60,
        )
        resp.raise_for_status()
        out: list[TorrentInfo] = []
        for item in resp.json():
            out.append(
                TorrentInfo(
                    hash=str(item.get("hash", "")),
                    name=str(item.get("name", "")),
                    state=str(item.get("state", "")),
                    progress=float(item.get("progress", 0) or 0),
                    save_path=str(item.get("save_path", "")),
                    category=str(item.get("category", "")),
                    dlspeed=int(item.get("dlspeed", 0) or 0),
                    eta=int(item.get("eta", 0) or 0),
                )
            )
        return out

    def find_by_name(self, needle: str) -> TorrentInfo | None:
        needle_l = needle.lower()
        for torrent in self.list_torrents(self.config.category):
            if needle_l in torrent.name.lower():
                return torrent
        return None

    def is_complete(self, torrent: TorrentInfo) -> bool:
        return torrent.progress >= 1.0 or torrent.state in {
            "uploading",
            "stalledUP",
            "pausedUP",
            "queuedUP",
            "forcedUP",
            "moving",
        }
