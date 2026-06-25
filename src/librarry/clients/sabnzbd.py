from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

from librarry.config import SabnzbdConfig


@dataclass
class SabHistoryItem:
    nzo_id: str
    name: str
    status: str
    storage: str
    bytes: int


@dataclass
class SabQueueItem:
    nzo_id: str
    name: str
    status: str
    percentage: float
    mb: float
    mb_left: float
    timeleft: str  # e.g. "0:12:34"


class SabnzbdClient:
    def __init__(self, config: SabnzbdConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.base = f"http://{config.host}:{config.port}/api"

    def _call(self, mode: str, **params: Any) -> dict[str, Any]:
        payload = {
            "mode": mode,
            "apikey": self.config.api_key,
            "output": "json",
            **params,
        }
        resp = self.session.get(self.base, params=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"SABnzbd unexpected response for {mode}")
        return data

    def add_url(self, url: str, name: str, category: str | None = None) -> str:
        """Fetch the NZB from the indexer and hand the bytes to SABnzbd.

        We download the NZB ourselves (the indexer URL already carries the API
        key) and submit it via ``addfile`` rather than asking SAB to fetch the
        URL (``addurl``). SAB's own outbound network/DNS to the indexer can be
        blocked even when Librarry's is fine — ``addurl`` then fails opaquely
        with ``{status: False}``. Fetching here also lets us surface a clear
        error when the indexer returns an error page instead of an NZB.
        """
        cat = category or self.config.category
        try:
            resp = self.session.get(url, timeout=60)
            resp.raise_for_status()
        except Exception as exc:  # network error, HTTP error, etc.
            raise RuntimeError(f"Failed to download NZB from indexer: {exc}") from exc
        content = resp.content or b""
        head = content[:4096].lstrip().lower()
        if not (head.startswith(b"<?xml") or b"<nzb" in head):
            ctype = resp.headers.get("content-type", "?")
            snippet = content[:200].decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"Indexer did not return an NZB (content-type {ctype}): {snippet}"
            )
        return self.add_file(content, name, cat)

    def add_file(self, nzb_bytes: bytes, name: str, category: str | None = None) -> str:
        cat = category or self.config.category
        resp = self.session.post(
            self.base,
            data={
                "mode": "addfile",
                "apikey": self.config.api_key,
                "output": "json",
                "cat": cat,
                "nzbname": name,
                "priority": "-1",
            },
            files={"name": (f"{name}.nzb", nzb_bytes, "application/x-nzb")},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("SABnzbd unexpected response for addfile")
        nzo_ids = data.get("nzo_ids") or []
        if nzo_ids:
            return str(nzo_ids[0])
        if data.get("status") is False:
            raise RuntimeError(f"SABnzbd addfile failed: {data}")
        # Some versions return status true without nzo_ids immediately
        return name

    def history(self, limit: int = 50) -> list[SabHistoryItem]:
        data = self._call("history", limit=limit)
        slots = data.get("history", {}).get("slots", [])
        out: list[SabHistoryItem] = []
        for slot in slots:
            out.append(
                SabHistoryItem(
                    nzo_id=str(slot.get("nzo_id", "")),
                    name=str(slot.get("name", "")),
                    status=str(slot.get("status", "")),
                    storage=str(slot.get("storage", "")),
                    bytes=int(slot.get("bytes", 0) or 0),
                )
            )
        return out

    def get_history_item(self, nzo_id: str) -> SabHistoryItem | None:
        for item in self.history(limit=200):
            if item.nzo_id == nzo_id:
                return item
        return None

    def queue(self) -> list[SabQueueItem]:
        """In-progress (downloading/queued) items, for progress reporting."""
        data = self._call("queue")
        slots = data.get("queue", {}).get("slots", [])
        out: list[SabQueueItem] = []
        for slot in slots:
            out.append(
                SabQueueItem(
                    nzo_id=str(slot.get("nzo_id", "")),
                    name=str(slot.get("filename") or slot.get("name", "")),
                    status=str(slot.get("status", "")),
                    percentage=float(slot.get("percentage", 0) or 0),
                    mb=float(slot.get("mb", 0) or 0),
                    mb_left=float(slot.get("mbleft", 0) or 0),
                    timeleft=str(slot.get("timeleft", "")),
                )
            )
        return out

    def get_queue_item(self, nzo_id: str) -> SabQueueItem | None:
        for item in self.queue():
            if item.nzo_id == nzo_id:
                return item
        return None
