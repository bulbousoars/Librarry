import tempfile
from pathlib import Path

from librarry.db import Database
from librarry.workers.import_book import scan_library


class _Cfg:
    """Minimal stand-in for AppConfig — scan_library only needs library_dir."""

    def __init__(self, library_dir: Path):
        self.library_dir = library_dir


def _make_db(tmp: Path) -> Database:
    db = Database(tmp / "test.db")
    db.init()
    return db


def test_scan_marks_existing_books_imported():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        lib = tmp / "books"
        # mirror the real on-disk layout, including author-name variants
        (lib / "Brandon Sanderson").mkdir(parents=True)
        (lib / "Brandon Sanderson" / "The Way of Kings.epub").write_text("x")
        (lib / "C. S. Lewis").mkdir(parents=True)
        (lib / "C. S. Lewis" / "Perelandra").mkdir()
        (lib / "C. S. Lewis" / "Perelandra" / "Perelandra - C S Lewis.epub").write_text("x")

        db = _make_db(tmp)
        have_id = db.add_manual("The Way of Kings", "Brandon Sanderson")
        have2_id = db.add_manual("Perelandra", "C.S. Lewis")  # punctuation differs
        missing_id = db.add_manual("Some Book Not On Disk", "Nobody")

        result = scan_library(_Cfg(lib), db)
        assert result["matched"] == 2
        assert db.get(have_id).status == "imported"
        assert db.get(have2_id).status == "imported"
        assert db.get(missing_id).status == "wanted"


def test_scan_does_not_match_wrong_author():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        lib = tmp / "books"
        (lib / "Stephen King").mkdir(parents=True)
        (lib / "Stephen King" / "The Dark Tower.epub").write_text("x")

        db = _make_db(tmp)
        # same-ish title, different author — must NOT match
        wid = db.add_manual("The Dark Tower and Other Stories", "C. S. Lewis")
        result = scan_library(_Cfg(lib), db)
        assert result["matched"] == 0
        assert db.get(wid).status == "wanted"
