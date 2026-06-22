#!/usr/bin/env python3
"""One-time mediaprod bootstrap: init vault and import secrets from existing homelab sources.

Reads:
  - Hardcover token via OpenBao (hardcover_sync.creds + secret/hardcover_token)
  - NZBGeek, Jackett, SABnzbd, qBittorrent from LazyLibrarian config.ini

Does not print secret values. Safe to run repeatedly (skips existing vault entries).
"""
from __future__ import annotations

import configparser
import json
import sys
from pathlib import Path

import requests

# Container mount points (see deploy/docker-compose.yml)
HARDCOVER_CREDS = Path("/secrets/hardcover_sync.creds")
LL_CONFIG = Path("/secrets/lazylibrarian.ini")
CONFIG_PATH = Path("/config/config.yaml")
OPENBAO_ADDR = "http://192.168.1.251:8200"
OPENBAO_SECRET = "secret/data/hardcover_token"


def _pick(data: dict, *names: str) -> str:
    for name in names:
        val = data.get(name)
        if val:
            return str(val)
    raise RuntimeError(f"OpenBao secret missing keys: {names}")


def hardcover_token() -> str:
    creds = json.loads(HARDCOVER_CREDS.read_text(encoding="utf-8"))
    login = requests.post(
        f"{OPENBAO_ADDR}/v1/auth/approle/login",
        json={"role_id": creds["role_id"], "secret_id": creds["secret_id"]},
        timeout=15,
    ).json()
    if "errors" in login:
        raise RuntimeError(f"OpenBao login failed: {login['errors']}")
    token = login["auth"]["client_token"]
    resp = requests.get(
        f"{OPENBAO_ADDR}/v1/{OPENBAO_SECRET}",
        headers={"X-Vault-Token": token},
        timeout=15,
    ).json()
    if "errors" in resp:
        raise RuntimeError(f"OpenBao read failed: {resp['errors']}")
    return _pick(resp["data"]["data"], "hardcover_token", "HARDCOVER_TOKEN")


def ll_secrets() -> dict[str, str]:
    cfg = configparser.ConfigParser()
    cfg.read(LL_CONFIG, encoding="utf-8")
    out: dict[str, str] = {}

    if cfg.has_section("Newznab_0"):
        out["nzbgeek_api_key"] = cfg.get(
            "Newznab_0", "api_key", fallback=cfg.get("Newznab_0", "api", fallback="")
        ).strip()

    if cfg.has_section("Torznab_0"):
        out["jackett_api_key"] = cfg.get(
            "Torznab_0", "api_key", fallback=cfg.get("Torznab_0", "api", fallback="")
        ).strip()

    if cfg.has_section("SABNZBD"):
        out["sab_user"] = cfg.get(
            "SABNZBD", "username", fallback=cfg.get("SABNZBD", "sab_user", fallback="")
        ).strip()
        out["sab_password"] = cfg.get(
            "SABNZBD", "password", fallback=cfg.get("SABNZBD", "sab_pass", fallback="")
        ).strip()
        out["sab_api_key"] = cfg.get(
            "SABNZBD", "api_key", fallback=cfg.get("SABNZBD", "sab_api", fallback="")
        ).strip()

    if cfg.has_section("QBITTORRENT"):
        out["qbit_user"] = cfg.get(
            "QBittorrent", "username",
            fallback=cfg.get("QBITTORRENT", "qbittorrent_user", fallback=""),
        ).strip()
        out["qbit_password"] = cfg.get(
            "QBittorrent", "password",
            fallback=cfg.get("QBITTORRENT", "qbittorrent_pass", fallback=""),
        ).strip()

    return out


def main() -> int:
    sys.path.insert(0, "/app/src")
    from librarry.config import load_config
    from librarry.vault import SecretsVault

    cfg = load_config(str(CONFIG_PATH))
    vault = SecretsVault(cfg.secrets.vault_path)
    key_file = cfg.secrets.key_file

    if not vault.exists():
        if not key_file:
            print("No key_file configured", file=sys.stderr)
            return 1
        vault.init_keyfile(key_file)
        print(f"Created vault: {vault.vault_path}")

    existing = set(vault.list_names(key_file=key_file))
    imported = 0

    def save(name: str, value: str) -> None:
        nonlocal imported
        if not value:
            print(f"skip (empty source): {name}")
            return
        if name in existing:
            print(f"skip (exists): {name}")
            return
        vault.set(name, value, key_file=key_file)
        existing.add(name)
        imported += 1
        print(f"saved: {name}")

    save("hardcover_token", hardcover_token())
    for key, value in ll_secrets().items():
        save(key, value)

    print(f"Done. {imported} new secret(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
