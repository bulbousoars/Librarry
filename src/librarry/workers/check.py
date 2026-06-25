from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from librarry.clients.qbittorrent import QBittorrentClient
from librarry.clients.sabnzbd import SabnzbdClient
from librarry.config import AppConfig
from librarry.vault import SecretsVault

log = logging.getLogger(__name__)

_LAST_RUN_FILE = "last_run.json"
STALE_RUN_SECONDS = 5400  # 90 min (cron runs the pipeline every 30 min)
STUCK_SNATCHED_SECONDS = 86400  # 24h "In Progress" with no import = likely stuck


def _last_run_path(cfg: AppConfig) -> Path:
    return Path(cfg.state_dir) / _LAST_RUN_FILE


def record_run(cfg: AppConfig) -> None:
    """Stamp when the full pipeline last completed, for staleness checks."""
    try:
        p = _last_run_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    except OSError as exc:
        log.warning("could not record pipeline run: %s", exc)


def _read_last_run(cfg: AppConfig) -> str | None:
    try:
        return json.loads(_last_run_path(cfg).read_text(encoding="utf-8")).get("at")
    except (OSError, ValueError):
        return None


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _fmt_age(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"

REQUIRED_SECRETS = [
    "hardcover_token",
    "nzbgeek_api_key",
    "jackett_api_key",
    "sab_user",
    "sab_password",
    "sab_api_key",
    "qbit_user",
    "qbit_password",
]


@dataclass
class CheckResult:
    ok: list[str] = field(default_factory=list)
    warn: list[str] = field(default_factory=list)
    fail: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.fail


def _pipeline_checks(cfg: AppConfig, db, result: CheckResult) -> None:
    """Surface a silently-broken pipeline: a stale last-run and stuck downloads
    (the failure mode where a crash left books stranded in 'In Progress')."""
    health = (getattr(cfg, "raw", {}) or {}).get("health", {}) or {}
    stale_limit = float(health.get("stale_run_seconds", STALE_RUN_SECONDS))
    stuck_limit = float(health.get("stuck_snatched_seconds", STUCK_SNATCHED_SECONDS))

    age = _age_seconds(_read_last_run(cfg))
    if age is None:
        result.warn.append("pipeline has not recorded a completed run yet")
    elif age > stale_limit:
        result.warn.append(f"pipeline last completed {_fmt_age(age)} ago — the scheduled run may not be firing")
    else:
        result.ok.append(f"pipeline ran {_fmt_age(age)} ago")

    if db is None:
        return
    try:
        stuck = []
        for b in db.list_by_status("snatched"):
            a = _age_seconds(b.snatched_at)
            if a is not None and a > stuck_limit:
                stuck.append((b, a))
        if stuck:
            titles = ", ".join(f"{b.title} ({_fmt_age(a)})" for b, a in stuck[:5])
            more = "" if len(stuck) <= 5 else f" (+{len(stuck) - 5} more)"
            result.warn.append(
                f"{len(stuck)} book(s) stuck In Progress > {_fmt_age(stuck_limit)}: {titles}{more}"
            )
        else:
            result.ok.append("no stuck downloads")
        failed = db.list_by_status("failed")
        if failed:
            titles = ", ".join(b.title for b in failed[:5])
            more = "" if len(failed) <= 5 else f" (+{len(failed) - 5} more)"
            result.warn.append(f"{len(failed)} failed book(s): {titles}{more}")
    except Exception as exc:
        result.warn.append(f"could not check stuck/failed books: {exc}")


def run_checks(cfg: AppConfig, db=None) -> CheckResult:
    result = CheckResult()

    if cfg.database.parent.exists() or cfg.database.exists():
        result.ok.append(f"database path reachable: {cfg.database.parent}")
    else:
        result.warn.append(f"database parent missing: {cfg.database.parent}")

    vault = SecretsVault(cfg.secrets.vault_path)
    if not vault.exists():
        result.fail.append("secrets vault not initialized (librarry secrets init)")
    else:
        try:
            names = set(
                vault.list_names(
                    password=cfg.resolver.master_password,
                    key_file=cfg.secrets.key_file,
                )
            )
            result.ok.append(f"secrets vault unlocked ({len(names)} entries)")
            for req in REQUIRED_SECRETS:
                if req in names:
                    result.ok.append(f"secret present: {req}")
                else:
                    result.warn.append(f"secret missing: {req}")
        except Exception as exc:
            result.fail.append(f"cannot unlock vault: {exc}")

    if not cfg.hardcover_token:
        result.fail.append("hardcover token not resolved from config")
    else:
        result.ok.append("hardcover token configured")

    for idx in cfg.newznab_indexers:
        if idx.api_key:
            result.ok.append(f"newznab configured: {idx.name}")
        else:
            result.fail.append(f"newznab missing api key: {idx.name}")

    for idx in cfg.torznab_indexers:
        if idx.api_key:
            result.ok.append(f"torznab configured: {idx.name}")
        else:
            result.fail.append(f"torznab missing api key: {idx.name}")

    if cfg.sabnzbd:
        try:
            SabnzbdClient(cfg.sabnzbd)._call("version")
            result.ok.append("SABnzbd API reachable")
        except Exception as exc:
            result.fail.append(f"SABnzbd unreachable: {exc}")

    if cfg.qbittorrent:
        try:
            QBittorrentClient(cfg.qbittorrent).login()
            result.ok.append("qBittorrent API reachable")
        except Exception as exc:
            result.fail.append(f"qBittorrent unreachable: {exc}")

    if cfg.library_dir.exists():
        result.ok.append(f"library dir exists: {cfg.library_dir}")
    else:
        result.warn.append(f"library dir missing: {cfg.library_dir}")

    if cfg.download_dir.exists():
        result.ok.append(f"download dir exists: {cfg.download_dir}")
    else:
        result.warn.append(f"download dir missing: {cfg.download_dir}")

    _pipeline_checks(cfg, db, result)

    return result
