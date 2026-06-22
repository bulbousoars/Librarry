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
