from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from librarry.secrets_resolver import SecretsResolver
from librarry.vault import SecretsVault


@dataclass
class IndexerConfig:
    name: str
    host: str
    api_key: str
    book_categories: list[int] = field(default_factory=lambda: [7020])
    enabled: bool = True
    priority: int = 0


@dataclass
class SabnzbdConfig:
    host: str
    port: int
    username: str
    password: str
    api_key: str
    category: str = "books"
    delete_after: bool = False
    enabled: bool = True
    priority: int = 10


@dataclass
class QBittorrentConfig:
    host: str
    port: int
    username: str
    password: str
    category: str = "books"
    save_path: str = "/downloads/books"
    enabled: bool = True
    priority: int = 5


@dataclass
class QualityConfig:
    required_extensions: list[str]
    acceptable_extensions: list[str]
    reject_extensions: list[str]
    reject_patterns: list[str]
    prefer_patterns: list[str]


@dataclass
class SecretsConfig:
    vault_path: Path
    key_file: Path | None
    openbao_addr: str
    openbao_creds_file: Path | None
    openbao_secret_path: str


@dataclass
class OIDCConfig:
    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: list[str] = field(default_factory=lambda: ["openid", "email", "profile"])


@dataclass
class LocalAdminConfig:
    enabled: bool = True


@dataclass
class AuthConfig:
    enabled: bool = False
    session_secret: str = ""
    oidc: OIDCConfig = field(default_factory=OIDCConfig)
    local_admin: LocalAdminConfig = field(default_factory=LocalAdminConfig)


@dataclass
class AppConfig:
    database: Path
    state_dir: Path
    log_dir: Path
    library_dir: Path
    download_dir: Path
    download_subdir: str
    secrets: SecretsConfig
    resolver: SecretsResolver
    hardcover_api_url: str
    hardcover_token: str
    hardcover_want_status_id: int
    hardcover_rate_limit_per_minute: int
    hardcover_min_interval_seconds: float
    newznab_indexers: list[IndexerConfig]
    torznab_indexers: list[IndexerConfig]
    sabnzbd: SabnzbdConfig | None
    qbittorrent: QBittorrentConfig | None
    libgen_enabled: bool
    libgen_max_per_run: int
    libgen_requeue_after_hours: int
    annas_enabled: bool
    annas_api_key: str
    annas_max_per_run: int
    quality: QualityConfig
    fuzz_threshold: float
    max_results_per_indexer: int
    usenet_before_torrent: bool
    max_snatches_per_run: int
    max_imports_per_run: int
    import_file_mode: str
    send_kindle: bool
    kindle_smtp_server: str
    kindle_smtp_port: int
    kindle_smtp_ssl: bool
    kindle_from: str
    kindle_to: str
    kindle_smtp_user: str
    kindle_smtp_password: str
    webui_enabled: bool
    webui_host: str
    webui_port: int
    auth: AuthConfig = field(default_factory=AuthConfig)
    raw: dict[str, Any] = field(default_factory=dict)


def _load_indexers(
    items: list[dict[str, Any]] | None,
    resolver: SecretsResolver,
) -> list[IndexerConfig]:
    out: list[IndexerConfig] = []
    for item in items or []:
        if not item.get("enabled", True):
            continue
        api_key = resolver.resolve_field(
            item, "api_key", "api_key_secret", "api_key_env", default=""
        )
        out.append(
            IndexerConfig(
                name=item["name"],
                host=item["host"].rstrip("/"),
                api_key=api_key,
                book_categories=[int(c) for c in item.get("book_categories", [7020])],
                enabled=True,
                priority=int(item.get("priority", 0)),
            )
        )
    return sorted(out, key=lambda i: i.priority, reverse=True)


def _build_resolver(raw: dict[str, Any], state_dir: Path) -> SecretsResolver:
    sec = raw.get("secrets", {})
    vault_path = Path(sec.get("vault", state_dir / "secrets.vault"))
    key_file_raw = sec.get("key_file")
    key_file = Path(key_file_raw) if key_file_raw else vault_path.parent / "secrets.key"

    openbao = raw.get("openbao", {})
    openbao_creds = openbao.get("creds_file")
    return SecretsResolver(
        vault=SecretsVault(vault_path),
        key_file=key_file,
        openbao_addr=openbao.get("addr", ""),
        openbao_creds_file=Path(openbao_creds) if openbao_creds else None,
    )


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    state_dir = Path(raw["state_dir"])
    resolver = _build_resolver(raw, state_dir)

    quality_raw = raw.get("quality", {}).get("ebook", {})
    quality = QualityConfig(
        required_extensions=[e.lower() for e in quality_raw.get("required_extensions", ["epub"])],
        acceptable_extensions=[e.lower() for e in quality_raw.get("acceptable_extensions", [])],
        reject_extensions=[e.lower() for e in quality_raw.get("reject_extensions", [])],
        reject_patterns=[p.lower() for p in quality_raw.get("reject_patterns", [])],
        prefer_patterns=[p.lower() for p in quality_raw.get("prefer_patterns", [])],
    )

    sab = None
    qbit = None
    for _name, client in (raw.get("download_clients") or {}).items():
        if not client.get("enabled", True):
            continue
        ctype = client.get("type", "").lower()
        if ctype == "sabnzbd":
            sab = SabnzbdConfig(
                host=client["host"],
                port=int(client["port"]),
                username=resolver.resolve_field(client, "username", "username_secret", "username_env"),
                password=resolver.resolve_field(client, "password", "password_secret", "password_env"),
                api_key=resolver.resolve_field(client, "api_key", "api_key_secret", "api_key_env"),
                category=client.get("category", "books"),
                delete_after=bool(client.get("delete_after", False)),
                priority=int(client.get("priority", 10)),
            )
        elif ctype == "qbittorrent":
            qbit = QBittorrentConfig(
                host=client["host"],
                port=int(client["port"]),
                username=resolver.resolve_field(client, "username", "username_secret", "username_env"),
                password=resolver.resolve_field(client, "password", "password_secret", "password_env"),
                category=client.get("category", "books"),
                save_path=client.get("save_path", "/downloads/books"),
                priority=int(client.get("priority", 5)),
            )

    providers_raw = raw.get("providers", {})
    providers = providers_raw.get("libgen", {})
    annas = providers_raw.get("annas", {})
    search = raw.get("search", {})
    run = raw.get("run", {})
    import_cfg = raw.get("import", {})
    hardcover = raw.get("hardcover", {})
    kindle = raw.get("kindle", {})
    sec_raw = raw.get("secrets", {})
    openbao = raw.get("openbao", {})
    webui = raw.get("webui", {})
    auth = raw.get("auth", {}) or {}
    oidc = auth.get("oidc", {}) or {}
    local_admin = auth.get("local_admin", {}) or {}

    vault_path = Path(sec_raw.get("vault", state_dir / "secrets.vault"))
    key_file_raw = sec_raw.get("key_file")
    key_file = Path(key_file_raw) if key_file_raw else vault_path.parent / "secrets.key"

    hardcover_token = resolver.resolve_field(
        hardcover, "token", "token_secret", default=""
    )
    if not hardcover_token:
        # Legacy OpenBao-only setups
        ob_path = openbao.get("secret_path", "secret/hardcover_token")
        try:
            hardcover_token = resolver.resolve(f"openbao:{ob_path}#hardcover_token")
        except Exception:
            hardcover_token = ""

    return AppConfig(
        database=Path(raw["database"]),
        state_dir=state_dir,
        log_dir=Path(raw["log_dir"]),
        library_dir=Path(raw["library_dir"]),
        download_dir=Path(raw["download_dir"]),
        download_subdir=raw.get("download_subdir", "books"),
        secrets=SecretsConfig(
            vault_path=vault_path,
            key_file=key_file,
            openbao_addr=openbao.get("addr", ""),
            openbao_creds_file=Path(openbao["creds_file"]) if openbao.get("creds_file") else None,
            openbao_secret_path=openbao.get("secret_path", "secret/data/hardcover_token"),
        ),
        resolver=resolver,
        hardcover_api_url=hardcover.get("api_url", "https://api.hardcover.app/v1/graphql"),
        hardcover_token=hardcover_token,
        hardcover_want_status_id=int(hardcover.get("want_status_id", 1)),
        hardcover_rate_limit_per_minute=int(hardcover.get("rate_limit_per_minute", 60)),
        hardcover_min_interval_seconds=float(hardcover.get("min_interval_seconds", 1.0)),
        newznab_indexers=_load_indexers(raw.get("newznab_indexers"), resolver),
        torznab_indexers=_load_indexers(raw.get("torznab_indexers"), resolver),
        sabnzbd=sab,
        qbittorrent=qbit,
        libgen_enabled=bool(providers.get("enabled", True)),
        libgen_max_per_run=int(providers.get("max_per_run", 12)),
        libgen_requeue_after_hours=int(providers.get("requeue_after_hours", 12)),
        annas_enabled=bool(annas.get("enabled", False)),
        annas_api_key=resolver.resolve_field(annas, "api_key", "api_key_secret", "api_key_env", default=""),
        annas_max_per_run=int(annas.get("max_per_run", 8)),
        quality=quality,
        fuzz_threshold=float(search.get("fuzz_threshold", 0.45)),
        max_results_per_indexer=int(search.get("max_results_per_indexer", 25)),
        usenet_before_torrent=bool(search.get("usenet_before_torrent", True)),
        max_snatches_per_run=int(run.get("max_snatches_per_run", 5)),
        max_imports_per_run=int(run.get("max_imports_per_run", 10)),
        import_file_mode=import_cfg.get("file_mode", "hardlink"),
        send_kindle=bool(import_cfg.get("send_kindle", False)),
        kindle_smtp_server=kindle.get("smtp_server", "smtp.gmail.com"),
        kindle_smtp_port=int(kindle.get("smtp_port", 465)),
        kindle_smtp_ssl=bool(kindle.get("use_ssl", True)),
        kindle_from=resolver.resolve_field(kindle, "from", "from_secret", "from_env"),
        kindle_to=kindle.get("to", ""),
        kindle_smtp_user=resolver.resolve_field(kindle, "user", "user_secret", "user_env"),
        kindle_smtp_password=resolver.resolve_field(kindle, "password", "password_secret", "password_env"),
        webui_enabled=bool(webui.get("enabled", True)),
        webui_host=str(webui.get("host", "0.0.0.0")),
        webui_port=int(webui.get("port", 5300)),
        auth=AuthConfig(
            enabled=bool(auth.get("enabled", False)),
            session_secret=resolver.resolve_field(auth, "session_secret", "session_secret_secret", "session_secret_env", default=""),
            oidc=OIDCConfig(
                enabled=bool(oidc.get("enabled", False)),
                issuer=str(oidc.get("issuer", "")).rstrip("/"),
                client_id=resolver.resolve_field(oidc, "client_id", "client_id_secret", "client_id_env", default=""),
                client_secret=resolver.resolve_field(oidc, "client_secret", "client_secret_secret", "client_secret_env", default=""),
                redirect_uri=str(oidc.get("redirect_uri", "")),
                scopes=list(oidc.get("scopes", ["openid", "email", "profile"])),
            ),
            local_admin=LocalAdminConfig(
                enabled=bool(local_admin.get("enabled", True)),
            ),
        ),
        raw=raw,
    )
