from pathlib import Path

from librarry.config import AppConfig
from librarry.workers.poll import _find_by_release_title, _resolve_sab_path


def test_resolve_sab_path_books_subdir(tmp_path: Path):
    cfg = AppConfig(
        database=tmp_path / "db",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        library_dir=tmp_path / "books",
        download_dir=tmp_path / "downloads",
        download_subdir="books",
        secrets=None,  # type: ignore
        resolver=None,  # type: ignore
        hardcover_api_url="",
        hardcover_token="",
        hardcover_want_status_id=1,
        hardcover_rate_limit_per_minute=60,
        hardcover_min_interval_seconds=1.0,
        newznab_indexers=[],
        torznab_indexers=[],
        sabnzbd=None,
        qbittorrent=None,
        libgen_enabled=False,
        libgen_max_per_run=0,
        libgen_requeue_after_hours=0,
        annas_enabled=False,
        annas_api_key="",
        annas_max_per_run=8,
        quality=None,  # type: ignore
        fuzz_threshold=0.45,
        max_results_per_indexer=25,
        usenet_before_torrent=True,
        max_snatches_per_run=5,
        max_imports_per_run=10,
        import_file_mode="copy",
        send_kindle=False,
        kindle_smtp_server="",
        kindle_smtp_port=465,
        kindle_smtp_ssl=True,
        kindle_from="",
        kindle_to="",
        kindle_smtp_user="",
        kindle_smtp_password="",
        webui_enabled=True,
        webui_host="127.0.0.1",
        webui_port=5300,
        raw={},
    )
    folder = tmp_path / "downloads" / "books" / "Author.Title.EPUB"
    folder.mkdir(parents=True)
    found = _resolve_sab_path(cfg, "Author.Title.EPUB")
    assert found == folder


def test_find_by_release_title(tmp_path: Path):
    cfg = AppConfig(
        database=tmp_path / "db",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        library_dir=tmp_path / "books",
        download_dir=tmp_path / "downloads",
        download_subdir="books",
        secrets=None,  # type: ignore
        resolver=None,  # type: ignore
        hardcover_api_url="",
        hardcover_token="",
        hardcover_want_status_id=1,
        hardcover_rate_limit_per_minute=60,
        hardcover_min_interval_seconds=1.0,
        newznab_indexers=[],
        torznab_indexers=[],
        sabnzbd=None,
        qbittorrent=None,
        libgen_enabled=False,
        libgen_max_per_run=0,
        libgen_requeue_after_hours=0,
        annas_enabled=False,
        annas_api_key="",
        annas_max_per_run=8,
        quality=None,  # type: ignore
        fuzz_threshold=0.45,
        max_results_per_indexer=25,
        usenet_before_torrent=True,
        max_snatches_per_run=5,
        max_imports_per_run=10,
        import_file_mode="copy",
        send_kindle=False,
        kindle_smtp_server="",
        kindle_smtp_port=465,
        kindle_smtp_ssl=True,
        kindle_from="",
        kindle_to="",
        kindle_smtp_user="",
        kindle_smtp_password="",
        webui_enabled=True,
        webui_host="127.0.0.1",
        webui_port=5300,
        raw={},
    )
    target = tmp_path / "downloads" / "books" / "Connie.Willis-Doomsday.Book.EPUB"
    target.mkdir(parents=True)
    found = _find_by_release_title(cfg, "Connie.Willis-Doomsday.Book.2005.Retail.EPUB")
    assert found == target


def test_human_speed_and_eta():
    from librarry.workers.poll import _human_speed, _human_eta
    assert _human_speed(0) == "0 B/s"
    assert _human_speed(1536) == "2 KB/s"
    assert _human_speed(5 * 1024 * 1024) == "5 MB/s"
    assert _human_eta(0) == "?"
    assert _human_eta(8640000) == "?"  # qBittorrent "unknown"
    assert _human_eta(75) == "1:15"
    assert _human_eta(3661) == "1:01:01"


def _poll_cfg(tmp_path, **over):
    from librarry.config import QBittorrentConfig
    base = dict(
        database=tmp_path / "db", state_dir=tmp_path / "state", log_dir=tmp_path / "logs",
        library_dir=tmp_path / "books", download_dir=tmp_path / "downloads", download_subdir="books",
        secrets=None, resolver=None, hardcover_api_url="", hardcover_token="",
        hardcover_want_status_id=1, hardcover_rate_limit_per_minute=60, hardcover_min_interval_seconds=1.0,
        newznab_indexers=[], torznab_indexers=[],
        sabnzbd=None,
        qbittorrent=QBittorrentConfig(host="qb", port=8080, username="u", password="p"),
        libgen_enabled=False, libgen_max_per_run=0, libgen_requeue_after_hours=0,
        annas_enabled=False, annas_api_key="", annas_max_per_run=8,
        quality=None, fuzz_threshold=0.45, max_results_per_indexer=25, usenet_before_torrent=True,
        max_snatches_per_run=5, max_imports_per_run=10, import_file_mode="copy",
        send_kindle=False, kindle_smtp_server="", kindle_smtp_port=465, kindle_smtp_ssl=True,
        kindle_from="", kindle_to="", kindle_smtp_user="", kindle_smtp_password="",
        webui_enabled=True, webui_host="127.0.0.1", webui_port=5300, raw={},
    )
    base.update(over)
    return AppConfig(**base)


def test_poll_logs_torrent_download_progress(tmp_path, monkeypatch, caplog):
    import logging
    from librarry.db import Database
    from librarry.clients.qbittorrent import TorrentInfo
    import librarry.workers.poll as pollmod

    db = Database(tmp_path / "db")
    db.init()
    bid = db.add_manual("Some Torrent Book", "Author")
    db.mark_snatched(bid, protocol="torrent", source="x", indexer="x",
                     release_title="Some Torrent Book", download_id="", file_format="epub")

    class FakeQbit:
        def __init__(self, *a, **k): pass
        def find_by_name(self, needle):
            return TorrentInfo(hash="h", name="Some Torrent Book", state="downloading",
                               progress=0.5, save_path="/dl", category="books",
                               dlspeed=2 * 1024 * 1024, eta=120)
        def is_complete(self, t): return False

    monkeypatch.setattr(pollmod, "QBittorrentClient", FakeQbit)

    with caplog.at_level(logging.INFO, logger="librarry.workers.poll"):
        out = pollmod.poll_downloads(_poll_cfg(tmp_path), db)

    assert out["waiting"] == 1
    msgs = "\n".join(r.message for r in caplog.records)
    assert "Downloading (qBit)" in msgs
    assert "50%" in msgs and "2 MB/s" in msgs and "2:00" in msgs
