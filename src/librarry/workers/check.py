from __future__ import annotations

import logging
from dataclasses import dataclass, field

from librarry.clients.qbittorrent import QBittorrentClient
from librarry.clients.sabnzbd import SabnzbdClient
from librarry.config import AppConfig
from librarry.vault import SecretsVault

log = logging.getLogger(__name__)

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


def run_checks(cfg: AppConfig) -> CheckResult:
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

    return result
