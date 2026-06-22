from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Iterable
from urllib.parse import urlencode

import requests

from librarry.config import IndexerConfig
from librarry.quality import ReleaseCandidate


class IndexerError(RuntimeError):
    pass


def _parse_newznab_xml(text: str, indexer: str, protocol: str) -> list[ReleaseCandidate]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise IndexerError(f"{indexer}: invalid XML: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        return []

    out: list[ReleaseCandidate] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        pub_date = (item.findtext("pubDate") or "").strip() or None

        attrs: dict[str, str] = {}
        for attr in item.findall("{http://www.newznab.com/DTD/2010/feeds/attributes/}attr"):
            name = attr.get("name")
            if name is not None:
                attrs[name] = attr.get("value", "")

        def _int(key: str) -> int | None:
            try:
                return int(attrs[key])
            except (KeyError, ValueError, TypeError):
                return None

        size = _int("size") or 0
        enclosure = item.find("enclosure")
        if enclosure is not None and enclosure.get("url"):
            link = enclosure.get("url", link)
            if not size:
                try:
                    size = int(enclosure.get("length", "0"))
                except (ValueError, TypeError):
                    size = 0

        seeders = _int("seeders")
        leechers = _int("leechers")
        peers = _int("peers")
        if leechers is None and peers is not None and seeders is not None:
            leechers = max(peers - seeders, 0)
        category = attrs.get("category") or None

        if not title or not link:
            continue
        out.append(
            ReleaseCandidate(
                title=title,
                download_url=link,
                size_bytes=size,
                indexer=indexer,
                protocol=protocol,
                guid=guid,
                category=category,
                pub_date=pub_date,
                seeders=seeders,
                leechers=leechers,
                grabs=_int("grabs"),
            )
        )
    return out


class NewznabClient:
    """Usenet indexer using the Newznab API (NZBGeek, etc.)."""

    def __init__(self, config: IndexerConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    def search(self, query: str, *, limit: int = 25) -> list[ReleaseCandidate]:
        params = {
            "t": "search",
            "q": query,
            "apikey": self.config.api_key,
            "o": "xml",
            "limit": limit,
        }
        if self.config.book_categories:
            params["cat"] = ",".join(str(c) for c in self.config.book_categories)
        url = f"{self.config.host}/api?{urlencode(params)}"
        resp = self.session.get(url, timeout=45)
        resp.raise_for_status()
        return _parse_newznab_xml(resp.text, self.config.name, "usenet")


class TorznabClient:
    """Torrent indexer using Torznab API (Jackett, Prowlarr, native Torznab)."""

    def __init__(self, config: IndexerConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    def search(self, query: str, *, limit: int = 25) -> list[ReleaseCandidate]:
        params = {
            "t": "search",
            "q": query,
            "apikey": self.config.api_key,
            "o": "xml",
            "limit": limit,
        }
        if self.config.book_categories:
            params["cat"] = ",".join(str(c) for c in self.config.book_categories)
        url = f"{self.config.host}?{urlencode(params)}"
        resp = self.session.get(url, timeout=45)
        resp.raise_for_status()
        return _parse_newznab_xml(resp.text, self.config.name, "torrent")


def search_all_indexers(
    query: str,
    newznab: Iterable[NewznabClient],
    torznab: Iterable[TorznabClient],
    *,
    limit: int,
    usenet_first: bool,
) -> list[ReleaseCandidate]:
    results: list[ReleaseCandidate] = []
    groups = ([("usenet", newznab), ("torrent", torznab)] if usenet_first
              else [("torrent", torznab), ("usenet", newznab)])
    for _kind, clients in groups:
        for client in clients:
            try:
                results.extend(client.search(query, limit=limit))
            except Exception:
                continue
    return results
