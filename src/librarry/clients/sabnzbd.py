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
        cat = category or self.config.category
        data = self._call(
            "addurl",
            name=name,
            nzbname=name,
            cat=cat,
            nzburl=url,
            priority="-1",
        )
        nzo_ids = data.get("nzo_ids") or []
        if nzo_ids:
            return str(nzo_ids[0])
        status = data.get("status")
        if status is False:
            raise RuntimeError(f"SABnzbd addurl failed: {data}")
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
