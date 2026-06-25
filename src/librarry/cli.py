from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

import uvicorn

from librarry.config import load_config
from librarry.db import Database
from librarry.users import UserStore
from librarry.vault import SecretsVault, VaultError
from librarry.workers.check import REQUIRED_SECRETS, record_run, run_checks
from librarry.workers.hardcover_sync import sync_hardcover
from librarry.workers.import_book import import_ready, scan_library
from librarry.workers.libgen import fetch_libgen
from librarry.workers.poll import poll_downloads
from librarry.workers.search import search_wanted

BOOTSTRAP_SECRETS = REQUIRED_SECRETS + [
    "kindle_smtp_from",
    "kindle_smtp_user",
    "kindle_smtp_password",
]


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "librarry.log"),
        ],
    )


def _vault_from_config(config_path: str) -> tuple[SecretsVault, Path | None, str | None]:
    cfg = load_config(config_path)
    password = cfg.resolver.master_password
    return SecretsVault(cfg.secrets.vault_path), cfg.secrets.key_file, password


def _cmd_secrets_init(args: argparse.Namespace) -> int:
    vault, key_file, _ = _vault_from_config(args.config)
    if vault.exists():
        print(f"Vault already exists: {vault.vault_path}", file=sys.stderr)
        return 1
    if args.password:
        p1 = getpass.getpass("Master password: ")
        p2 = getpass.getpass("Confirm password: ")
        if p1 != p2:
            print("Passwords do not match", file=sys.stderr)
            return 1
        vault.init_password(p1)
        print(f"Password-protected vault created: {vault.vault_path}")
    else:
        if not key_file:
            print("No key_file configured", file=sys.stderr)
            return 1
        vault.init_keyfile(key_file)
        print(f"Key-file vault created: {vault.vault_path}")
        print(f"Key file (mode 600): {key_file}")
    return 0


def _cmd_secrets_set(args: argparse.Namespace) -> int:
    vault, key_file, password = _vault_from_config(args.config)
    if not vault.exists():
        print("Vault not initialized. Run: librarry secrets init", file=sys.stderr)
        return 1
    value = sys.stdin.read().rstrip("\n") if args.stdin else getpass.getpass(f"Value for {args.name}: ")
    vault.set(args.name, value, password=password, key_file=key_file)
    print(f"Saved secret: {args.name}")
    return 0


def _cmd_secrets_bootstrap(args: argparse.Namespace) -> int:
    vault, key_file, password = _vault_from_config(args.config)
    if not vault.exists():
        print("Vault not initialized. Run: librarry secrets init", file=sys.stderr)
        return 1
    existing = set(vault.list_names(password=password, key_file=key_file))
    for name in BOOTSTRAP_SECRETS:
        if name in existing and not args.force:
            print(f"skip (exists): {name}")
            continue
        value = getpass.getpass(f"{name} [{ 'optional, enter to skip' if name.startswith('kindle') else 'required'} ]: ")
        if not value:
            if name.startswith("kindle"):
                continue
            print(f"Skipped required secret {name}")
            continue
        vault.set(name, value, password=password, key_file=key_file)
        print(f"saved: {name}")
    return 0


def _cmd_secrets_delete(args: argparse.Namespace) -> int:
    vault, key_file, password = _vault_from_config(args.config)
    if vault.delete(args.name, password=password, key_file=key_file):
        print(f"Deleted: {args.name}")
        return 0
    print(f"Not found: {args.name}", file=sys.stderr)
    return 1


def _cmd_secrets_list(args: argparse.Namespace) -> int:
    vault, key_file, password = _vault_from_config(args.config)
    if not vault.exists():
        print("Vault not initialized.")
        return 0
    for name in vault.list_names(password=password, key_file=key_file):
        print(name)
    return 0


def _cmd_secrets_rotate(args: argparse.Namespace) -> int:
    vault, _, _ = _vault_from_config(args.config)
    old = getpass.getpass("Current master password: ")
    new = getpass.getpass("New master password: ")
    confirm = getpass.getpass("Confirm new password: ")
    if new != confirm:
        print("Passwords do not match", file=sys.stderr)
        return 1
    vault.rotate_password(old, new)
    print("Master password rotated.")
    return 0


def _print_check(result) -> int:
    for line in result.ok:
        print(f"OK   {line}")
    for line in result.warn:
        print(f"WARN {line}")
    for line in result.fail:
        print(f"FAIL {line}")
    return 0 if result.success else 1


def _cmd_admin_bootstrap(config_path: str, username: str) -> int:
    cfg = load_config(config_path)
    p1 = getpass.getpass("Local admin password: ")
    p2 = getpass.getpass("Confirm local admin password: ")
    if p1 != p2:
        print("Passwords do not match", file=sys.stderr)
        return 1
    store = UserStore(cfg.state_dir / "users.db", cfg.state_dir / "users")
    store.init()
    store.upsert_local_admin(username, p1)
    print(f"Bootstrapped local admin: {username}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="librarry", description="Librarry ebook orchestrator")
    default_config = os.environ.get("LIBRARRY_CONFIG", "")
    parser.add_argument(
        "--config",
        default=default_config,
        help="Path to config.yaml (or set LIBRARRY_CONFIG)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize database")
    sub.add_parser("sync", help="Sync Hardcover Want to Read")
    sub.add_parser("scan", help="Mark wanted books already in the library as imported")
    sub.add_parser("search", help="Search indexers and snatch")
    sub.add_parser("poll", help="Poll download clients")
    sub.add_parser("libgen", help="LibGen fallback fetch")
    sub.add_parser("import", help="Import completed downloads")
    sub.add_parser("run", help="sync + search + poll + libgen + import")
    sub.add_parser("status", help="Show book counts")
    sub.add_parser("check", help="Validate config, secrets, and connectivity")
    p_serve = sub.add_parser("serve", help="Start web UI")
    p_serve.add_argument("--host", default=None, help="Bind host (default from config)")
    p_serve.add_argument("--port", type=int, default=None, help="Bind port (default from config)")

    p_books = sub.add_parser("books", help="List books")
    p_books.add_argument("--status", default=None, help="Filter by status")
    p_books.add_argument("--limit", type=int, default=50)

    p_retry = sub.add_parser("retry", help="Reset failed books to wanted")
    p_retry.add_argument("--failed-only", action="store_true", default=True)

    admin = sub.add_parser("admin", help="Manage local admin accounts")
    admin_sub = admin.add_subparsers(dest="admin_cmd", required=True)
    p_admin_boot = admin_sub.add_parser("bootstrap", help="Create or rotate a local break-glass admin")
    p_admin_boot.add_argument("--username", default="admin")

    secrets = sub.add_parser("secrets", help="Manage encrypted credentials")
    secrets_sub = secrets.add_subparsers(dest="secrets_cmd", required=True)

    p_init = secrets_sub.add_parser("init", help="Create encrypted secrets vault")
    p_init.add_argument("--password", action="store_true")

    p_set = secrets_sub.add_parser("set", help="Store a secret")
    p_set.add_argument("name")
    p_set.add_argument("--stdin", action="store_true")

    p_boot = secrets_sub.add_parser("bootstrap", help="Interactive setup for all secrets")
    p_boot.add_argument("--force", action="store_true", help="Overwrite existing secrets")

    p_del = secrets_sub.add_parser("delete", help="Delete a secret")
    p_del.add_argument("name")

    secrets_sub.add_parser("list", help="List secret names")
    secrets_sub.add_parser("rotate-password", help="Change master password")

    args = parser.parse_args(argv)
    if not args.config:
        parser.error("--config is required (or set LIBRARRY_CONFIG)")

    if args.command == "secrets":
        try:
            handlers = {
                "init": _cmd_secrets_init,
                "set": _cmd_secrets_set,
                "bootstrap": _cmd_secrets_bootstrap,
                "delete": _cmd_secrets_delete,
                "list": _cmd_secrets_list,
                "rotate-password": _cmd_secrets_rotate,
            }
            return handlers[args.secrets_cmd](args)
        except VaultError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command == "admin":
        if args.admin_cmd == "bootstrap":
            return _cmd_admin_bootstrap(args.config, args.username)
        print(f"Unknown admin command: {args.admin_cmd}", file=sys.stderr)
        return 1

    cfg = load_config(args.config)

    if args.command == "check":
        return _print_check(run_checks(cfg))

    if args.command == "serve":
        if not cfg.webui_enabled:
            print("Web UI disabled in config (webui.enabled: false)", file=sys.stderr)
            return 1
        host = args.host or cfg.webui_host
        port = args.port or cfg.webui_port
        print(f"Librarry web UI on http://{host}:{port}")

        def _app_factory():
            from librarry.webui.app import create_app

            return create_app(args.config)

        uvicorn.run(
            _app_factory,
            factory=True,
            host=host,
            port=port,
            log_level="info",
        )
        return 0

    _setup_logging(cfg.log_dir)
    db = Database(cfg.database)

    if args.command == "init":
        db.init()
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        print(f"Initialized {cfg.database}")
        if not cfg.secrets.vault_path.exists():
            print("Next: librarry secrets init --config ...")
        return 0

    db.init()

    if args.command == "sync":
        print(sync_hardcover(cfg, db))
    elif args.command == "scan":
        print(scan_library(cfg, db))
    elif args.command == "search":
        print(search_wanted(cfg, db))
    elif args.command == "poll":
        print(poll_downloads(cfg, db))
    elif args.command == "libgen":
        print(fetch_libgen(cfg, db))
    elif args.command == "import":
        print(import_ready(cfg, db))
    elif args.command == "run":
        print("sync", sync_hardcover(cfg, db))
        print("scan", scan_library(cfg, db))
        print("search", search_wanted(cfg, db))
        print("poll", poll_downloads(cfg, db))
        print("libgen", fetch_libgen(cfg, db))
        print("import", import_ready(cfg, db))
        record_run(cfg)
    elif args.command == "status":
        counts = db.counts()
        for status in ("wanted", "snatched", "imported", "failed"):
            print(f"{status}: {counts.get(status, 0)}")
    elif args.command == "books":
        rows = db.list_by_status(args.status) if args.status else db.list_all(limit=args.limit)
        for b in rows[: args.limit]:
            err = f"  ({b.last_error[:60]}...)" if b.last_error else ""
            print(f"{b.status:8}  {b.author} — {b.title}{err}")
    elif args.command == "retry":
        n = db.retry_failed()
        print(f"Reset {n} failed book(s) to wanted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
