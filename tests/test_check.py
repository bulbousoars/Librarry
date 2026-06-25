import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from librarry.db import Database
from librarry.workers import check


def _cfg(tmp: Path, raw=None):
    return types.SimpleNamespace(state_dir=Path(tmp), raw=raw or {})


def test_record_and_read_last_run(tmp_path):
    cfg = _cfg(tmp_path)
    assert check._read_last_run(cfg) is None
    check.record_run(cfg)
    age = check._age_seconds(check._read_last_run(cfg))
    assert age is not None and age < 5


def test_pipeline_checks_warns_when_no_run_recorded(tmp_path):
    res = check.CheckResult()
    check._pipeline_checks(_cfg(tmp_path), None, res)
    assert any("not recorded a completed run" in w for w in res.warn)


def test_pipeline_checks_warns_when_run_is_stale(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    (tmp_path / "last_run.json").write_text(json.dumps({"at": old}), encoding="utf-8")
    res = check.CheckResult()
    check._pipeline_checks(_cfg(tmp_path), None, res)
    assert any("last completed" in w and "may not be firing" in w for w in res.warn)


def test_pipeline_checks_ok_when_run_recent(tmp_path):
    check.record_run(_cfg(tmp_path))
    res = check.CheckResult()
    check._pipeline_checks(_cfg(tmp_path), None, res)
    assert any("pipeline ran" in m for m in res.ok)


def test_pipeline_checks_flags_stuck_snatched(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init()
    bid = db.add_manual("Stuck Book", "Author")
    db.mark_snatched(bid, protocol="direct", source="x", indexer="x",
                     release_title="r", download_id="d", file_format="epub")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with db.connect() as c:
        c.execute("UPDATE books SET snatched_at=? WHERE id=?", (old, bid))

    cfg = _cfg(tmp_path)
    check.record_run(cfg)  # so staleness doesn't add noise
    res = check.CheckResult()
    check._pipeline_checks(cfg, db, res)
    assert any("stuck In Progress" in w and "Stuck Book" in w for w in res.warn)


def test_pipeline_checks_ok_when_recent_snatch(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init()
    bid = db.add_manual("Fresh Book", "Author")
    db.mark_snatched(bid, protocol="direct", source="x", indexer="x",
                     release_title="r", download_id="d", file_format="epub")
    cfg = _cfg(tmp_path)
    check.record_run(cfg)
    res = check.CheckResult()
    check._pipeline_checks(cfg, db, res)
    assert "no stuck downloads" in res.ok
