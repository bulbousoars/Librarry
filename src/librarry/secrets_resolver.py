from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

from librarry.vault import SecretsVault, VaultError

log = logging.getLogger(__name__)


def openbao_login(addr: str, creds_file: Path) -> str:
    creds = json.loads(creds_file.read_text(encoding="utf-8"))
    resp = requests.post(
        f"{addr.rstrip('/')}/v1/auth/approle/login",
        json={"role_id": creds["role_id"], "secret_id": creds["secret_id"]},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"OpenBao login failed: {body['errors']}")
    return body["auth"]["client_token"]


def openbao_read_secret(addr: str, token: str, secret_path: str) -> dict[str, Any]:
    path = secret_path.removeprefix("secret/data/").removeprefix("secret/")
    resp = requests.get(
        f"{addr.rstrip('/')}/v1/secret/data/{path}",
        headers={"X-Vault-Token": token},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"OpenBao read failed: {body['errors']}")
    return body["data"]["data"]


def warn_if_jwt_expiring(token: str, name: str = "token", days_warn: int = 30) -> None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return
    if not exp:
        return
    days_left = (exp - time.time()) / 86400
    if days_left < 0:
        raise RuntimeError(f"{name} expired {abs(days_left):.0f} days ago")
    if days_left < days_warn:
        log.warning("%s expires in %.0f days", name, days_left)


class SecretsResolver:
    """Resolve secret references from vault, env, or optional OpenBao."""

    def __init__(
        self,
        vault: SecretsVault | None = None,
        key_file: Path | None = None,
        master_password: str | None = None,
        openbao_addr: str = "",
        openbao_creds_file: Path | None = None,
    ):
        self.vault = vault
        self.key_file = key_file
        self.master_password = master_password or os.environ.get("LIBRARRY_MASTER_PASSWORD")
        self.openbao_addr = openbao_addr
        self.openbao_creds_file = openbao_creds_file

    def resolve(self, ref: str | None, *, default: str = "") -> str:
        if ref is None or ref == "":
            return default
        if not isinstance(ref, str):
            return str(ref)

        if ref.startswith("secret:"):
            name = ref[7:]
            if not self.vault or not self.vault.exists():
                log.warning("Vault not initialized; cannot resolve %s", ref)
                return default
            try:
                return self.vault.get(
                    name,
                    password=self.master_password,
                    key_file=self.key_file,
                )
            except VaultError as exc:
                log.warning("Secret %s unavailable: %s", name, exc)
                return default

        if ref.startswith("env:"):
            return os.environ.get(ref[4:], default)

        # Legacy *_env field names passed directly
        if ref.endswith("_env"):
            return os.environ.get(ref, default)

        if ref.startswith("openbao:"):
            # openbao:secret/hardcover_token#hardcover_token
            spec = ref[8:]
            if "#" in spec:
                path, key = spec.split("#", 1)
            else:
                path, key = spec, spec.rsplit("/", 1)[-1]
            if not self.openbao_addr or not self.openbao_creds_file:
                raise RuntimeError(f"OpenBao not configured; cannot resolve {ref}")
            token = openbao_login(self.openbao_addr, self.openbao_creds_file)
            data = openbao_read_secret(self.openbao_addr, token, path)
            for candidate in (key, key.upper(), key.lower()):
                if candidate in data and data[candidate]:
                    return str(data[candidate])
            raise RuntimeError(f"OpenBao path {path} missing key {key}")

        # Literal value — allowed for non-sensitive fields only
        return ref

    def resolve_field(self, item: dict[str, Any], *keys: str, default: str = "") -> str:
        for key in keys:
            if key in item and item[key] not in (None, ""):
                return self.resolve(str(item[key]), default=default)
        return default
