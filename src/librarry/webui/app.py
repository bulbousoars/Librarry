from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import yaml
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from librarry import __version__, hardcover
from librarry.auth import OIDCClient, OIDCError
from librarry.hardcover import HardcoverRateLimited
from librarry.config import AppConfig, load_config
from librarry.db import Database
from librarry.kindle import send_to_kindle
from librarry.users import User, UserStore
from librarry.workers.check import run_checks
from librarry.workers.hardcover_sync import sync_hardcover
from librarry.workers.import_book import import_ready, scan_library
from librarry.workers import annas as annas_worker
from librarry.workers import libgen as libgen_worker
from librarry.workers.libgen import fetch_libgen
from librarry.workers.poll import poll_downloads
from librarry.workers.search import search_book, search_releases, search_wanted, snatch_release

_ACTIONS = {
    "sync": sync_hardcover,
    "scan": scan_library,
    "search": search_wanted,
    "poll": poll_downloads,
    "libgen": fetch_libgen,
    "import": import_ready,
}

_current_user: contextvars.ContextVar[User | None] = contextvars.ContextVar("librarry_user", default=None)
_current_db: contextvars.ContextVar[Database | None] = contextvars.ContextVar("librarry_db", default=None)
_effective_user: contextvars.ContextVar[User | None] = contextvars.ContextVar("librarry_effective_user", default=None)

SESSION_COOKIE = "librarry_session"
OIDC_LOGIN_COOKIE = "librarry_oidc_login"


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge patch into base. Non-dict values (incl. lists) replace."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _is_set(value: Any) -> bool:
    return bool(value) and str(value).strip() != ""


def _indexer_view(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items or []:
        out.append(
            {
                "name": it.get("name", "—"),
                "host": it.get("host", ""),
                "categories": it.get("book_categories", [7020]),
                "priority": it.get("priority", 0),
                "enabled": it.get("enabled", True),
                "has_key": _is_set(it.get("api_key"))
                or _is_set(it.get("api_key_secret"))
                or _is_set(it.get("api_key_env")),
            }
        )
    return out


def _clients_view(clients: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, c in (clients or {}).items():
        out.append(
            {
                "name": name,
                "type": c.get("type", "?"),
                "host": c.get("host", ""),
                "port": c.get("port", ""),
                "category": c.get("category", ""),
                "save_path": c.get("save_path", ""),
                "priority": c.get("priority", 0),
                "enabled": c.get("enabled", True),
            }
        )
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "x"


def _url_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _importlists_view(raw: dict[str, Any]) -> list[dict[str, Any]]:
    hc = raw.get("hardcover", {}) or {}
    configured = _is_set(hc.get("token")) or _is_set(hc.get("token_secret"))
    return [
        {
            "name": "Hardcover — Want to Read",
            "type": "hardcover",
            "api_url": hc.get("api_url", "https://api.hardcover.app/v1/graphql"),
            "want_status_id": hc.get("want_status_id", 1),
            "configured": configured,
        }
    ]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign_session(payload: dict[str, Any], secret: str) -> str:
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(sig)}"


def _unsign_session(value: str, secret: str) -> dict[str, Any] | None:
    try:
        body, sig = value.split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expected), sig):
            return None
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _user_json(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "auth_type": user.auth_type,
        "email": user.email,
        "display_name": user.display_name,
        "username": user.username,
        "enabled": user.enabled,
        "is_admin": user.is_admin,
        "setup_complete": user.setup_complete,
    }


def _kindle_json(settings) -> dict[str, Any]:
    return {
        "user_id": settings.user_id,
        "kindle_to": settings.kindle_to,
        "send_kindle": settings.send_kindle,
        "setup_complete": settings.setup_complete,
        "last_test_status": settings.last_test_status,
        "last_test_at": settings.last_test_at,
        "last_send_status": settings.last_send_status,
        "last_send_at": settings.last_send_at,
    }


def create_app(config_path: str) -> FastAPI:
    cfg = load_config(config_path)
    db = Database(cfg.database)
    db.init()
    users = UserStore(cfg.state_dir / "users.db", cfg.state_dir / "users")
    users.init()

    app = FastAPI(title="Librarry", version=__version__)
    app.state.config_path = config_path
    app.state.cfg = cfg
    app.state.db = db
    app.state.users = users
    app.state.last_action: dict[str, Any] = {}

    def _auth_enabled() -> bool:
        return bool(app.state.cfg.auth.enabled)

    def _session_secret() -> str:
        secret = app.state.cfg.auth.session_secret
        if not secret:
            raise HTTPException(500, "auth.session_secret is required when auth is enabled")
        return secret

    def _current() -> User | None:
        return _current_user.get()

    def _db() -> Database:
        return _current_db.get() or app.state.db

    def _effective() -> User | None:
        return _effective_user.get() or _current()

    def _require_user() -> User:
        user = _current()
        if not user:
            raise HTTPException(401, "authentication required")
        return user

    def _require_admin() -> User:
        user = _require_user()
        if not user.is_admin:
            raise HTTPException(403, "admin access required")
        return user

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        user_token = None
        db_token = None
        effective_token = None
        user: User | None = None
        if _auth_enabled():
            raw = request.cookies.get(SESSION_COOKIE)
            if raw:
                payload = _unsign_session(raw, _session_secret())
                user_id = str((payload or {}).get("user_id") or "")
                if user_id:
                    found = app.state.users.get_user(user_id)
                    if found and found.enabled:
                        user = found
                        user_token = _current_user.set(user)
                        effective = user
                        effective_id = str((payload or {}).get("effective_user_id") or "")
                        if user.is_admin and effective_id:
                            selected = app.state.users.get_user(effective_id)
                            if selected and selected.enabled:
                                effective = selected
                        db_token = _current_db.set(Database(effective.database_path))
                        effective_token = _effective_user.set(effective)
            public = (
                request.url.path == "/"
                or request.url.path.startswith("/auth/")
                or request.url.path.startswith("/favicon.")
            )
            if request.url.path.startswith("/api/") and not public and user is None:
                return JSONResponse({"detail": "authentication required"}, status_code=401)
        try:
            return await call_next(request)
        finally:
            if db_token is not None:
                _current_db.reset(db_token)
            if effective_token is not None:
                _effective_user.reset(effective_token)
            if user_token is not None:
                _current_user.reset(user_token)

    def _reload_cfg() -> AppConfig:
        cfg = load_config(config_path)
        app.state.cfg = cfg
        return cfg

    def _save_config_patch(patch: dict[str, Any]) -> dict[str, Any]:
        p = Path(config_path)
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _deep_merge(raw, patch)
        p.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        _reload_cfg()
        return raw

    def _read_raw() -> dict[str, Any]:
        return yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}

    def _write_raw(raw: dict[str, Any]) -> None:
        Path(config_path).write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        _reload_cfg()

    def _vault_set(name: str, value: str) -> None:
        resolver = app.state.cfg.resolver
        vault = getattr(resolver, "vault", None)
        if not vault:
            raise HTTPException(400, "No secrets vault configured; cannot store credentials")
        try:
            vault.set(name, value, password=resolver.master_password, key_file=resolver.key_file)
        except Exception as exc:
            raise HTTPException(500, f"vault write failed: {exc}")

    def _book_progress(b) -> tuple[str, str]:
        if b.status == "imported":
            return ("Owned", f"Imported to {b.library_path}" if b.library_path else "Imported")
        if b.status == "failed":
            return ("Failed", b.last_error or "Last action failed")
        if b.status == "wanted":
            return ("Wanted", "Queued for search")
        if b.status == "snatched":
            if b.download_path:
                return ("Downloaded", f"Download complete at {b.download_path}; ready for import")
            if b.protocol == "direct":
                return ("Downloading", f"Direct download from {b.indexer or b.source or 'provider'} is running")
            if b.protocol in ("usenet", "torrent"):
                return ("Queued", f"Sent to {b.indexer or b.source or b.protocol}; waiting for completion")
            return ("In Progress", "Waiting for download status")
        return (b.status.title(), "")

    def _hardcover_url(b) -> str:
        slug = b.hardcover_slug or _url_slug(b.title)
        return f"https://hardcover.app/books/{slug}" if slug else ""

    def _book_json(b) -> dict[str, Any]:
        data = asdict(b)
        extras = _db().get_book_extras(b.id)
        stage, detail = _book_progress(b)
        data["local_path"] = b.library_path or b.download_path
        data["hardcover_url"] = _hardcover_url(b)
        data["progress_stage"] = stage
        data["progress_detail"] = detail
        data["notes"] = extras.get("notes") or ""
        data["tags"] = extras.get("tags") or ""
        data["cover_override_url"] = extras.get("cover_override_url") or ""
        data["cover_display_url"] = data["cover_override_url"] or b.cover_url
        return data

    def _int_or_none(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _bibliography_category(subjects: list[str]) -> str:
        lowered = [s.lower() for s in subjects]
        if any("nonfiction" in s or "non-fiction" in s or "non fiction" in s for s in lowered):
            return "Nonfiction"
        if any("fiction" in s for s in lowered):
            return "Fiction"
        return ""

    def _bibliography_genre(subjects: list[str], category: str) -> str:
        skip = {"fiction", "nonfiction", "non-fiction", "non fiction"}
        genres: list[str] = []
        for s in subjects:
            clean = str(s).strip()
            lowered = clean.lower()
            if not clean or lowered in skip or lowered.startswith("series:"):
                continue
            genres.append(clean)
            if len(genres) >= 3:
                break
        return ", ".join(genres)

    def _bibliography_series(value: Any, subjects: list[str]) -> str:
        if isinstance(value, list):
            explicit = ", ".join(str(s).strip() for s in value[:2] if str(s).strip())
        else:
            explicit = str(value or "").strip()
        if explicit:
            return explicit
        found = []
        for s in subjects:
            clean = str(s).strip()
            if clean.lower().startswith("series:"):
                name = clean.split(":", 1)[1].strip()
                if name:
                    found.append(name)
        return ", ".join(found[:2])

    def _text_value(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("value") or "").strip()
        return str(value or "").strip()

    def _openlibrary_author_metadata(author_key: str, bibliography_count: int) -> dict[str, Any]:
        if not author_key:
            return {}
        try:
            resp = requests.get(f"https://openlibrary.org/authors/{author_key}.json", timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return {}
        photos = data.get("photos") or []
        image_url = f"https://covers.openlibrary.org/a/id/{photos[0]}-L.jpg" if photos else ""
        location = _text_value(data.get("location") or data.get("birth_place"))
        return {
            "profile": _text_value(data.get("bio")),
            "image_url": image_url,
            "total_books_written": bibliography_count or None,
            "nationality": _text_value(data.get("nationality")),
            "hometown": location,
            "source_url": f"https://openlibrary.org/authors/{author_key}",
        }

    def _bibliography_json(author: str, rows: list[dict]) -> list[dict]:
        labels = {
            "imported": "Owned",
            "snatched": "In Progress",
            "wanted": "Wanted",
            "failed": "Failed",
        }
        out = []
        for row in rows:
            d = dict(row)
            if not d.get("series") and "series:" in str(d.get("genre") or "").lower():
                genre_parts = [p.strip() for p in str(d.get("genre") or "").split(",")]
                series_parts = []
                remaining_genres = []
                for part in genre_parts:
                    if part.lower().startswith("series:"):
                        name = part.split(":", 1)[1].strip()
                        if name:
                            series_parts.append(name)
                    elif part:
                        remaining_genres.append(part)
                d["series"] = ", ".join(series_parts[:2])
                d["genre"] = ", ".join(remaining_genres)
            book = _db().get_by_author_title(author, d.get("title", ""))
            d["library_status"] = labels.get(book.status, book.status.title()) if book else ""
            d["library_book_id"] = book.id if book else ""
            out.append(d)
        return out

    # ----- pages -----

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE_HTML

    @app.get("/favicon.svg")
    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(content=_FAVICON_SVG, media_type="image/svg+xml")

    # ----- auth -----

    @app.get("/auth/me")
    def auth_me() -> dict[str, Any]:
        user = _current()
        return {"authenticated": bool(user), "user": _user_json(user) if user else None}

    @app.post("/auth/local")
    def auth_local(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        if not _auth_enabled() or not app.state.cfg.auth.local_admin.enabled:
            raise HTTPException(404, "local admin login is not enabled")
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        user = app.state.users.verify_local_admin(username, password)
        if not user:
            raise HTTPException(401, "invalid username or password")
        response = JSONResponse({"authenticated": True, "user": _user_json(user)})
        response.set_cookie(
            SESSION_COOKIE,
            _sign_session({"user_id": user.id, "auth_type": "local"}, _session_secret()),
            httponly=True,
            samesite="lax",
            secure=False,
        )
        return response

    @app.post("/auth/logout")
    def auth_logout() -> JSONResponse:
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/auth/oidc/start")
    def auth_oidc_start() -> RedirectResponse:
        if not _auth_enabled() or not app.state.cfg.auth.oidc.enabled:
            raise HTTPException(404, "OIDC login is not enabled")
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        url = OIDCClient(app.state.cfg.auth.oidc).authorization_url(state, nonce)
        response = RedirectResponse(url)
        response.set_cookie(
            OIDC_LOGIN_COOKIE,
            _sign_session({"state": state, "nonce": nonce}, _session_secret()),
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=600,
        )
        return response

    @app.get("/auth/oidc/callback")
    def auth_oidc_callback(
        request: Request,
        code: str = Query(default=""),
        state: str = Query(default=""),
    ) -> RedirectResponse:
        if not _auth_enabled() or not app.state.cfg.auth.oidc.enabled:
            raise HTTPException(404, "OIDC login is not enabled")
        login = _unsign_session(request.cookies.get(OIDC_LOGIN_COOKIE, ""), _session_secret())
        if not login or not state or state != login.get("state"):
            raise HTTPException(400, "OIDC state is invalid")
        if not code:
            raise HTTPException(400, "OIDC code is required")
        try:
            claims = OIDCClient(app.state.cfg.auth.oidc).callback_claims(code, str(login.get("nonce") or ""))
        except OIDCError as exc:
            raise HTTPException(401, str(exc)) from exc
        user = app.state.users.upsert_oidc_user(
            issuer=str(claims.get("iss") or app.state.cfg.auth.oidc.issuer),
            subject=str(claims.get("sub") or ""),
            email=str(claims.get("email") or ""),
            display_name=str(claims.get("name") or claims.get("preferred_username") or claims.get("email") or ""),
            preferred_username=str(claims.get("preferred_username") or ""),
        )
        response = RedirectResponse("/")
        response.set_cookie(
            SESSION_COOKIE,
            _sign_session({"user_id": user.id, "auth_type": "oidc"}, _session_secret()),
            httponly=True,
            samesite="lax",
            secure=False,
        )
        response.delete_cookie(OIDC_LOGIN_COOKIE)
        return response

    # ----- admin users -----

    @app.get("/api/admin/users")
    def api_admin_users() -> dict[str, Any]:
        _require_admin()
        return {"users": [_user_json(u) for u in app.state.users.list_users()]}

    @app.post("/api/admin/users/{user_id}/enabled")
    def api_admin_user_enabled(user_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require_admin()
        user = app.state.users.set_user_enabled(user_id, bool(payload.get("enabled")))
        return {"user": _user_json(user)}

    @app.post("/api/admin/users/{user_id}/kindle")
    def api_admin_user_kindle(user_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require_admin()
        if not app.state.users.get_user(user_id):
            raise HTTPException(404, "user not found")
        settings = app.state.users.set_kindle_settings(
            user_id,
            kindle_to=str(payload["kindle_to"]).strip() if "kindle_to" in payload else None,
            send_kindle=bool(payload["send_kindle"]) if "send_kindle" in payload else None,
            setup_complete=bool(payload["setup_complete"]) if "setup_complete" in payload else None,
        )
        return _kindle_json(settings)

    @app.post("/api/admin/effective-user")
    def api_admin_effective_user(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        admin = _require_admin()
        user_id = str(payload.get("user_id") or "").strip()
        selected = app.state.users.get_user(user_id)
        if not selected or not selected.enabled:
            raise HTTPException(404, "user not found")
        response = JSONResponse({"selected_user": _user_json(selected)})
        response.set_cookie(
            SESSION_COOKIE,
            _sign_session(
                {"user_id": admin.id, "auth_type": admin.auth_type, "effective_user_id": selected.id},
                _session_secret(),
            ),
            httponly=True,
            samesite="lax",
            secure=False,
        )
        return response

    @app.get("/api/kindle/settings")
    def api_kindle_settings() -> dict[str, Any]:
        user = _require_user()
        return _kindle_json(app.state.users.get_kindle_settings(user.id))

    @app.post("/api/kindle/settings")
    def api_save_kindle_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        user = _require_user()
        settings = app.state.users.set_kindle_settings(
            user.id,
            kindle_to=str(payload["kindle_to"]).strip() if "kindle_to" in payload else None,
            send_kindle=bool(payload["send_kindle"]) if "send_kindle" in payload else None,
            setup_complete=bool(payload["setup_complete"]) if "setup_complete" in payload else None,
        )
        return _kindle_json(settings)

    @app.post("/api/kindle/test")
    def api_kindle_test() -> dict[str, Any]:
        user = _require_user()
        settings = app.state.users.get_kindle_settings(user.id)
        if not settings.send_kindle:
            raise HTTPException(400, "Send to Kindle is disabled for this user")
        if not settings.kindle_to:
            raise HTTPException(400, "Kindle email is not configured for this user")
        test_dir = app.state.cfg.state_dir / "kindle-tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / f"{user.id}.txt"
        test_file.write_text("This is a Librarry Send to Kindle test document.\n", encoding="utf-8")
        try:
            send_to_kindle(
                app.state.cfg,
                test_file,
                title="Librarry Test Document",
                author="Librarry",
                kindle_to=settings.kindle_to,
                send_kindle=settings.send_kindle,
            )
        except Exception as exc:
            updated = app.state.users.set_kindle_test_status(user.id, f"failed: {exc}")
            raise HTTPException(500, _kindle_json(updated)) from exc
        return _kindle_json(app.state.users.set_kindle_test_status(user.id, "sent"))

    # ----- core data -----

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        cfg = app.state.cfg
        return {
            "counts": _db().counts(),
            "library_dir": str(cfg.library_dir),
            "download_dir": str(cfg.download_dir),
            "version": __version__,
        }

    @app.get("/api/books")
    def api_books(
        status: str | None = Query(default=None),
        limit: int = Query(default=100, le=500),
    ) -> list[dict[str, Any]]:
        db = _db()
        if status:
            books = db.list_by_status(status)[:limit]
        else:
            books = db.list_all(limit=limit)
        return [_book_json(b) for b in books]

    @app.post("/api/books/add")
    def api_add_book(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        author = str(payload.get("author", "")).strip()
        if not title:
            raise HTTPException(400, "title is required")
        book_id = _db().add_manual(title, author)
        return {"added": True, "id": book_id, "title": title, "author": author or "Unknown"}

    @app.post("/api/books/{book_id:path}/resend_kindle")
    def api_resend_kindle(book_id: str) -> dict[str, Any]:
        effective = _effective()
        if not effective:
            raise HTTPException(401, "authentication required")
        settings = app.state.users.get_kindle_settings(effective.id)
        if not settings.send_kindle:
            raise HTTPException(400, "Send to Kindle is disabled for this user")
        if not settings.kindle_to:
            raise HTTPException(400, "Kindle email is not configured for this user")
        book = _db().get(book_id)
        if not book:
            raise HTTPException(404, "book not found")
        if book.status != "imported" or not book.library_path:
            raise HTTPException(400, "book is not imported")
        path = Path(book.library_path)
        if not path.is_file():
            raise HTTPException(404, "book file not found")
        try:
            send_to_kindle(
                app.state.cfg,
                path,
                title=book.title,
                author=book.author,
                kindle_to=settings.kindle_to,
                send_kindle=settings.send_kindle,
            )
        except Exception as exc:
            app.state.users.set_kindle_send_status(effective.id, f"failed: {exc}")
            raise HTTPException(500, f"Kindle send failed: {exc}") from exc
        app.state.users.set_kindle_send_status(effective.id, f"sent: {book.title}")
        return {"sent": True, "book_id": book.id}

    @app.get("/api/books/{book_id:path}")
    def api_book_detail(book_id: str) -> dict[str, Any]:
        book = _db().get(book_id)
        if not book:
            raise HTTPException(404, "book not found")
        return _book_json(book)

    @app.post("/api/books/{book_id:path}/extras")
    def api_book_extras(book_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        book = _db().get(book_id)
        if not book:
            raise HTTPException(404, "book not found")
        _db().set_book_extras(
            book_id,
            notes=str(payload.get("notes", ""))[:10000],
            tags=str(payload.get("tags", ""))[:1000],
            cover_override_url=str(payload.get("cover_override_url", ""))[:2000],
        )
        return _book_json(_db().get(book_id))

    @app.get("/api/authors")
    def api_authors() -> list[dict[str, Any]]:
        return _db().list_authors()

    @app.post("/api/authors/{author:path}/poll-bibliography")
    def api_poll_author_bibliography(author: str) -> dict[str, Any]:
        try:
            resp = requests.get(
                "https://openlibrary.org/search.json",
                params={
                    "author": author,
                    "limit": 100,
                    "fields": "key,title,series,first_publish_year,subject,author_key",
                },
                timeout=20,
            )
            resp.raise_for_status()
            docs = resp.json().get("docs", [])
        except Exception as exc:
            return {
                "author": author,
                "bibliography": _bibliography_json(author, _db().list_author_bibliography(author)),
                "error": str(exc),
            }
        rows = []
        seen: set[str] = set()
        author_key = ""
        for d in docs:
            title = str(d.get("title") or "").strip()
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            if not author_key and d.get("author_key"):
                author_key = str((d.get("author_key") or [""])[0])
            subjects = [str(s) for s in (d.get("subject") or [])[:20]]
            category = _bibliography_category(subjects)
            rows.append(
                {
                    "title": title,
                    "series": _bibliography_series(d.get("series"), subjects),
                    "release_year": d.get("first_publish_year"),
                    "release_date": None,
                    "category": category,
                    "genre": _bibliography_genre(subjects, category),
                    "source": "openlibrary",
                    "source_id": d.get("key") or "",
                    "source_url": f"https://openlibrary.org{d.get('key')}" if d.get("key") else "",
                }
            )
        bibliography = _db().replace_author_bibliography(author, rows)
        profile = _db().get_author_profile(author)
        metadata = _openlibrary_author_metadata(author_key, len(bibliography))
        if bibliography or metadata:
            _db().set_author_profile(
                author,
                profile=metadata.get("profile") or profile.get("profile", ""),
                notes=profile.get("notes", ""),
                tags=profile.get("tags", ""),
                image_url=metadata.get("image_url") or profile.get("image_url", ""),
                total_books_written=metadata.get("total_books_written") or profile.get("total_books_written"),
                nationality=metadata.get("nationality") or profile.get("nationality", ""),
                hometown=metadata.get("hometown") or profile.get("hometown", ""),
                source_url=metadata.get("source_url") or profile.get("source_url", ""),
            )
        return {"author": author, "bibliography": _bibliography_json(author, bibliography)}

    @app.post("/api/authors/{author:path}/bibliography/wanted")
    def api_add_bibliography_to_wanted(author: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        if not title:
            raise HTTPException(400, "title is required")
        row = _db().get_author_bibliography_item(author, title)
        book_id = _db().add_manual(title, author)
        meta = {}
        if row:
            meta = {
                "release_year": row.get("release_year"),
                "release_date": row.get("release_date"),
                "genres": row.get("genre"),
            }
            _db().set_metadata(book_id, {k: v for k, v in meta.items() if v})
        return {"added": True, "id": book_id, "title": title, "author": author, "meta": meta}

    @app.get("/api/authors/{author:path}")
    def api_author_detail(author: str) -> dict[str, Any]:
        profile = _db().get_author_profile(author)
        books = [_book_json(b) for b in _db().list_by_author(author)]
        bibliography = _bibliography_json(author, _db().list_author_bibliography(author))
        return {**profile, "books": books, "bibliography": bibliography}

    @app.post("/api/authors/{author:path}")
    def api_author_profile(author: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        current = _db().get_author_profile(author)
        profile = _db().set_author_profile(
            author,
            profile=current.get("profile", ""),
            notes=str(payload.get("notes", ""))[:10000],
            tags=str(payload.get("tags", ""))[:1000],
            image_url=current.get("image_url", ""),
            total_books_written=current.get("total_books_written"),
            nationality=current.get("nationality", ""),
            hometown=current.get("hometown", ""),
            source_url=current.get("source_url", ""),
        )
        books = [_book_json(b) for b in _db().list_by_author(author)]
        bibliography = _bibliography_json(author, _db().list_author_bibliography(author))
        return {**profile, "books": books, "bibliography": bibliography}

    def _delete_library_file(book) -> bool:
        """Delete a book's file from the library (sandboxed to library_dir)."""
        if not book or not book.library_path:
            return False
        lib = Path(app.state.cfg.library_dir).resolve()
        try:
            p = Path(book.library_path).resolve()
        except OSError:
            return False
        if p != lib and lib not in p.parents:
            return False  # refuse anything outside the library
        try:
            if not p.is_file():
                return False
            p.unlink()
            parent = p.parent
            if parent != lib and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()  # tidy up now-empty author folder
            return True
        except OSError:
            return False

    @app.post("/api/books/{book_id:path}/delete_file")
    def api_delete_file(book_id: str) -> dict[str, Any]:
        """Delete the file from disk but keep the request: book returns to 'wanted'."""
        book = _db().get(book_id)
        if not book:
            raise HTTPException(404, "book not found")
        file_deleted = _delete_library_file(book)
        _db().clear_file(book_id)
        return {"reset": True, "file_deleted": file_deleted}

    @app.delete("/api/books/{book_id:path}")
    def api_delete_book(book_id: str, delete_file: bool = Query(default=False)) -> dict[str, Any]:
        book = _db().get(book_id)
        file_deleted = False
        if delete_file and book:
            file_deleted = _delete_library_file(book)
        n = _db().delete(book_id)
        if not n:
            raise HTTPException(404, "book not found")
        return {"deleted": n, "file_deleted": file_deleted}

    @app.post("/api/books/{book_id:path}/remove")
    def api_remove_book(book_id: str, background: BackgroundTasks, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        action = payload.get("action")
        book = _db().get(book_id)
        if not book:
            raise HTTPException(404, "book not found")
        db = _db()
        if action == "delete_disk":
            fd = _delete_library_file(book)
            db.clear_file(book_id)
            return {"ok": True, "message": "File deleted; book set to Wanted" if fd else "No file on disk; book set to Wanted"}
        if action == "remove_list":
            db.delete(book_id)
            return {"ok": True, "message": "Book removed (file left on disk)"}
        if action == "delete_and_remove":
            fd = _delete_library_file(book)
            db.delete(book_id)
            return {"ok": True, "message": "File deleted and book removed" if fd else "Book removed (no file on disk)"}
        if action == "delete_and_research":
            _delete_library_file(book)
            db.clear_file(book_id)
            background.add_task(_research_one, config_path, book_id)
            return {"ok": True, "message": "File deleted; searching indexers…"}
        raise HTTPException(400, "unknown action")

    @app.get("/api/hardcover/search")
    def api_hardcover_search(q: str = Query(..., min_length=2)) -> dict[str, Any]:
        """Primary book search via Hardcover (Typesense) — returns full metadata in one call."""
        cfg = app.state.cfg
        if not cfg.hardcover_token:
            return {"query": q, "results": [], "error": "Hardcover token not configured"}
        query = (
            "query($q:String!){ search(query:$q, query_type:\"Book\", per_page:12, page:1)"
            "{ results } }"
        )
        try:
            body = hardcover.request(cfg, query, {"q": q}, block=False, timeout=20)
            if body.get("errors"):
                return {"query": q, "results": [], "error": str(body["errors"])}
            hits = ((body.get("data") or {}).get("search") or {}).get("results", {}).get("hits", [])
        except HardcoverRateLimited as exc:
            return {"query": q, "results": [], "error": str(exc)}
        except Exception as exc:
            return {"query": q, "results": [], "error": str(exc)}
        results = []
        for h in hits:
            d = h.get("document", {})
            if not d.get("title"):
                continue
            genres = ", ".join((d.get("genres") or [])[:3]) or None
            series = (d.get("series_names") or [None])[0]
            rating = round(d["rating"], 2) if d.get("rating") else None
            results.append(
                {
                    "id": str(d.get("id")),
                    "title": d.get("title", ""),
                    "author": (d.get("author_names") or ["Unknown"])[0],
                    "year": d.get("release_year"),
                    "cover": (d.get("image") or {}).get("url"),
                    "rating": rating,
                    "genres": genres,
                    "series": series,
                    "has_ebook": d.get("has_ebook"),
                    "slug": d.get("slug"),
                    "meta": {
                        "subtitle": d.get("subtitle"),
                        "series": series,
                        "genres": genres,
                        "rating": rating,
                        "ratings_count": d.get("ratings_count"),
                        "pages": d.get("pages"),
                        "isbn_13": (d.get("isbns") or [None])[0],
                        "release_date": d.get("release_date"),
                        "release_year": d.get("release_year"),
                        "description": (d.get("description") or "").strip()[:1500] or None,
                        "hardcover_slug": d.get("slug"),
                        "cover_url": (d.get("image") or {}).get("url"),
                    },
                }
            )
        return {"query": q, "results": results}

    @app.post("/api/books/add_hardcover")
    def api_add_hardcover(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        hc_id = str(payload.get("id", "")).strip()
        title = str(payload.get("title", "")).strip()
        author = str(payload.get("author", "")).strip()
        if not hc_id or not title:
            raise HTTPException(400, "id and title are required")
        book_id = _db().add_hardcover(hc_id, title, author)
        meta = payload.get("meta") or {}
        if isinstance(meta, dict):
            if payload.get("slug") and "hardcover_slug" not in meta:
                meta["hardcover_slug"] = payload.get("slug")
            if payload.get("cover") and "cover_url" not in meta:
                meta["cover_url"] = payload.get("cover")
            _db().set_metadata(book_id, {k: v for k, v in meta.items() if v is not None})
        return {"added": True, "id": book_id, "title": title, "author": author or "Unknown"}

    @app.get("/api/lookup")
    def api_lookup(q: str = Query(..., min_length=2)) -> dict[str, Any]:
        """Fallback metadata search via OpenLibrary — for books not on Hardcover."""
        try:
            resp = requests.get(
                "https://openlibrary.org/search.json",
                params={"q": q, "limit": 12, "fields": "title,author_name,first_publish_year,cover_i,key"},
                timeout=15,
            )
            resp.raise_for_status()
            docs = resp.json().get("docs", [])
        except Exception as exc:  # network/parse errors shouldn't 500 the UI
            return {"query": q, "results": [], "error": str(exc)}
        results = [
            {
                "title": d.get("title", ""),
                "author": (d.get("author_name") or ["Unknown"])[0],
                "year": d.get("first_publish_year"),
                "cover": (
                    f"https://covers.openlibrary.org/b/id/{d['cover_i']}-S.jpg"
                    if d.get("cover_i")
                    else None
                ),
            }
            for d in docs
            if d.get("title")
        ]
        return {"query": q, "results": results}

    @app.get("/api/releases")
    def api_releases(
        book_id: str | None = Query(default=None),
        title: str | None = Query(default=None),
        author: str | None = Query(default=None),
    ) -> dict[str, Any]:
        cfg = app.state.cfg
        isbns: list[str] = []
        if book_id:
            book = _db().get(book_id)
            if not book:
                raise HTTPException(404, "book not found")
            title, author = book.title, book.author
            isbns = [i for i in (book.isbn_13, book.isbn_10) if i]
        if not title:
            raise HTTPException(400, "book_id or title is required")
        try:
            releases = search_releases(cfg, title, author or "")
        except Exception as exc:
            raise HTTPException(502, f"indexer search failed: {exc}")
        if cfg.libgen_enabled:
            try:
                releases = releases + libgen_worker.libgen_search(cfg, author or "", title, isbns=isbns)
            except Exception:
                pass  # LibGen is best-effort; never fail the indexer search
        if cfg.annas_enabled:
            try:
                releases = releases + annas_worker.annas_search(cfg, author or "", title, isbns=isbns)
            except Exception:
                pass  # Anna's Archive is best-effort too
        releases.sort(key=lambda r: (r["rejected"], -r["score"]))
        return {"title": title, "author": author or "", "releases": releases}

    @app.post("/api/releases/grab")
    def api_grab(payload: dict[str, Any] = Body(...), background: BackgroundTasks = None) -> dict[str, Any]:
        required = ("book_id", "title", "download_url", "protocol", "indexer")
        if not all(payload.get(k) for k in required):
            raise HTTPException(400, f"missing fields; need {', '.join(required)}")
        cfg = _reload_cfg()
        url = str(payload["download_url"])
        # LibGen / Anna's direct downloads → run in background (file fetch can be slow)
        if payload["protocol"] == "direct" and url.startswith("libgen:"):
            try:
                _, host, md5, ext = url.split(":", 3)
            except ValueError:
                raise HTTPException(400, "bad libgen url")
            background.add_task(_libgen_grab_bg, config_path, payload["book_id"], host, md5, ext, payload["title"])
            return {"grabbed": True, "source": "libgen", "queued": True}
        if payload["protocol"] == "direct" and url.startswith("annas:"):
            try:
                _, md5, ext = url.split(":", 2)
            except ValueError:
                raise HTTPException(400, "bad annas url")
            background.add_task(_annas_grab_bg, config_path, payload["book_id"], md5, ext, payload["title"])
            return {"grabbed": True, "source": "annas", "queued": True}
        try:
            download_id = snatch_release(
                cfg,
                _db(),
                book_id=payload["book_id"],
                title=payload["title"],
                download_url=url,
                protocol=payload["protocol"],
                indexer=payload["indexer"],
            )
        except Exception as exc:
            raise HTTPException(502, str(exc))
        return {"grabbed": True, "download_id": download_id}

    # ----- health / check -----

    @app.get("/api/check")
    @app.get("/api/health")
    def api_check() -> dict[str, Any]:
        result = run_checks(_reload_cfg())
        return {
            "ok": result.ok,
            "warn": result.warn,
            "fail": result.fail,
            "success": result.success,
        }

    # ----- tasks / actions -----

    @app.get("/api/tasks")
    def api_tasks() -> dict[str, Any]:
        tasks = [
            {"name": "sync", "label": "Sync Hardcover", "desc": "Pull Want to Read into wanted"},
            {"name": "scan", "label": "Scan library", "desc": "Mark books already on disk as imported (skip re-downloads)"},
            {"name": "search", "label": "Search indexers", "desc": "Find and snatch best releases"},
            {"name": "poll", "label": "Poll downloads", "desc": "Check SAB/qBit for completed grabs"},
            {"name": "libgen", "label": "LibGen fallback", "desc": "Fetch still-wanted books from LibGen"},
            {"name": "import", "label": "Import", "desc": "Move completed downloads to library"},
            {"name": "run", "label": "Run full pipeline", "desc": "sync → search → poll → libgen → import"},
            {"name": "retry", "label": "Retry failed", "desc": "Reset failed books to wanted"},
        ]
        return {"tasks": tasks, "last": app.state.last_action}

    @app.post("/api/actions/{name}")
    def api_action(name: str, background: BackgroundTasks) -> JSONResponse:
        if name == "run":
            cfg = _reload_cfg()
            db = _db()
            effective = _effective()
            kindle_settings = app.state.users.get_kindle_settings(effective.id) if effective else None
            result = {
                "sync": sync_hardcover(cfg, db),
                "scan": scan_library(cfg, db),
                "search": search_wanted(cfg, db),
                "poll": poll_downloads(cfg, db),
                "libgen": fetch_libgen(cfg, db),
                "import": import_ready(cfg, db, kindle_settings=kindle_settings),
            }
            app.state.last_action = {"action": "run", "result": result, "at": _now()}
            return JSONResponse({"action": "run", "result": result})
        if name == "retry":
            n = _db().retry_failed()
            app.state.last_action = {"action": "retry", "reset": n, "at": _now()}
            return JSONResponse({"reset": n})
        fn = _ACTIONS.get(name)
        if not fn:
            raise HTTPException(404, f"Unknown action: {name}")
        cfg = _reload_cfg()
        if name == "import":
            effective = _effective()
            kindle_settings = app.state.users.get_kindle_settings(effective.id) if effective else None
            result = import_ready(cfg, _db(), kindle_settings=kindle_settings)
        else:
            result = fn(cfg, _db())
        app.state.last_action = {"action": name, "result": result, "at": _now()}
        return JSONResponse({"action": name, "result": result})

    # ----- settings (read) -----

    @app.get("/api/config")
    def api_config() -> dict[str, Any]:
        raw = app.state.cfg.raw or {}
        q = (raw.get("quality") or {}).get("ebook") or {}
        kindle = raw.get("kindle") or {}
        search = raw.get("search") or {}
        run = raw.get("run") or {}
        imp = raw.get("import") or {}
        return {
            "formats": {
                "required_extensions": q.get("required_extensions", []),
                "acceptable_extensions": q.get("acceptable_extensions", []),
                "reject_extensions": q.get("reject_extensions", []),
                "reject_patterns": q.get("reject_patterns", []),
                "prefer_patterns": q.get("prefer_patterns", []),
            },
            "email": {
                "smtp_server": kindle.get("smtp_server", "smtp.gmail.com"),
                "smtp_port": kindle.get("smtp_port", 465),
                "use_ssl": kindle.get("use_ssl", True),
                "to": kindle.get("to", ""),
                "from_set": _is_set(kindle.get("from")) or _is_set(kindle.get("from_secret")),
                "user_set": _is_set(kindle.get("user")) or _is_set(kindle.get("user_secret")),
                "password_set": _is_set(kindle.get("password")) or _is_set(kindle.get("password_secret")),
                "send_kindle": bool(imp.get("send_kindle", False)),
            },
            "search": {
                "fuzz_threshold": search.get("fuzz_threshold", 0.45),
                "max_results_per_indexer": search.get("max_results_per_indexer", 25),
                "usenet_before_torrent": search.get("usenet_before_torrent", True),
                "max_snatches_per_run": run.get("max_snatches_per_run", 5),
                "max_imports_per_run": run.get("max_imports_per_run", 10),
            },
            "indexers": {
                "newznab": _indexer_view(raw.get("newznab_indexers")),
                "torznab": _indexer_view(raw.get("torznab_indexers")),
            },
            "clients": _clients_view(raw.get("download_clients")),
            "importlists": _importlists_view(raw),
            "hardcover": {
                "rate_limit_per_minute": (raw.get("hardcover") or {}).get("rate_limit_per_minute", 60),
                "min_interval_seconds": (raw.get("hardcover") or {}).get("min_interval_seconds", 1.0),
            },
            "providers": {
                "libgen": {
                    "enabled": ((raw.get("providers") or {}).get("libgen") or {}).get("enabled", True),
                    "max_per_run": ((raw.get("providers") or {}).get("libgen") or {}).get("max_per_run", 12),
                },
                "annas": {
                    "enabled": ((raw.get("providers") or {}).get("annas") or {}).get("enabled", False),
                    "max_per_run": ((raw.get("providers") or {}).get("annas") or {}).get("max_per_run", 8),
                    "api_key_set": _is_set(((raw.get("providers") or {}).get("annas") or {}).get("api_key"))
                    or _is_set(((raw.get("providers") or {}).get("annas") or {}).get("api_key_secret")),
                },
            },
        }

    @app.post("/api/config/providers")
    def save_providers(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        patch: dict[str, Any] = {"providers": {}}
        lib = payload.get("libgen") or {}
        ann = payload.get("annas") or {}
        if lib:
            lg: dict[str, Any] = {}
            if "enabled" in lib:
                lg["enabled"] = bool(lib["enabled"])
            if "max_per_run" in lib:
                lg["max_per_run"] = max(1, int(lib["max_per_run"]))
            if lg:
                patch["providers"]["libgen"] = lg
        if ann:
            an: dict[str, Any] = {}
            if "enabled" in ann:
                an["enabled"] = bool(ann["enabled"])
            if "max_per_run" in ann:
                an["max_per_run"] = max(1, int(ann["max_per_run"]))
            api_key = str(ann.get("api_key", "")).strip()
            if api_key:
                _vault_set("annas_api_key", api_key)
                an["api_key"] = "secret:annas_api_key"
            if an:
                patch["providers"]["annas"] = an
        if not patch["providers"]:
            raise HTTPException(400, "nothing to save")
        _save_config_patch(patch)
        return {"saved": True}

    @app.post("/api/config/hardcover")
    def save_hardcover(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        hc: dict[str, Any] = {}
        if "rate_limit_per_minute" in payload:
            hc["rate_limit_per_minute"] = max(1, int(payload["rate_limit_per_minute"]))
        if "min_interval_seconds" in payload:
            hc["min_interval_seconds"] = max(0.0, float(payload["min_interval_seconds"]))
        if not hc:
            raise HTTPException(400, "nothing to save")
        _save_config_patch({"hardcover": hc})
        return {"saved": True}

    # ----- settings (write) -----

    @app.post("/api/config/formats")
    def save_formats(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        def _clean(key: str) -> list[str]:
            vals = payload.get(key, [])
            if isinstance(vals, str):
                vals = [v.strip() for v in vals.split(",")]
            return [str(v).strip().lower() for v in vals if str(v).strip()]

        ebook = {
            "required_extensions": _clean("required_extensions"),
            "acceptable_extensions": _clean("acceptable_extensions"),
            "reject_extensions": _clean("reject_extensions"),
            "reject_patterns": _clean("reject_patterns"),
            "prefer_patterns": _clean("prefer_patterns"),
        }
        _save_config_patch({"quality": {"ebook": ebook}})
        return {"saved": True}

    @app.post("/api/config/email")
    def save_email(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        kindle: dict[str, Any] = {}
        if "smtp_server" in payload:
            kindle["smtp_server"] = str(payload["smtp_server"]).strip()
        if "smtp_port" in payload:
            kindle["smtp_port"] = int(payload["smtp_port"])
        if "use_ssl" in payload:
            kindle["use_ssl"] = bool(payload["use_ssl"])
        if "to" in payload:
            kindle["to"] = str(payload["to"]).strip()
        patch: dict[str, Any] = {"kindle": kindle} if kindle else {}
        if "send_kindle" in payload:
            patch["import"] = {"send_kindle": bool(payload["send_kindle"])}
        if not patch:
            raise HTTPException(400, "nothing to save")
        _save_config_patch(patch)
        return {"saved": True}

    @app.post("/api/config/search")
    def save_search(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        search: dict[str, Any] = {}
        if "fuzz_threshold" in payload:
            search["fuzz_threshold"] = float(payload["fuzz_threshold"])
        if "max_results_per_indexer" in payload:
            search["max_results_per_indexer"] = int(payload["max_results_per_indexer"])
        if "usenet_before_torrent" in payload:
            search["usenet_before_torrent"] = bool(payload["usenet_before_torrent"])
        run: dict[str, Any] = {}
        if "max_snatches_per_run" in payload:
            run["max_snatches_per_run"] = int(payload["max_snatches_per_run"])
        if "max_imports_per_run" in payload:
            run["max_imports_per_run"] = int(payload["max_imports_per_run"])
        patch: dict[str, Any] = {}
        if search:
            patch["search"] = search
        if run:
            patch["run"] = run
        if not patch:
            raise HTTPException(400, "nothing to save")
        _save_config_patch(patch)
        return {"saved": True}

    # ----- indexers (add/edit/delete) -----

    @app.post("/api/indexers")
    def save_indexer(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        kind = payload.get("kind")
        if kind not in ("newznab", "torznab"):
            raise HTTPException(400, "kind must be newznab or torznab")
        name = str(payload.get("name", "")).strip()
        host = str(payload.get("host", "")).strip()
        if not name or not host:
            raise HTTPException(400, "name and host are required")
        list_key = f"{kind}_indexers"
        raw = _read_raw()
        items = raw.get(list_key) or []
        entry = next((it for it in items if it.get("name") == name), None)
        if entry is None:
            entry = {"name": name}
            items.append(entry)
        entry["host"] = host.rstrip("/")
        cats = payload.get("book_categories")
        if isinstance(cats, str):
            cats = [int(x) for x in re.split(r"[,\s]+", cats) if x.strip().isdigit()]
        entry["book_categories"] = cats or [7020]
        entry["priority"] = int(payload.get("priority", 0))
        entry["enabled"] = bool(payload.get("enabled", True))
        api_key = str(payload.get("api_key", "")).strip()
        if api_key:
            secret_name = f"idx_{_slug(name)}_api_key"
            _vault_set(secret_name, api_key)
            entry["api_key"] = f"secret:{secret_name}"
        raw[list_key] = items
        _write_raw(raw)
        return {"saved": True, "name": name}

    @app.delete("/api/indexers")
    def delete_indexer(kind: str = Query(...), name: str = Query(...)) -> dict[str, Any]:
        if kind not in ("newznab", "torznab"):
            raise HTTPException(400, "bad kind")
        list_key = f"{kind}_indexers"
        raw = _read_raw()
        items = raw.get(list_key) or []
        new_items = [it for it in items if it.get("name") != name]
        if len(new_items) == len(items):
            raise HTTPException(404, "indexer not found")
        raw[list_key] = new_items
        _write_raw(raw)
        return {"deleted": name}

    # ----- download clients (add/edit/delete) -----

    @app.post("/api/clients")
    def save_client(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        ctype = payload.get("type")
        if not name:
            raise HTTPException(400, "name is required")
        if ctype not in ("sabnzbd", "qbittorrent"):
            raise HTTPException(400, "type must be sabnzbd or qbittorrent")
        raw = _read_raw()
        clients = raw.get("download_clients") or {}
        entry = dict(clients.get(name) or {})
        entry["type"] = ctype
        entry["host"] = str(payload.get("host", "")).strip()
        entry["port"] = int(payload.get("port") or 0)
        entry["category"] = str(payload.get("category", "books")).strip() or "books"
        entry["priority"] = int(payload.get("priority", 10 if ctype == "sabnzbd" else 5))
        entry["enabled"] = bool(payload.get("enabled", True))
        if ctype == "qbittorrent":
            entry["save_path"] = str(payload.get("save_path", "/downloads/books")).strip()
        elif "delete_after" in payload:
            entry["delete_after"] = bool(payload["delete_after"])
        for field in ("username", "password", "api_key"):
            val = str(payload.get(field, "")).strip()
            if val:
                secret_name = f"dc_{_slug(name)}_{field}"
                _vault_set(secret_name, val)
                entry[field] = f"secret:{secret_name}"
        clients[name] = entry
        raw["download_clients"] = clients
        _write_raw(raw)
        return {"saved": True, "name": name}

    @app.delete("/api/clients")
    def delete_client(name: str = Query(...)) -> dict[str, Any]:
        raw = _read_raw()
        clients = raw.get("download_clients") or {}
        if name not in clients:
            raise HTTPException(404, "client not found")
        del clients[name]
        raw["download_clients"] = clients
        _write_raw(raw)
        return {"deleted": name}

    # ----- file explorer -----

    @app.get("/api/files")
    def api_files(root: str = Query(default=""), path: str = Query(default="")) -> dict[str, Any]:
        cfg = app.state.cfg
        roots = {"library": Path(cfg.library_dir), "downloads": Path(cfg.download_dir)}
        roots_meta = [
            {"key": k, "path": str(v), "exists": v.exists()} for k, v in roots.items()
        ]
        if not root:
            return {"roots": roots_meta, "root": "", "path": "", "parent": None, "entries": []}
        base = roots.get(root)
        if base is None:
            raise HTTPException(404, "unknown root")
        base_resolved = base.resolve()
        target = (base_resolved / path).resolve()
        if base_resolved != target and base_resolved not in target.parents:
            raise HTTPException(403, "path outside root")
        if not target.exists() or not target.is_dir():
            raise HTTPException(404, "directory not found")
        entries = []
        try:
            for e in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                is_dir = e.is_dir()
                entries.append(
                    {
                        "name": e.name,
                        "is_dir": is_dir,
                        "size": (e.stat().st_size if not is_dir else None),
                    }
                )
        except PermissionError:
            raise HTTPException(403, "permission denied")
        rel = "" if target == base_resolved else str(target.relative_to(base_resolved)).replace("\\", "/")
        parent = None if not rel else "/".join(rel.split("/")[:-1])
        return {
            "roots": roots_meta,
            "root": root,
            "path": rel,
            "parent": parent,
            "entries": entries,
        }

    # ----- logs -----

    @app.get("/api/logs")
    def api_logs() -> dict[str, Any]:
        d = Path(app.state.cfg.log_dir)
        files = []
        if d.exists():
            for f in sorted(d.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.is_file():
                    stat = f.stat()
                    files.append(
                        {
                            "name": f.name,
                            "size": stat.st_size,
                            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                        }
                    )
        return {"dir": str(d), "files": files}

    @app.get("/api/logs/{name}")
    def api_log(name: str, lines: int = Query(default=300, le=5000)) -> dict[str, Any]:
        if "/" in name or "\\" in name or ".." in name:
            raise HTTPException(400, "invalid name")
        f = Path(app.state.cfg.log_dir) / name
        if not f.is_file():
            raise HTTPException(404, "log not found")
        content = f.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"name": name, "lines": content[-lines:]}

    # ----- about / updates -----

    @app.get("/api/about")
    def api_about() -> dict[str, Any]:
        import platform
        import sys

        cfg = app.state.cfg
        return {
            "version": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "library_dir": str(cfg.library_dir),
            "download_dir": str(cfg.download_dir),
            "database": str(cfg.database),
            "state_dir": str(cfg.state_dir),
            "log_dir": str(cfg.log_dir),
            "config_path": config_path,
            "counts": _db().counts(),
            "source": "https://github.com/  (MIT)",
        }

    @app.get("/api/updates")
    def api_updates() -> dict[str, Any]:
        return {
            "current": __version__,
            "channel": "manual",
            "how_to": [
                "cd Documents/librarry && git pull",
                "pip install -e .",
                "restart the librarry service (docker compose up -d / systemctl restart librarry)",
            ],
            "note": "Librarry updates are pulled from git and reinstalled; there is no auto-updater.",
        }

    return app


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _libgen_grab_bg(config_path: str, book_id: str, host: str, md5: str, ext: str, title: str) -> None:
    cfg = load_config(config_path)
    db = Database(cfg.database)
    db.init()
    try:
        libgen_worker.grab_md5(cfg, db, book_id, host, md5, ext, title)
    except Exception:  # background task — never raise
        pass


def _annas_grab_bg(config_path: str, book_id: str, md5: str, ext: str, title: str) -> None:
    cfg = load_config(config_path)
    db = Database(cfg.database)
    db.init()
    try:
        annas_worker.grab_md5(cfg, db, book_id, md5, ext, title)
    except Exception:  # background task — never raise
        pass


def _research_one(config_path: str, book_id: str) -> None:
    cfg = load_config(config_path)
    db = Database(cfg.database)
    db.init()
    try:
        search_book(cfg, db, book_id)
    except Exception:  # background task — never raise
        pass


def _run_pipeline(config_path: str) -> None:
    cfg = load_config(config_path)
    db = Database(cfg.database)
    db.init()
    sync_hardcover(cfg, db)
    scan_library(cfg, db)
    search_wanted(cfg, db)
    poll_downloads(cfg, db)
    fetch_libgen(cfg, db)
    import_ready(cfg, db)


_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" rx="22" fill="#0f1419"/>
  <path d="M50 33 C41 27 27 27 18 31 L18 71 C27 67 41 67 50 73 Z" fill="#3fb564"/>
  <path d="M50 33 C59 27 73 27 82 31 L82 71 C73 67 59 67 50 73 Z" fill="#2f9c4a"/>
  <rect x="47.5" y="33" width="5" height="40" rx="2" fill="#1f6b34"/>
  <path d="M58 31 L70 31 L70 56 L64 50 L58 56 Z" fill="#e8b84a"/>
</svg>"""


_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Librarry</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <style>
    :root {
      --bg: #0f1419;
      --panel: #1a2332;
      --panel2: #141b27;
      --border: #2d3a4f;
      --text: #e7ecf3;
      --muted: #8b9cb3;
      --accent: #5b9fd4;
      --wanted: #e8b84a;
      --snatched: #7eb8da;
      --imported: #6bc98a;
      --failed: #e07070;
      --sidebar: 240px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    a { color: var(--accent); text-decoration: none; }
    .app { display: flex; min-height: 100vh; }
    /* sidebar */
    aside {
      width: var(--sidebar);
      flex-shrink: 0;
      background: var(--panel2);
      border-right: 1px solid var(--border);
      position: sticky;
      top: 0;
      height: 100vh;
      overflow-y: auto;
      padding: 0 0 2rem;
    }
    .brand { padding: 1.25rem 1.25rem 1rem; font-size: 1.35rem; font-weight: 600; letter-spacing: -0.02em; }
    .brand span { color: var(--accent); }
    .nav-group { margin-top: 0.75rem; }
    .nav-group h4 {
      margin: 0; padding: 0.4rem 1.25rem; font-size: 0.68rem; text-transform: uppercase;
      letter-spacing: 0.08em; color: var(--muted); font-weight: 600;
    }
    .nav-item {
      display: flex; align-items: center; gap: 0.6rem;
      padding: 0.5rem 1.25rem; cursor: pointer; font-size: 0.9rem; color: var(--text);
      border-left: 3px solid transparent;
    }
    .nav-item:hover { background: rgba(91,159,212,0.08); }
    .nav-item.active { background: rgba(91,159,212,0.14); border-left-color: var(--accent); color: var(--accent); }
    .nav-item .ic { width: 1.1rem; text-align: center; opacity: 0.85; }
    /* content */
    main { flex: 1; min-width: 0; }
    .topbar {
      padding: 1rem 1.5rem; border-bottom: 1px solid var(--border);
      display: flex; align-items: center; justify-content: space-between; gap: 1rem;
    }
    .topbar h2 { margin: 0; font-size: 1.25rem; font-weight: 600; }
    .content { padding: 1.5rem; max-width: 1100px; }
    /* buttons */
    button {
      background: var(--panel); border: 1px solid var(--border); color: var(--text);
      padding: 0.45rem 0.85rem; border-radius: 6px; cursor: pointer; font-size: 0.875rem;
    }
    button:hover { border-color: var(--accent); color: var(--accent); }
    button.primary { background: var(--accent); border-color: var(--accent); color: #0f1419; }
    button.primary:hover { filter: brightness(1.1); color: #0f1419; }
    .actions { display: flex; flex-wrap: wrap; gap: 0.5rem; }
    /* cards */
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
    .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 1rem; }
    .card .label { font-size: 0.75rem; text-transform: uppercase; color: var(--muted); }
    .card .value { font-size: 1.75rem; font-weight: 600; }
    .card.wanted .value { color: var(--wanted); }
    .card.snatched .value { color: var(--snatched); }
    .card.imported .value { color: var(--imported); }
    .card.failed .value { color: var(--failed); }
    /* toolbar + table */
    .toolbar { display: flex; gap: 0.5rem; margin-bottom: 1rem; flex-wrap: wrap; align-items: center; }
    select, input[type=text], input[type=number], textarea {
      background: var(--panel); border: 1px solid var(--border); color: var(--text);
      padding: 0.45rem 0.6rem; border-radius: 6px; font-size: 0.9rem;
    }
    table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
    th, td { padding: 0.6rem 1rem; text-align: left; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
    th { font-size: 0.72rem; text-transform: uppercase; color: var(--muted); font-weight: 500; }
    tr:last-child td { border-bottom: none; }
    .status { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.72rem; font-weight: 500; text-transform: uppercase; }
    .status-wanted { background: rgba(232,184,74,0.15); color: var(--wanted); }
    .status-snatched { background: rgba(126,184,218,0.15); color: var(--snatched); }
    .status-imported { background: rgba(107,201,138,0.15); color: var(--imported); }
    .status-failed { background: rgba(224,112,112,0.15); color: var(--failed); }
    .muted { color: var(--muted); font-size: 0.85rem; }
    /* library data table */
    .tablewrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; }
    .tablewrap table { border: none; border-radius: 0; }
    .libtable { white-space: nowrap; }
    .libtable td { max-width: 340px; overflow: hidden; text-overflow: ellipsis; }
    .libtable th.sortable { cursor: pointer; user-select: none; white-space: nowrap; }
    .libtable th.sortable:hover { color: var(--accent); }
    .libtable th .arrow { color: var(--accent); margin-left: 3px; }
    .colmenu { position: relative; display: inline-block; }
    .colmenu-panel { position: absolute; right: 0; top: 112%; background: var(--panel); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.4rem; z-index: 40; display: none; min-width: 200px; max-height: 340px; overflow: auto; }
    .colmenu-panel.open { display: block; }
    .colmenu-panel label { display: flex; align-items: center; gap: 0.5rem; padding: 0.25rem 0.45rem; font-size: 0.85rem; cursor: pointer; }
    .colmenu-panel label:hover { background: rgba(91,159,212,0.08); }
    .colmenu-panel input { width: auto; }
    .pill { display:inline-block; padding:0.1rem 0.5rem; border-radius:999px; font-size:0.72rem; border:1px solid var(--border); }
    .pill.on { color: var(--imported); border-color: rgba(107,201,138,0.5); }
    .pill.off { color: var(--muted); }
    /* settings forms */
    .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1.25rem; }
    .panel h3 { margin: 0 0 1rem; font-size: 1rem; }
    .field { margin-bottom: 1rem; }
    .field label { display: block; font-size: 0.8rem; color: var(--muted); margin-bottom: 0.3rem; }
    .field input[type=text], .field input[type=number], .field textarea { width: 100%; }
    .field textarea { min-height: 7rem; resize: vertical; }
    .detail-grid { display: grid; grid-template-columns: minmax(120px, 170px) minmax(0, 1fr); gap: 1.25rem; align-items: start; }
    .author-detail-layout { display: flex; gap: 1.25rem; align-items: flex-start; overflow-x: auto; padding-bottom: 0.25rem; }
    .author-detail-main { flex: 0 0 520px; }
    .author-detail-bibliography { flex: 0 0 1100px; }
    .detail-cover { width: 100%; max-width: 170px; border-radius: 6px; background: var(--panel2); border: 1px solid var(--border); }
    .book-list { list-style: none; padding: 0; margin: 0; }
    .book-list li { padding: 0.5rem 0; border-bottom: 1px solid var(--border); }
    .book-list li:last-child { border-bottom: none; }
    @media (max-width: 900px) { .author-detail-layout { display: block; overflow-x: visible; } .author-detail-main, .author-detail-bibliography { width: auto; } }
    @media (max-width: 700px) { .detail-grid { grid-template-columns: 1fr; } .detail-cover { max-width: 130px; } }
    .field .hint { font-size: 0.75rem; color: var(--muted); margin-top: 0.25rem; }
    .row { display: flex; gap: 1rem; flex-wrap: wrap; }
    .row .field { flex: 1; min-width: 180px; }
    .check { display: flex; align-items: center; gap: 0.5rem; }
    .check input { width: auto; }
    .health-list { list-style: none; padding: 0; margin: 0; }
    .health-list li { padding: 0.45rem 0.75rem; border-radius: 6px; margin-bottom: 0.4rem; font-size: 0.88rem; }
    .health-list li.ok { background: rgba(107,201,138,0.10); color: var(--imported); }
    .health-list li.warn { background: rgba(232,184,74,0.10); color: var(--wanted); }
    .health-list li.fail { background: rgba(224,112,112,0.12); color: var(--failed); }
    .crumbs { margin-bottom: 1rem; font-size: 0.9rem; }
    .crumbs a { cursor: pointer; }
    .logview { background:#0a0e13; border:1px solid var(--border); border-radius:8px; padding:1rem; font-family: "Cascadia Code", Consolas, monospace; font-size:0.8rem; white-space:pre-wrap; max-height:60vh; overflow:auto; }
    .kv { display:grid; grid-template-columns: 160px 1fr; gap:0.4rem 1rem; font-size:0.9rem; }
    .kv div:nth-child(odd) { color: var(--muted); }
    .kv div, .author-detail-main a { overflow-wrap: anywhere; word-break: break-word; }
    .clickable { cursor: pointer; }
    .clickable:hover { color: var(--accent); }
    #toast {
      position: fixed; bottom: 1.5rem; right: 1.5rem; background: var(--panel); border: 1px solid var(--border);
      padding: 0.75rem 1rem; border-radius: 8px; display: none; max-width: 380px; font-size: 0.875rem; z-index: 50;
    }
    /* modal */
    .overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: none; align-items: flex-start; justify-content: center; z-index: 60; padding: 4vh 1rem; overflow:auto; }
    .overlay.open { display: flex; }
    .modal { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; width: 100%; max-width: 960px; }
    .modal-head { display: flex; justify-content: space-between; align-items: center; padding: 1rem 1.25rem; border-bottom: 1px solid var(--border); }
    .modal-head h3 { margin: 0; font-size: 1.05rem; }
    .modal-body { padding: 1.25rem; max-height: 70vh; overflow: auto; }
    .x { cursor: pointer; color: var(--muted); font-size: 1.25rem; line-height: 1; }
    .x:hover { color: var(--accent); }
    .rejected { opacity: 0.55; }
    .btn-sm { padding: 0.25rem 0.55rem; font-size: 0.78rem; }
    .lookup-row { display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem 0; border-bottom: 1px solid var(--border); }
    .lookup-row img { width: 34px; height: 50px; object-fit: cover; border-radius: 3px; background: var(--panel2); flex-shrink: 0; }
    .lookup-row .meta { flex: 1; min-width: 0; }
    .proto { font-size: 0.68rem; text-transform: uppercase; padding: 0.05rem 0.4rem; border-radius: 3px; border: 1px solid var(--border); }
    .proto.usenet { color: var(--snatched); } .proto.torrent { color: var(--imported); } .proto.direct { color: var(--wanted); }
    /* row action buttons (search / trash) */
    .row-action { padding: 0.36rem 0.66rem; font-size: 0.92rem; line-height: 1; white-space: nowrap; }
    .row-action + .row-action { margin-left: 0.45rem; }
    .row-action.danger { color: var(--failed); border-color: rgba(224,112,112,0.45); }
    .row-action.danger:hover { color: var(--failed); border-color: var(--failed); background: rgba(224,112,112,0.12); }
    /* delete option cards */
    .del-opts { display: flex; flex-direction: column; gap: 0.5rem; }
    .del-opt { display: flex; gap: 0.7rem; align-items: flex-start; padding: 0.7rem 0.9rem; border: 1px solid var(--border); border-radius: 8px; cursor: pointer; }
    .del-opt:hover { border-color: var(--accent); }
    .del-opt.sel { border-color: var(--accent); background: rgba(91,159,212,0.10); }
    .del-opt input { width: auto; margin-top: 0.2rem; }
    /* processing animation */
    .modal-center { text-align: center; padding: 1.5rem 1rem; }
    .spinner { width: 40px; height: 40px; border: 4px solid var(--border); border-top-color: var(--accent); border-radius: 50%; margin: 0.5rem auto 1rem; animation: spin 0.8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .checkmark { width: 46px; height: 46px; display: block; margin: 0.5rem auto 1rem; }
    .checkmark circle { stroke: var(--imported); stroke-width: 3; fill: none; stroke-dasharray: 151; stroke-dashoffset: 151; animation: cmdraw 0.45s ease forwards; }
    .checkmark path { stroke: var(--imported); stroke-width: 4; fill: none; stroke-linecap: round; stroke-dasharray: 48; stroke-dashoffset: 48; animation: cmdraw 0.3s 0.4s ease forwards; }
    @keyframes cmdraw { to { stroke-dashoffset: 0; } }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="brand">Lib<span>rarry</span></div>
      <nav id="nav"></nav>
    </aside>
    <main>
      <div class="topbar">
        <h2 id="view-title">Library</h2>
        <div class="actions" id="view-actions"></div>
      </div>
      <div class="content" id="content"></div>
    </main>
  </div>
  <div id="toast"></div>
  <div class="overlay" id="overlay" onclick="if(event.target===this)closeModal()">
    <div class="modal">
      <div class="modal-head"><h3 id="modal-title">Releases</h3><span class="x" onclick="closeModal()">✕</span></div>
      <div class="modal-body" id="modal-body"></div>
    </div>
  </div>
  <script>
    const NAV = [
      { group: "Library", items: [
        { id: "library", label: "Library", ic: "▤" },
        { id: "authors", label: "Authors", ic: "A" },
        { id: "add", label: "Add Books", ic: "＋" },
        { id: "tasks", label: "Tasks", ic: "⚡" },
      ]},
      { group: "Settings", items: [
        { id: "email", label: "Email Forwarder", ic: "✉" },
        { id: "formats", label: "Formats", ic: "▣" },
        { id: "indexers", label: "Indexers", ic: "⌕" },
        { id: "clients", label: "Download Clients", ic: "▼" },
        { id: "importlists", label: "Import Lists", ic: "☰" },
        { id: "search", label: "Search & Run", ic: "◎" },
      ]},
      { group: "System", items: [
        { id: "health", label: "Health", ic: "♥" },
        { id: "logs", label: "Logs", ic: "▦" },
        { id: "updates", label: "Updates", ic: "↻" },
        { id: "about", label: "About", ic: "ⓘ" },
      ]},
    ];

    function esc(s) { const d = document.createElement('div'); d.textContent = (s===null||s===undefined)?'':s; return d.innerHTML; }
    function toast(msg) {
      const el = document.getElementById('toast');
      el.textContent = msg; el.style.display = 'block';
      clearTimeout(window._tt); window._tt = setTimeout(() => { el.style.display = 'none'; }, 4000);
    }
    async function jget(u) { const r = await fetch(u); if (!r.ok) throw new Error(await r.text()); return r.json(); }
    async function jpost(u, body) {
      const r = await fetch(u, { method: 'POST', headers: {'Content-Type':'application/json'}, body: body ? JSON.stringify(body) : undefined });
      const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || r.statusText); return j;
    }
    function fmtBytes(n) {
      if (n === null || n === undefined) return '';
      const u = ['B','KB','MB','GB','TB']; let i = 0; let v = n;
      while (v >= 1024 && i < u.length-1) { v /= 1024; i++; }
      return v.toFixed(v < 10 && i > 0 ? 1 : 0) + ' ' + u[i];
    }

    function renderNav() {
      const cur = location.hash.replace('#','') || 'library';
      document.getElementById('nav').innerHTML = NAV.map(g => `
        <div class="nav-group">
          <h4>${g.group}</h4>
          ${g.items.map(it => `
            <div class="nav-item ${it.id===cur?'active':''}" onclick="location.hash='${it.id}'">
              <span class="ic">${it.ic}</span><span>${it.label}</span>
            </div>`).join('')}
        </div>`).join('');
    }

    const VIEWS = {};
    function setActions(html) { document.getElementById('view-actions').innerHTML = html || ''; }

    // ---------- Library ----------
    const STATUS_LABEL = { wanted: 'Wanted', snatched: 'In Progress', imported: 'Owned', failed: 'Failed' };
    function trimNum(n) { return (n != null && Number(n) === Math.floor(Number(n))) ? String(Math.floor(Number(n))) : String(n); }
    function baseName(p) { return p ? String(p).split(/[\\\\/]/).pop() : ''; }
    function fileType(b) { return (b.format || (b.library_path ? baseName(b.library_path).split('.').pop() : '') || '').toLowerCase(); }
    function seriesText(b) { return b.series ? b.series + (b.series_position!=null ? ' #' + trimNum(b.series_position) : '') : ''; }
    function shortPath(p) { if (!p) return ''; const s = String(p); return s.length > 68 ? '…' + s.slice(-67) : s; }

    const COLUMNS = [
      { key:'cover_url', label:'Cover', def:true, get:b=>b.cover_display_url||b.cover_url||'',
        cell:b=>{
          const u = b.cover_display_url || b.cover_url;
          return u ? `<img src="${esc(u)}" alt="" style="width:28px;height:42px;object-fit:cover;border-radius:3px;background:var(--panel2)">` : '';
        } },
      { key:'status', label:'Status', def:true, get:b=>STATUS_LABEL[b.status]||b.status,
        cell:b=>`<span class="status status-${esc(b.status)}">${esc(STATUS_LABEL[b.status]||b.status)}</span>` },
      { key:'author', label:'Author', def:true, get:b=>b.author||'',
        cell:b=>`<a href="#author:${encodeURIComponent(b.author||'Unknown')}">${esc(b.author||'Unknown')}</a>` },
      { key:'title', label:'Title', def:true, get:b=>b.title||'',
        cell:b=>`<a href="#book:${encodeURIComponent(b.id)}">${esc(b.title||'')}</a>` },
      { key:'series', label:'Series', def:true, get:b=>seriesText(b) },
      { key:'genres', label:'Genre', def:true, get:b=>b.genres||'' },
      { key:'rating', label:'Rating', num:true, def:true, get:b=>b.rating,
        cell:b=> b.rating!=null ? `${Number(b.rating).toFixed(2)} ★` : '' },
      { key:'pages', label:'Pages', num:true, def:true, get:b=>b.pages },
      { key:'type', label:'Type', def:true, get:b=>fileType(b) },
      { key:'size_bytes', label:'Size', num:true, def:true, get:b=>b.size_bytes,
        cell:b=> b.size_bytes ? fmtBytes(b.size_bytes) : '' },
      { key:'released', label:'Released', def:true, get:b=> b.release_date || (b.release_year!=null?String(b.release_year):'') },
      { key:'language', label:'Language', def:true, get:b=>b.language||'' },
      { key:'publisher', label:'Publisher', def:false, get:b=>b.publisher||'' },
      { key:'ratings_count', label:'# Ratings', num:true, def:false, get:b=>b.ratings_count },
      { key:'isbn_10', label:'ISBN-10', def:false, get:b=>b.isbn_10||'' },
      { key:'isbn_13', label:'ISBN-13', def:false, get:b=>b.isbn_13||'' },
      { key:'hardcover_url', label:'Hardcover', def:true, get:b=>b.hardcover_url||'',
        cell:b=>b.hardcover_url ? `<a href="${esc(b.hardcover_url)}" target="_blank" rel="noopener">Open</a>` : '' },
      { key:'local_path', label:'Local Path', def:true, get:b=>b.local_path||'',
        cell:b=>`<span class="muted" title="${esc(b.local_path||'')}">${esc(shortPath(b.local_path))}</span>` },
      { key:'filename', label:'Filename', def:false, get:b=>baseName(b.library_path) },
      { key:'library_path', label:'Filepath', def:false, get:b=>b.library_path||'',
        cell:b=>`<span class="muted">${esc(b.library_path||'')}</span>` },
      { key:'source', label:'Source', def:false, get:b=>b.source||b.indexer||'' },
      { key:'updated_at', label:'Updated', def:false, get:b=>(b.updated_at||'').replace('T',' ').slice(0,19) },
    ];

    function loadColumnOrder() {
      try {
        const saved = JSON.parse(localStorage.getItem('librarry_col_order'));
        if (Array.isArray(saved) && saved.length) {
          const known = new Set(COLUMNS.map(c=>c.key));
          const merged = saved.filter(k=>known.has(k));
          COLUMNS.forEach(c => { if (!merged.includes(c.key)) merged.push(c.key); });
          return merged;
        }
      } catch(e){}
      return COLUMNS.map(c=>c.key);
    }
    let _colOrder = loadColumnOrder();
    function orderedColumns() {
      return _colOrder.map(k => COLUMNS.find(c=>c.key===k)).filter(Boolean);
    }
    function loadVisibleCols() {
      try { const s = JSON.parse(localStorage.getItem('librarry_cols')); if (Array.isArray(s) && s.length) return new Set(s); } catch(e){}
      return new Set(COLUMNS.filter(c=>c.def).map(c=>c.key));
    }
    let _visibleCols = loadVisibleCols();
    let _sort = { key: 'author', dir: 'asc' };
    window._allBooks = [];

    VIEWS.library = async () => {
      setActions(`
        <button onclick="act('sync')">Sync</button>
        <button onclick="act('scan')">Scan</button>
        <button onclick="act('search')">Search</button>
        <button class="primary" onclick="act('run')">Run all</button>
        <button onclick="act('retry')">Retry failed</button>`);
      const c = document.getElementById('content');
      c.style.maxWidth = 'none';
      c.innerHTML = `
        <div class="cards" id="cards"></div>
        <div class="toolbar">
          <label class="muted">Status</label>
          <select id="filter" onchange="VIEWS.loadBooks()">
            <option value="">All</option>
            <option value="wanted">Wanted</option>
            <option value="snatched">In Progress</option>
            <option value="imported">Owned</option>
            <option value="failed">Failed</option>
          </select>
          <input type="text" id="authorFilter" placeholder="Filter by author…" oninput="VIEWS.renderTable()">
          <button onclick="VIEWS.library()">Refresh</button>
          <div class="colmenu" style="margin-left:auto">
            <button onclick="document.getElementById('colpanel').classList.toggle('open')">Columns ▾</button>
            <div class="colmenu-panel" id="colpanel"></div>
          </div>
        </div>
        <div class="tablewrap"><table class="libtable">
          <thead id="libhead"></thead>
          <tbody id="books"></tbody>
        </table></div>`;
      VIEWS.renderColPanel();
      const st = await jget('/api/status');
      const ct = st.counts || {};
      document.getElementById('cards').innerHTML = [
        ['wanted','Wanted',ct.wanted||0],['snatched','In Progress',ct.snatched||0],
        ['imported','Owned',ct.imported||0],['failed','Failed',ct.failed||0],
      ].map(([k,lbl,v]) => `<div class="card ${k}"><div class="label">${lbl}</div><div class="value">${v}</div></div>`).join('');
      await VIEWS.loadBooks();
    };
    VIEWS.renderColPanel = () => {
      const panel = document.getElementById('colpanel'); if (!panel) return;
      const cols = orderedColumns();
      panel.innerHTML = cols.map((col, idx) => `<div style="display:flex;align-items:center;gap:0.25rem">
          <label style="flex:1"><input type="checkbox" ${_visibleCols.has(col.key)?'checked':''}
            onchange="VIEWS.toggleCol('${col.key}', this.checked)">${esc(col.label)}</label>
          <button class="btn-sm" title="Move left" onclick="VIEWS.moveCol('${col.key}', -1)">↑</button>
          <button class="btn-sm" title="Move right" onclick="VIEWS.moveCol('${col.key}', 1)">↓</button>
        </div>`).join('');
    };

    VIEWS.toggleCol = (key, on) => {
      if (on) _visibleCols.add(key); else _visibleCols.delete(key);
      localStorage.setItem('librarry_cols', JSON.stringify([..._visibleCols]));
      VIEWS.renderTable();
      VIEWS.renderColPanel();
    };
    VIEWS.moveCol = (key, delta) => {
      const i = _colOrder.indexOf(key); if (i < 0) return;
      const j = Math.max(0, Math.min(_colOrder.length - 1, i + delta));
      if (i === j) return;
      const [k] = _colOrder.splice(i, 1); _colOrder.splice(j, 0, k);
      localStorage.setItem('librarry_col_order', JSON.stringify(_colOrder));
      VIEWS.renderColPanel(); VIEWS.renderTable();
    };
    VIEWS.sortBy = (key) => {
      if (_sort.key === key) _sort.dir = _sort.dir === 'asc' ? 'desc' : 'asc';
      else _sort = { key, dir: 'asc' };
      VIEWS.renderTable();
    };

    VIEWS.loadBooks = async () => {
      const f = (document.getElementById('filter')||{}).value || '';
      window._allBooks = await jget(f ? `/api/books?status=${f}` : '/api/books');
      window._books = {}; window._allBooks.forEach(b => window._books[b.id] = b);
      VIEWS.renderTable();
    };

    VIEWS.renderTable = () => {
      const cols = orderedColumns().filter(c => _visibleCols.has(c.key));
      const af = (document.getElementById('authorFilter')||{}).value || '';
      let rows = window._allBooks.slice();
      if (af.trim()) { const q = af.toLowerCase(); rows = rows.filter(b => (b.author||'').toLowerCase().includes(q)); }
      const col = COLUMNS.find(c => c.key === _sort.key) || COLUMNS[1];
      const dir = _sort.dir === 'asc' ? 1 : -1;
      rows.sort((a,b) => {
        let va = col.get(a), vb = col.get(b);
        if (col.num) { va = (va==null||va===''?-Infinity:Number(va)); vb = (vb==null||vb===''?-Infinity:Number(vb)); return (va-vb)*dir; }
        va = (va==null?'':String(va)).toLowerCase(); vb = (vb==null?'':String(vb)).toLowerCase();
        return va < vb ? -dir : va > vb ? dir : 0;
      });
      document.getElementById('libhead').innerHTML = `<tr>${cols.map(c => {
        const arrow = _sort.key===c.key ? `<span class="arrow">${_sort.dir==='asc'?'▲':'▼'}</span>` : '';
        return `<th class="sortable" onclick="VIEWS.sortBy('${c.key}')">${esc(c.label)}${arrow}</th>`;
      }).join('')}<th></th></tr>`;
      document.getElementById('books').innerHTML = rows.map(b => {
        const tds = cols.map(c => `<td>${c.cell ? c.cell(b) : esc(c.get(b))}</td>`).join('');
        return `<tr>${tds}<td style="white-space:nowrap; text-align:right">
            <button class="btn-sm row-action" onclick="openReleases('${esc(b.id)}')" title="Interactive search">⌕ Interactive Search</button>
            <button class="btn-sm row-action danger" onclick="openDelete('${esc(b.id)}')" title="Delete / remove">🗑</button>
          </td></tr>`;
      }).join('') || `<tr><td colspan="${cols.length+1}" class="muted">No books — add some from “Add Books” or run a Hardcover sync.</td></tr>`;
    };
    VIEWS.book = async (id) => {
      setActions(`<button onclick="location.hash='library'">Back to Library</button>`);
      const b = await jget('/api/books/' + encodeURIComponent(id));
      window._detailBookId = id;
      document.getElementById('view-title').textContent = b.title || 'Book';
      const cover = b.cover_display_url || b.cover_url;
      document.getElementById('content').innerHTML = `
        <div class="panel">
          <div class="detail-grid">
            <div>${cover ? `<img class="detail-cover" src="${esc(cover)}" alt="">` : ''}</div>
            <div>
              <h3 style="margin-bottom:0.35rem">${esc(b.title||'Untitled')}</h3>
              <div class="muted" style="margin-bottom:1rem">
                <a href="#author:${encodeURIComponent(b.author||'Unknown')}">${esc(b.author||'Unknown')}</a>
                ${b.series ? ` - ${esc(seriesText(b))}` : ''}
              </div>
              <div class="kv">
                <div>Status</div><div><span class="status status-${esc(b.status)}">${esc(STATUS_LABEL[b.status]||b.status)}</span></div>
                <div>Progress</div><div>${esc(b.progress_stage||'')} ${b.progress_detail ? `<span class="muted">- ${esc(b.progress_detail)}</span>` : ''}</div>
                <div>Local path</div><div class="muted">${esc(b.local_path||'')}</div>
                <div>Hardcover</div><div>${b.hardcover_url ? `<a href="${esc(b.hardcover_url)}" target="_blank" rel="noopener">${esc(b.hardcover_url)}</a>` : ''}</div>
                <div>Format</div><div>${esc(fileType(b))}</div>
                <div>Pages</div><div>${b.pages!=null ? esc(b.pages) : ''}</div>
                <div>Rating</div><div>${b.rating!=null ? esc(Number(b.rating).toFixed(2)) : ''}</div>
                <div>Released</div><div>${esc(b.release_date || b.release_year || '')}</div>
                <div>Genres</div><div>${esc(b.genres||'')}</div>
              </div>
              ${b.status === 'imported' && b.library_path ? `<div class="toolbar" style="margin-top:1rem">
                <button class="primary" onclick="VIEWS.resendKindle('${esc(b.id)}')">Re-send to Kindle</button>
              </div>` : ''}
            </div>
          </div>
        </div>
        ${b.description ? `<div class="panel"><h3>Description</h3><div style="white-space:pre-wrap;line-height:1.5">${esc(b.description)}</div></div>` : ''}
        <div class="panel">
          <h3>Book Notes</h3>
          <div class="field"><label>Tags</label><input type="text" id="book_tags" value="${esc(b.tags||'')}"></div>
          <div class="field"><label>Cover override URL</label><input type="text" id="book_cover_override" value="${esc(b.cover_override_url||'')}"></div>
          <div class="field"><label>Notes</label><textarea id="book_notes">${esc(b.notes||'')}</textarea></div>
          <button class="primary" onclick="VIEWS.saveBookExtras()">Save</button>
        </div>`;
    };

    VIEWS.saveBookExtras = async () => {
      const id = window._detailBookId;
      if (!id) return;
      await jpost('/api/books/' + encodeURIComponent(id) + '/extras', {
        tags: val('book_tags'),
        cover_override_url: val('book_cover_override'),
        notes: val('book_notes'),
      });
      toast('Book saved');
      await VIEWS.book(id);
    };

    VIEWS.resendKindle = async (id) => {
      try {
        await jpost('/api/books/' + encodeURIComponent(id) + '/resend_kindle', {});
        toast('Sent to Kindle');
        await VIEWS.book(id);
      } catch (err) {
        toast('Kindle send failed: ' + err.message);
      }
    };

    VIEWS.authors = async () => {
      setActions(`<button onclick="VIEWS.authors()">Refresh</button>`);
      const authors = await jget('/api/authors');
      document.getElementById('view-title').textContent = 'Authors';
      document.getElementById('content').innerHTML = `
        <div class="tablewrap"><table>
          <thead><tr>
            <th></th><th>Author</th><th>In Librarry</th><th>Owned</th><th>Wanted</th><th>In Progress</th>
            <th>Avg Rating</th><th>Total Written</th><th>Nationality</th><th>Hometown</th><th>Tags</th>
          </tr></thead>
          <tbody>${authors.map(a => `
            <tr>
              <td>${a.image_url ? `<img src="${esc(a.image_url)}" alt="" style="width:32px;height:42px;object-fit:cover;border-radius:4px;background:var(--panel2)">` : ''}</td>
              <td><a href="#author:${encodeURIComponent(a.author)}">${esc(a.author)}</a></td>
              <td>${esc(a.book_count)}</td>
              <td>${esc(a.owned_count)}</td>
              <td>${esc(a.wanted_count)}</td>
              <td>${esc(a.in_progress_count)}</td>
              <td>${a.average_rating!=null ? esc(Number(a.average_rating).toFixed(2)) : ''}</td>
              <td>${a.total_books_written!=null ? esc(a.total_books_written) : ''}</td>
              <td>${esc(a.nationality||'')}</td>
              <td>${esc(a.hometown||'')}</td>
              <td>${esc(a.tags||'')}</td>
            </tr>`).join('') || '<tr><td colspan="11" class="muted">No authors found.</td></tr>'}</tbody>
        </table></div>`;
    };

    VIEWS.author = async (author) => {
      setActions(`<button onclick="location.hash='authors'">Back to Authors</button>`);
      const a = await jget('/api/authors/' + encodeURIComponent(author));
      window._detailAuthor = author;
      document.getElementById('view-title').textContent = a.author || author || 'Author';
      document.getElementById('content').style.maxWidth = 'none';
      const books = (a.books||[]).map(b => `
        <li>
          <a href="#book:${encodeURIComponent(b.id)}">${esc(b.title||'Untitled')}</a>
          <span class="muted">${b.release_year ? ' - ' + esc(b.release_year) : ''} ${b.status ? ' - ' + esc(STATUS_LABEL[b.status]||b.status) : ''}</span>
        </li>`).join('');
      const bibliography = (a.bibliography||[]).map(row => `
        <tr>
          <td>${esc(row.title||'')}</td>
          <td>${esc(row.series||'')}</td>
          <td>${esc(row.release_date || row.release_year || '')}</td>
          <td>${esc(row.category||'')}</td>
          <td>${esc(row.genre||'')}</td>
          <td style="text-align:right">${row.library_status
            ? `<span class="status status-${row.library_status==='Owned'?'imported':row.library_status==='In Progress'?'snatched':row.library_status.toLowerCase()}">${esc(row.library_status)}</span>`
            : `<button class="btn-sm" onclick="VIEWS.addBibliographyWanted('${encodeURIComponent(row.title||'')}')">Add to wanted</button>`}</td>
        </tr>`).join('');
      const meta = [
        ['Total books written', a.total_books_written],
        ['Nationality', a.nationality],
        ['Hometown', a.hometown],
      ].filter(([_, v]) => v !== null && v !== undefined && String(v).trim() !== '');
      const source = a.source_url ? `<div style="margin-top:0.75rem"><div class="muted">Source</div><div><a href="${esc(a.source_url)}" target="_blank" rel="noopener">${esc(a.source_url)}</a></div></div>` : '';
      const stats = [
        ['In Librarry', (a.books||[]).length],
        ['Bibliography', (a.bibliography||[]).length],
      ].map(([label, value]) => `<span class="pill">${esc(label)}: ${esc(value)}</span>`).join(' ');
      const initial = esc((a.author||author||'?').trim().slice(0,1).toUpperCase());
      document.getElementById('content').innerHTML = `
        <div class="author-detail-layout">
          <div class="author-detail-main">
            <div class="panel">
              <div class="detail-grid">
                <div>${a.image_url ? `<img class="detail-cover" src="${esc(a.image_url)}" alt="">` : `<div class="detail-cover" style="aspect-ratio:2/3;display:flex;align-items:center;justify-content:center;font-size:3rem;color:var(--muted)">${initial}</div>`}</div>
                <div>
                  <h3 style="margin-bottom:0.35rem">${esc(a.author||author||'Author')}</h3>
                  <div style="margin-bottom:1rem">${stats}</div>
                  ${a.tags ? `<div class="muted" style="margin-bottom:1rem">${esc(a.tags)}</div>` : ''}
                  <h3>Author Bio</h3>
                  <div style="white-space:pre-wrap;line-height:1.5">${a.profile ? esc(a.profile) : '<span class="muted">No author bio available yet. Poll the bibliography to try to populate it from metadata sources.</span>'}</div>
                  ${meta.length ? `<div class="kv" style="margin-top:1rem">${meta.map(([k,v]) => `<div>${esc(k)}</div><div>${typeof v === 'string' && v.startsWith('<a ') ? v : esc(v)}</div>`).join('')}</div>` : ''}
                  ${source}
                </div>
              </div>
            </div>
            <div class="panel">
              <h3>Author Notes</h3>
              <div class="field"><label>Tags</label><input type="text" id="author_tags" value="${esc(a.tags||'')}"></div>
              <div class="field"><label>Notes</label><textarea id="author_notes">${esc(a.notes||'')}</textarea></div>
              <button class="primary" onclick="VIEWS.saveAuthorProfile()">Save</button>
            </div>
            <div class="panel">
              <h3>Books in Library</h3>
              <ul class="book-list">${books || '<li class="muted">No books found.</li>'}</ul>
            </div>
          </div>
          <div class="author-detail-bibliography">
            <div class="panel">
              <div style="display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-bottom:1rem">
                <h3 style="margin:0">Bibliography</h3>
                <button class="primary" onclick="VIEWS.pollAuthorBibliography()">Poll Author Bibliography</button>
              </div>
              <div class="tablewrap"><table>
                <thead><tr><th>Title</th><th>Series</th><th>Released</th><th>Category</th><th>Genre</th><th></th></tr></thead>
                <tbody>${bibliography || '<tr><td colspan="6" class="muted">No bibliography rows yet. Poll this author to fetch them.</td></tr>'}</tbody>
              </table></div>
            </div>
          </div>
        </div>`;
    };

    VIEWS.saveAuthorProfile = async () => {
      const author = window._detailAuthor;
      if (!author) return;
      await jpost('/api/authors/' + encodeURIComponent(author), {
        tags: val('author_tags'),
        notes: val('author_notes'),
      });
      toast('Author saved');
      await VIEWS.author(author);
    };

    VIEWS.pollAuthorBibliography = async () => {
      const author = window._detailAuthor;
      if (!author) return;
      toast('Polling bibliography...');
      const r = await jpost('/api/authors/' + encodeURIComponent(author) + '/poll-bibliography');
      toast(r.error ? 'Poll finished with error: ' + r.error : 'Bibliography updated');
      await VIEWS.author(author);
    };

    VIEWS.addBibliographyWanted = async (encodedTitle) => {
      const author = window._detailAuthor;
      const title = decodeURIComponent(encodedTitle || '');
      if (!author || !title) return;
      await jpost('/api/authors/' + encodeURIComponent(author) + '/bibliography/wanted', { title });
      toast('Added to wanted');
      await VIEWS.author(author);
    };

    const DEL_OPTS = [
      ['delete_disk', 'Delete from disk', 'Remove the file; keep the book in your list (set to Wanted).'],
      ['remove_list', 'Remove book from list', 'Remove the book from Librarry; leave the file on disk.'],
      ['delete_and_remove', 'Delete from disk and remove from list', 'Remove the file and the book entirely.'],
      ['delete_and_research', 'Delete from disk and re-search', 'Remove the file and immediately search indexers for a replacement.'],
    ];
    window.openDelete = (id) => {
      const b = (window._books||{})[id] || {};
      const opts = DEL_OPTS.map(([v,label,desc]) => `
        <label class="del-opt" onclick="selDelOpt(this)">
          <input type="checkbox" name="delopt" value="${v}">
          <div><strong>${label}</strong><div class="muted">${desc}</div></div>
        </label>`).join('');
      showModal('Delete / Remove — ' + (b.title||id), `
        <div class="muted" style="margin-bottom:1rem">${esc(b.author||'')} — <strong>${esc(b.title||'')}</strong>
          ${b.status?` · <span class="status status-${esc(b.status)}">${esc(STATUS_LABEL[b.status]||b.status)}</span>`:''}</div>
        <div class="del-opts">${opts}</div>
        <button class="primary" style="margin-top:1rem" onclick="confirmDelete('${esc(id)}')">Apply</button>`);
    };
    window.selDelOpt = (lbl) => {
      document.querySelectorAll('.del-opt').forEach(el => el.classList.remove('sel'));
      document.querySelectorAll('input[name=delopt]').forEach(c => c.checked = false);
      lbl.classList.add('sel');
      lbl.querySelector('input').checked = true;
    };
    function modalWorking(text) {
      document.getElementById('modal-body').innerHTML =
        `<div class="modal-center"><div class="spinner"></div><div class="muted">${esc(text||'Working…')}</div></div>`;
    }
    function modalDone(text, cb) {
      document.getElementById('modal-body').innerHTML =
        `<div class="modal-center">
          <svg class="checkmark" viewBox="0 0 52 52"><circle cx="26" cy="26" r="24"/><path d="M14 27 l8 8 l16 -16"/></svg>
          <div>${esc(text||'Done')}</div></div>`;
      setTimeout(cb, 1000);
    }
    window.confirmDelete = async (id) => {
      const sel = document.querySelector('input[name=delopt]:checked');
      if (!sel) { toast('Pick an option'); return; }
      const action = sel.value;
      modalWorking(action === 'delete_and_research' ? 'Deleting file and searching indexers…' : 'Working…');
      try {
        const r = await jpost('/api/books/' + encodeURIComponent(id) + '/remove', { action });
        modalDone(r.message || 'Done', () => { closeModal(); VIEWS.library(); });
      } catch (err) { toast('Failed: ' + err.message); closeModal(); VIEWS.library(); }
    };

    // ---------- release search modal ----------
    function openModal(title) {
      document.getElementById('modal-title').textContent = title;
      document.getElementById('modal-body').innerHTML = '<div class="muted">Searching indexers…</div>';
      document.getElementById('overlay').classList.add('open');
    }
    window.closeModal = () => document.getElementById('overlay').classList.remove('open');
    function showModal(title, html) {
      document.getElementById('modal-title').textContent = title;
      document.getElementById('modal-body').innerHTML = html;
      document.getElementById('overlay').classList.add('open');
    }
    function val(id) { const e = document.getElementById(id); return e ? e.value.trim() : ''; }
    function ageText(d) {
      if (!d) return '';
      const t = Date.parse(d); if (isNaN(t)) return '';
      let s = (Date.now() - t) / 1000;
      for (const [lbl, sec] of [['y',31536000],['mo',2592000],['d',86400],['h',3600],['m',60]]) {
        if (s >= sec) return Math.floor(s/sec) + lbl;
      }
      return 'now';
    }
    window.openReleases = async (id) => {
      const b = (window._books || {})[id] || {};
      openModal(`Releases — ${b.title || id}`);
      try {
        const d = await jget('/api/releases?book_id=' + encodeURIComponent(id));
        const rows = d.releases.map((r, i) => {
          const isT = r.protocol === 'torrent';
          const seed = isT ? (r.seeders != null ? r.seeders : '–') : '—';
          const leech = isT ? (r.leechers != null ? r.leechers : '–') : '—';
          const grabs = r.grabs != null ? r.grabs : '–';
          const peers = isT ? `<span style="color:var(--imported)">${seed}</span> / <span class="muted">${leech}</span>` : '<span class="muted">—</span>';
          return `<tr class="${r.rejected?'rejected':''}">
            <td><span class="proto ${esc(r.protocol)}">${esc(r.protocol)}</span></td>
            <td>${esc(r.title)}</td>
            <td class="muted">${esc(r.indexer)}</td>
            <td>${r.format?`<span class="proto">${esc(r.format)}</span>`:'<span class="muted">—</span>'}</td>
            <td class="muted" title="${esc(r.category||'')}">${fmtBytes(r.size_bytes)}</td>
            <td class="muted" title="${esc(r.pub_date||'')}">${esc(ageText(r.pub_date))}</td>
            <td>${peers}</td>
            <td class="muted">${esc(grabs)}</td>
            <td>${r.score}${r.reason?`<div class="muted" style="font-size:0.72rem">${esc(r.reason)}</div>`:''}</td>
            <td style="text-align:right"><button class="btn-sm ${r.rejected?'':'primary'}" onclick="grab('${esc(id)}',${i})">Grab</button></td>
          </tr>`;
        }).join('');
        window._releases = d.releases;
        document.getElementById('modal-body').innerHTML = d.releases.length ? `
          <div class="tablewrap"><table>
          <thead><tr><th>Type</th><th>Release</th><th>Indexer</th><th>Format</th><th>Size</th><th>Age</th><th>Seed/Leech</th><th>Grabs</th><th>Score</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table></div>
          <div class="muted" style="margin-top:0.6rem">Seed/Leech applies to torrents; Grabs to usenet. Dimmed rows were rejected by quality rules but can still be grabbed manually.</div>`
          : '<div class="muted">No releases found on any enabled indexer.</div>';
      } catch (err) { document.getElementById('modal-body').innerHTML = `<span style="color:var(--failed)">${esc(err.message)}</span>`; }
    };
    window.grab = async (bookId, idx) => {
      const r = (window._releases || [])[idx]; if (!r) return;
      modalWorking(r.protocol === 'direct'
        ? 'Downloading from ' + r.indexer + '…'
        : 'Sending to ' + r.indexer + ' download client…');
      try {
        const res = await jpost('/api/releases/grab', {
          book_id: bookId, title: r.title, download_url: r.download_url, protocol: r.protocol, indexer: r.indexer });
        const msg = res.queued ? r.indexer + ' download started' : 'Snatched: ' + r.title;
        modalDone(msg, () => { closeModal(); VIEWS.library(); });
      } catch (err) { toast('Grab failed: ' + err.message); closeModal(); }
    };

    // ---------- Add Books ----------
    VIEWS.add = async () => {
      setActions('');
      document.getElementById('content').innerHTML = `
        <div class="panel">
          <h3>Search Hardcover <span class="muted" style="font-weight:400">— recommended</span></h3>
          <div class="muted" style="margin-bottom:0.6rem">
            Hardcover is the source of truth and gives full metadata (series, genre, rating, pages, ISBN).
            Browse on <a href="https://hardcover.app/search" target="_blank" rel="noopener">hardcover.app ↗</a>,
            or add to your <a href="https://hardcover.app" target="_blank" rel="noopener">Want&nbsp;to&nbsp;Read</a> list and run a Sync.
          </div>
          <div class="toolbar">
            <input type="text" id="hc_q" placeholder="Title, author, or series…" style="flex:1; min-width:240px"
              onkeydown="if(event.key==='Enter')VIEWS.hcSearch()">
            <button class="primary" onclick="VIEWS.hcSearch()">Search Hardcover</button>
          </div>
          <div id="hc_results" style="margin-top:1rem"></div>
        </div>
        <div class="panel">
          <h3>Fallback: OpenLibrary <span class="muted" style="font-weight:400">— for books not on Hardcover</span></h3>
          <div class="toolbar">
            <input type="text" id="lk_q" placeholder="Title, author, or ISBN…" style="flex:1; min-width:240px"
              onkeydown="if(event.key==='Enter')VIEWS.lookup()">
            <button onclick="VIEWS.lookup()">Search OpenLibrary</button>
          </div>
          <div class="muted">No metadata beyond title/author; queues the book as <em>wanted</em>.</div>
          <div id="lk_results" style="margin-top:1rem"></div>
        </div>
        <div class="panel">
          <h3>Add manually</h3>
          <div class="row">
            <div class="field"><label>Title</label><input type="text" id="m_title"></div>
            <div class="field"><label>Author</label><input type="text" id="m_author"></div>
          </div>
          <button onclick="VIEWS.addManual()">Add to wanted</button>
        </div>`;
    };
    function _lookupRow(r, i, fn, badges) {
      return `<div class="lookup-row">
        ${r.cover ? `<img src="${esc(r.cover)}" alt="">` : '<img alt="">'}
        <div class="meta"><strong>${esc(r.title)}</strong>
          <div class="muted">${esc(r.author)}${r.year?` · ${esc(r.year)}`:''}${badges||''}</div></div>
        <button class="btn-sm primary" onclick="${fn}(${i})">Add</button>
      </div>`;
    }
    VIEWS.hcSearch = async () => {
      if (window._hcBusy) return;            // guard against rapid repeat requests
      const q = (document.getElementById('hc_q').value||'').trim();
      if (q.length < 2) { toast('Enter a search term'); return; }
      window._hcBusy = true;
      const box = document.getElementById('hc_results');
      box.innerHTML = '<div class="muted">Searching Hardcover…</div>';
      try {
        const d = await jget('/api/hardcover/search?q=' + encodeURIComponent(q));
        if (d.error) { box.innerHTML = `<span style="color:var(--failed)">Hardcover: ${esc(d.error)}</span>`; return; }
        window._hc = d.results;
        box.innerHTML = d.results.map((r, i) => {
          const badges = `${r.series?` · ${esc(r.series)}`:''}${r.rating?` · ★${esc(r.rating)}`:''}${r.genres?` · ${esc(r.genres)}`:''}${r.has_ebook?` · <span class="pill on">ebook</span>`:''}`;
          return _lookupRow(r, i, 'VIEWS.addFromHardcover', badges);
        }).join('') || '<div class="muted">No matches on Hardcover.</div>';
      } catch (err) { box.innerHTML = `<span style="color:var(--failed)">${esc(err.message)}</span>`; }
      finally { setTimeout(() => { window._hcBusy = false; }, 800); }
    };
    VIEWS.addFromHardcover = async (i) => {
      const r = (window._hc || [])[i]; if (!r) return;
      try { await jpost('/api/books/add_hardcover', { id: r.id, title: r.title, author: r.author, slug: r.slug, cover: r.cover, meta: r.meta });
        toast('Added (with metadata): ' + r.title); }
      catch (err) { toast('Add failed: ' + err.message); }
    };
    VIEWS.lookup = async () => {
      const q = document.getElementById('lk_q').value.trim();
      if (q.length < 2) { toast('Enter a search term'); return; }
      const box = document.getElementById('lk_results');
      box.innerHTML = '<div class="muted">Searching…</div>';
      try {
        const d = await jget('/api/lookup?q=' + encodeURIComponent(q));
        if (d.error) { box.innerHTML = `<span style="color:var(--failed)">Lookup failed: ${esc(d.error)}</span>`; return; }
        window._lk = d.results;
        box.innerHTML = d.results.map((r, i) => _lookupRow(r, i, 'VIEWS.addFromLookup', '')).join('') || '<div class="muted">No matches.</div>';
      } catch (err) { box.innerHTML = `<span style="color:var(--failed)">${esc(err.message)}</span>`; }
    };
    VIEWS.addFromLookup = async (i) => {
      const r = (window._lk || [])[i]; if (!r) return;
      try { await jpost('/api/books/add', { title: r.title, author: r.author }); toast('Added: ' + r.title); }
      catch (err) { toast('Add failed: ' + err.message); }
    };
    VIEWS.addManual = async () => {
      const title = document.getElementById('m_title').value.trim();
      const author = document.getElementById('m_author').value.trim();
      if (!title) { toast('Title is required'); return; }
      try {
        await jpost('/api/books/add', { title, author });
        toast('Added: ' + title);
        document.getElementById('m_title').value = ''; document.getElementById('m_author').value = '';
      } catch (err) { toast('Add failed: ' + err.message); }
    };

    // ---------- Tasks ----------
    VIEWS.tasks = async () => {
      setActions('');
      const d = await jget('/api/tasks');
      const last = d.last && d.last.action
        ? `<div class="panel"><h3>Last run</h3><div class="kv">
             <div>Action</div><div>${esc(d.last.action)}</div>
             <div>When</div><div>${esc(d.last.at||'')}</div>
             <div>Result</div><div>${esc(JSON.stringify(d.last.result ?? d.last.reset ?? d.last.started ?? ''))}</div>
           </div></div>` : '';
      document.getElementById('content').innerHTML = `${last}
        <div class="panel"><h3>Run a task</h3>
          ${d.tasks.map(t => `
            <div class="toolbar" style="justify-content:space-between">
              <div><strong>${esc(t.label)}</strong><div class="muted">${esc(t.desc)}</div></div>
              <button class="${t.name==='run'?'primary':''}" onclick="act('${t.name}')">Run</button>
            </div>`).join('')}
        </div>`;
    };

    // ---------- Email forwarder ----------
    VIEWS.email = async () => {
      setActions('');
      const cfg = await jget('/api/config');
      const e = cfg.email;
      const sec = (set) => set ? '<span class="pill on">set (vault)</span>' : '<span class="pill off">not set</span>';
      document.getElementById('content').innerHTML = `
        <div class="panel">
          <h3>Kindle / Email Forwarder</h3>
          <div class="field check"><input type="checkbox" id="e_send" ${e.send_kindle?'checked':''}>
            <label style="margin:0">Send imported books to Kindle by email</label></div>
          <div class="row">
            <div class="field"><label>SMTP server</label><input type="text" id="e_server" value="${esc(e.smtp_server)}"></div>
            <div class="field"><label>SMTP port</label><input type="number" id="e_port" value="${esc(e.smtp_port)}"></div>
          </div>
          <div class="field check"><input type="checkbox" id="e_ssl" ${e.use_ssl?'checked':''}>
            <label style="margin:0">Use SSL</label></div>
          <div class="field"><label>Deliver to (Kindle address)</label><input type="text" id="e_to" value="${esc(e.to)}"></div>
          <div class="field"><label>Credentials (managed in encrypted vault)</label>
            <div class="kv">
              <div>From address</div><div>${sec(e.from_set)}</div>
              <div>SMTP user</div><div>${sec(e.user_set)}</div>
              <div>SMTP password</div><div>${sec(e.password_set)}</div>
            </div>
            <div class="hint">Set secrets with: <code>librarry secrets set kindle_smtp_from|kindle_smtp_user|kindle_smtp_password</code></div>
          </div>
          <button class="primary" onclick="VIEWS.saveEmail()">Save</button>
        </div>`;
    };
    VIEWS.saveEmail = async () => {
      try {
        await jpost('/api/config/email', {
          send_kindle: document.getElementById('e_send').checked,
          smtp_server: document.getElementById('e_server').value,
          smtp_port: Number(document.getElementById('e_port').value),
          use_ssl: document.getElementById('e_ssl').checked,
          to: document.getElementById('e_to').value,
        });
        toast('Email settings saved');
      } catch (err) { toast('Save failed: ' + err.message); }
    };

    // ---------- Formats ----------
    VIEWS.formats = async () => {
      setActions('');
      const cfg = await jget('/api/config');
      const f = cfg.formats;
      const fld = (id,label,val,hint) => `
        <div class="field"><label>${label}</label>
          <input type="text" id="${id}" value="${esc((val||[]).join(', '))}">
          <div class="hint">${hint}</div></div>`;
      document.getElementById('content').innerHTML = `
        <div class="panel">
          <h3>Accepted Formats &amp; Quality</h3>
          ${fld('f_req','Required extensions', f.required_extensions, 'A release must contain one of these (e.g. epub, azw3, mobi)')}
          ${fld('f_acc','Acceptable extensions', f.acceptable_extensions, 'Allowed as fallback (e.g. pdf, fb2)')}
          ${fld('f_rej','Rejected extensions', f.reject_extensions, 'Never grab these (e.g. m4a, mp3, m4b)')}
          ${fld('f_rejp','Reject title patterns', f.reject_patterns, 'Skip releases whose title contains these words')}
          ${fld('f_prefp','Prefer title patterns', f.prefer_patterns, 'Rank releases containing these higher')}
          <button class="primary" onclick="VIEWS.saveFormats()">Save</button>
        </div>`;
    };
    VIEWS.saveFormats = async () => {
      const parse = id => document.getElementById(id).value.split(',').map(s=>s.trim()).filter(Boolean);
      try {
        await jpost('/api/config/formats', {
          required_extensions: parse('f_req'),
          acceptable_extensions: parse('f_acc'),
          reject_extensions: parse('f_rej'),
          reject_patterns: parse('f_rejp'),
          prefer_patterns: parse('f_prefp'),
        });
        toast('Formats saved');
      } catch (err) { toast('Save failed: ' + err.message); }
    };

    // ---------- Indexers ----------
    VIEWS.indexers = async () => {
      setActions(`<button class="primary" onclick="VIEWS.indexerForm('newznab')">+ Newznab</button>
        <button class="primary" onclick="VIEWS.indexerForm('torznab')">+ Torznab</button>`);
      const cfg = await jget('/api/config'); window._cfgCache = cfg;
      const tbl = (kind, title, rows) => `
        <div class="panel"><h3>${title}</h3>
          <table><thead><tr><th>Name</th><th>Host</th><th>Cats</th><th>Priority</th><th>API key</th><th>Enabled</th><th></th></tr></thead>
          <tbody>${rows.map((i,idx) => `<tr>
            <td>${esc(i.name)}</td><td class="muted">${esc(i.host)}</td>
            <td>${esc((i.categories||[]).join(', '))}</td><td>${esc(i.priority)}</td>
            <td>${i.has_key?'<span class="pill on">set</span>':'<span class="pill off">missing</span>'}</td>
            <td><span class="pill ${i.enabled?'on':'off'}">${i.enabled?'yes':'no'}</span></td>
            <td style="text-align:right; white-space:nowrap">
              <button class="btn-sm" onclick="VIEWS.indexerForm('${kind}',${idx})">Edit</button>
              <button class="btn-sm" onclick="VIEWS.delIndexer('${kind}','${esc(i.name)}')">✕</button></td>
          </tr>`).join('') || '<tr><td colspan="7" class="muted">None configured</td></tr>'}</tbody></table>
        </div>`;
      const p = cfg.providers || {libgen:{enabled:true,max_per_run:12}, annas:{enabled:false,max_per_run:8,api_key_set:false}};
      const providers = `
        <div class="panel"><h3>Direct download providers</h3>
          <div class="muted" style="margin-bottom:0.8rem">Free fallbacks searched alongside your indexers in the interactive search. LibGen needs no account; Anna's Archive fast downloads need a member API key.</div>
          <div class="row" style="align-items:flex-end">
            <div class="field check" style="flex:0 0 auto"><input type="checkbox" id="pv_lg_en" ${p.libgen.enabled?'checked':''}><label style="margin:0"><strong>LibGen</strong></label></div>
            <div class="field"><label>Max auto-fetch per run</label><input type="number" min="1" id="pv_lg_max" value="${esc(p.libgen.max_per_run)}"></div>
          </div>
          <hr style="border:none; border-top:1px solid var(--border); margin:0.6rem 0 1rem">
          <div class="row" style="align-items:flex-end">
            <div class="field check" style="flex:0 0 auto"><input type="checkbox" id="pv_an_en" ${p.annas.enabled?'checked':''}><label style="margin:0"><strong>Anna's Archive</strong></label></div>
            <div class="field"><label>Max auto-fetch per run</label><input type="number" min="1" id="pv_an_max" value="${esc(p.annas.max_per_run)}"></div>
          </div>
          <div class="field"><label>Anna's member API key ${p.annas.api_key_set?'<span class="pill on">set</span>':'<span class="pill off">not set</span>'} <span class="muted">(blank = keep)</span></label>
            <input type="text" id="pv_an_key" placeholder="${p.annas.api_key_set?'•••••• stored in vault':'member API key for fast downloads'}"></div>
          <button class="primary" onclick="VIEWS.saveProviders()">Save providers</button>
        </div>`;
      document.getElementById('content').innerHTML =
        tbl('newznab','Newznab (Usenet)', cfg.indexers.newznab) +
        tbl('torznab','Torznab (Torrents)', cfg.indexers.torznab) +
        providers +
        `<div class="muted">Indexer API keys are stored in the encrypted vault. When editing, leave the key blank to keep the existing one.</div>`;
    };
    VIEWS.saveProviders = async () => {
      try {
        await jpost('/api/config/providers', {
          libgen: { enabled: document.getElementById('pv_lg_en').checked, max_per_run: Number(val('pv_lg_max')||12) },
          annas: { enabled: document.getElementById('pv_an_en').checked, max_per_run: Number(val('pv_an_max')||8), api_key: val('pv_an_key') },
        });
        toast('Providers saved'); VIEWS.indexers();
      } catch (err) { toast('Save failed: ' + err.message); }
    };
    VIEWS.indexerForm = (kind, idx) => {
      const cfg = window._cfgCache || {indexers:{newznab:[],torznab:[]}};
      const i = (idx!=null) ? cfg.indexers[kind][idx]
        : {name:'',host:'',categories:[7020],priority:kind==='newznab'?10:5,enabled:true,has_key:false};
      showModal((idx!=null?'Edit ':'Add ')+kind+' indexer', `
        <div class="field"><label>Name</label><input type="text" id="ix_name" value="${esc(i.name||'')}" ${idx!=null?'readonly':''}></div>
        <div class="field"><label>Host (base URL)</label><input type="text" id="ix_host" value="${esc(i.host||'')}" placeholder="https://api.nzbgeek.info"></div>
        <div class="field"><label>API key ${i.has_key?'<span class="muted">(leave blank to keep)</span>':''}</label>
          <input type="text" id="ix_key" placeholder="${i.has_key?'•••••• stored in vault':'paste API key'}"></div>
        <div class="row">
          <div class="field"><label>Book categories</label><input type="text" id="ix_cats" value="${esc((i.categories||[7020]).join(', '))}"></div>
          <div class="field"><label>Priority</label><input type="number" id="ix_prio" value="${esc(i.priority!=null?i.priority:0)}"></div>
        </div>
        <div class="field check"><input type="checkbox" id="ix_en" ${i.enabled?'checked':''}><label style="margin:0">Enabled</label></div>
        <button class="primary" onclick="VIEWS.saveIndexer('${kind}')">Save</button>`);
    };
    VIEWS.saveIndexer = async (kind) => {
      try {
        await jpost('/api/indexers', { kind, name: val('ix_name'), host: val('ix_host'),
          api_key: val('ix_key'), book_categories: val('ix_cats'),
          priority: Number(val('ix_prio')||0), enabled: document.getElementById('ix_en').checked });
        closeModal(); toast('Indexer saved'); VIEWS.indexers();
      } catch (err) { toast('Save failed: ' + err.message); }
    };
    VIEWS.delIndexer = async (kind, name) => {
      if (!confirm('Delete indexer '+name+'?')) return;
      try { await fetch(`/api/indexers?kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(name)}`, {method:'DELETE'});
        toast('Deleted'); VIEWS.indexers(); } catch (err) { toast('Delete failed: ' + err.message); }
    };

    // ---------- Download clients ----------
    VIEWS.clients = async () => {
      setActions(`<button class="primary" onclick="VIEWS.clientForm()">+ Add client</button>`);
      const cfg = await jget('/api/config'); window._cfgCache = cfg;
      document.getElementById('content').innerHTML = `
        <div class="panel"><h3>Download Clients</h3>
          <table><thead><tr><th>Name</th><th>Type</th><th>Host</th><th>Port</th><th>Category</th><th>Priority</th><th>Enabled</th><th></th></tr></thead>
          <tbody>${cfg.clients.map((c,idx) => `<tr>
            <td>${esc(c.name)}</td><td>${esc(c.type)}</td><td class="muted">${esc(c.host)}</td>
            <td>${esc(c.port)}</td><td>${esc(c.category)}</td><td>${esc(c.priority)}</td>
            <td><span class="pill ${c.enabled?'on':'off'}">${c.enabled?'yes':'no'}</span></td>
            <td style="text-align:right; white-space:nowrap">
              <button class="btn-sm" onclick="VIEWS.clientForm(${idx})">Edit</button>
              <button class="btn-sm" onclick="VIEWS.delClient('${esc(c.name)}')">✕</button></td>
          </tr>`).join('') || '<tr><td colspan="8" class="muted">None configured</td></tr>'}</tbody></table>
          <div class="muted" style="margin-top:0.75rem">Credentials are stored in the encrypted vault. Use Health to test connectivity.</div>
        </div>`;
    };
    VIEWS.clientForm = (idx) => {
      const cfg = window._cfgCache || {clients:[]};
      const c = (idx!=null) ? cfg.clients[idx]
        : {name:'',type:'sabnzbd',host:'',port:'',category:'books',priority:10,enabled:true,save_path:'/downloads/books'};
      showModal((idx!=null?'Edit ':'Add ')+'download client', `
        <div class="row">
          <div class="field"><label>Name</label><input type="text" id="dc_name" value="${esc(c.name||'')}" ${idx!=null?'readonly':''}></div>
          <div class="field"><label>Type</label>
            <select id="dc_type" onchange="VIEWS.clientTypeToggle()" style="width:100%">
              <option value="sabnzbd" ${c.type==='sabnzbd'?'selected':''}>SABnzbd (usenet)</option>
              <option value="qbittorrent" ${c.type==='qbittorrent'?'selected':''}>qBittorrent (torrent)</option>
            </select></div>
        </div>
        <div class="row">
          <div class="field"><label>Host</label><input type="text" id="dc_host" value="${esc(c.host||'')}" placeholder="192.168.1.212"></div>
          <div class="field"><label>Port</label><input type="number" id="dc_port" value="${esc(c.port||'')}"></div>
        </div>
        <div class="row">
          <div class="field"><label>Username <span class="muted">(blank=keep)</span></label><input type="text" id="dc_user"></div>
          <div class="field"><label>Password <span class="muted">(blank=keep)</span></label><input type="text" id="dc_pass"></div>
        </div>
        <div class="field" id="dc_apikey_field"><label>API key <span class="muted">(SABnzbd; blank=keep)</span></label><input type="text" id="dc_api"></div>
        <div class="field" id="dc_savepath_field"><label>Save path (qBittorrent)</label><input type="text" id="dc_save" value="${esc(c.save_path||'/downloads/books')}"></div>
        <div class="row">
          <div class="field"><label>Category</label><input type="text" id="dc_cat" value="${esc(c.category||'books')}"></div>
          <div class="field"><label>Priority</label><input type="number" id="dc_prio" value="${esc(c.priority!=null?c.priority:10)}"></div>
        </div>
        <div class="field check"><input type="checkbox" id="dc_en" ${c.enabled?'checked':''}><label style="margin:0">Enabled</label></div>
        <button class="primary" onclick="VIEWS.saveClient()">Save</button>`);
      VIEWS.clientTypeToggle();
    };
    VIEWS.clientTypeToggle = () => {
      const t = document.getElementById('dc_type').value;
      document.getElementById('dc_apikey_field').style.display = (t==='sabnzbd')?'':'none';
      document.getElementById('dc_savepath_field').style.display = (t==='qbittorrent')?'':'none';
    };
    VIEWS.saveClient = async () => {
      const t = document.getElementById('dc_type').value;
      const body = { name: val('dc_name'), type: t, host: val('dc_host'), port: Number(val('dc_port')||0),
        category: val('dc_cat'), priority: Number(val('dc_prio')||0), enabled: document.getElementById('dc_en').checked,
        username: val('dc_user'), password: val('dc_pass') };
      if (t==='sabnzbd') body.api_key = val('dc_api');
      if (t==='qbittorrent') body.save_path = val('dc_save');
      try { await jpost('/api/clients', body); closeModal(); toast('Client saved'); VIEWS.clients(); }
      catch (err) { toast('Save failed: ' + err.message); }
    };
    VIEWS.delClient = async (name) => {
      if (!confirm('Delete client '+name+'?')) return;
      try { await fetch('/api/clients?name='+encodeURIComponent(name), {method:'DELETE'}); toast('Deleted'); VIEWS.clients(); }
      catch (err) { toast('Delete failed: ' + err.message); }
    };

    // ---------- Import lists ----------
    VIEWS.importlists = async () => {
      setActions('');
      const cfg = await jget('/api/config');
      const hc = cfg.hardcover || {rate_limit_per_minute:60, min_interval_seconds:1.0};
      document.getElementById('content').innerHTML = `
        <div class="panel"><h3>Import Lists</h3>
          <table><thead><tr><th>Name</th><th>Type</th><th>Endpoint</th><th>Want status</th><th>Configured</th></tr></thead>
          <tbody>${cfg.importlists.map(l => `<tr>
            <td>${esc(l.name)}</td><td>${esc(l.type)}</td><td class="muted">${esc(l.api_url)}</td>
            <td>${esc(l.want_status_id)}</td>
            <td>${l.configured?'<span class="pill on">token set</span>':'<span class="pill off">no token</span>'}</td>
          </tr>`).join('')}</tbody></table>
          <div class="muted" style="margin-top:0.75rem">Librarry pulls "Want to Read" from Hardcover. Run a sync from the Tasks page.</div>
        </div>
        <div class="panel"><h3>Hardcover API rate limit</h3>
          <div class="muted" style="margin-bottom:0.8rem">All Hardcover requests (sync + search) share one throttle so we stay polite and avoid getting rate-limited or banned. Lower = gentler on their server.</div>
          <div class="row">
            <div class="field"><label>Max requests per minute</label><input type="number" min="1" id="hc_rpm" value="${esc(hc.rate_limit_per_minute)}"></div>
            <div class="field"><label>Min seconds between requests</label><input type="number" step="0.1" min="0" id="hc_int" value="${esc(hc.min_interval_seconds)}"></div>
          </div>
          <button class="primary" onclick="VIEWS.saveHardcover()">Save</button>
        </div>`;
    };
    VIEWS.saveHardcover = async () => {
      try {
        await jpost('/api/config/hardcover', {
          rate_limit_per_minute: Number(val('hc_rpm')),
          min_interval_seconds: Number(val('hc_int')),
        });
        toast('Hardcover rate limit saved');
      } catch (err) { toast('Save failed: ' + err.message); }
    };

    // ---------- Search & Run ----------
    VIEWS.search = async () => {
      setActions('');
      const cfg = await jget('/api/config');
      const s = cfg.search;
      document.getElementById('content').innerHTML = `
        <div class="panel"><h3>Search</h3>
          <div class="row">
            <div class="field"><label>Fuzzy match threshold (0–1)</label><input type="number" step="0.05" min="0" max="1" id="s_fuzz" value="${esc(s.fuzz_threshold)}"></div>
            <div class="field"><label>Max results per indexer</label><input type="number" id="s_max" value="${esc(s.max_results_per_indexer)}"></div>
          </div>
          <div class="field check"><input type="checkbox" id="s_usenet" ${s.usenet_before_torrent?'checked':''}>
            <label style="margin:0">Prefer Usenet before torrents</label></div>
        </div>
        <div class="panel"><h3>Pipeline limits</h3>
          <div class="row">
            <div class="field"><label>Max snatches per run</label><input type="number" id="s_snatch" value="${esc(s.max_snatches_per_run)}"></div>
            <div class="field"><label>Max imports per run</label><input type="number" id="s_imp" value="${esc(s.max_imports_per_run)}"></div>
          </div>
          <button class="primary" onclick="VIEWS.saveSearch()">Save</button>
        </div>`;
    };
    VIEWS.saveSearch = async () => {
      try {
        await jpost('/api/config/search', {
          fuzz_threshold: Number(document.getElementById('s_fuzz').value),
          max_results_per_indexer: Number(document.getElementById('s_max').value),
          usenet_before_torrent: document.getElementById('s_usenet').checked,
          max_snatches_per_run: Number(document.getElementById('s_snatch').value),
          max_imports_per_run: Number(document.getElementById('s_imp').value),
        });
        toast('Search settings saved');
      } catch (err) { toast('Save failed: ' + err.message); }
    };

    // ---------- Health ----------
    VIEWS.health = async () => {
      setActions('<button onclick="VIEWS.health()">Re-run checks</button>');
      document.getElementById('content').innerHTML = '<div class="muted">Running checks…</div>';
      const h = await jget('/api/health');
      const section = (cls, title, items) => items.length ? `
        <div class="panel"><h3>${title} (${items.length})</h3>
          <ul class="health-list">${items.map(i => `<li class="${cls}">${esc(i)}</li>`).join('')}</ul></div>` : '';
      document.getElementById('content').innerHTML =
        `<div class="panel"><h3>Overall</h3>${h.success
          ? '<span class="pill on">healthy</span>' : '<span class="pill off" style="color:var(--failed)">problems found</span>'}</div>` +
        section('fail','Failures', h.fail) + section('warn','Warnings', h.warn) + section('ok','OK', h.ok);
    };

    // ---------- Logs (with live tail) ----------
    VIEWS.logs = async (name, live) => {
      clearInterval(window._logTimer);
      if (name != null) window._logName = name;
      if (live != null) window._logLive = live;
      setActions(`<button onclick="VIEWS.refreshLog()">Refresh</button>`);
      const d = await jget('/api/logs');
      const list = `<div class="panel"><h3>Log files</h3><div class="muted">${esc(d.dir)}</div>
        <table><thead><tr><th>Name</th><th>Size</th><th>Modified</th></tr></thead><tbody>
        ${d.files.map(f => `<tr>
          <td><span class="clickable" onclick="VIEWS.logs('${esc(f.name)}')">${esc(f.name)}</span></td>
          <td class="muted">${fmtBytes(f.size)}</td><td class="muted">${esc(f.modified.replace('T',' '))}</td>
        </tr>`).join('') || '<tr><td colspan="3" class="muted">No log files</td></tr>'}</tbody></table></div>`;
      let viewer = '';
      if (window._logName) {
        viewer = `<div class="panel">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.6rem">
            <h3 style="margin:0">${esc(window._logName)}</h3>
            <label class="check" style="margin:0"><input type="checkbox" id="loglive" ${window._logLive?'checked':''}
              onchange="VIEWS.toggleLive(this.checked)"> Live tail</label>
          </div>
          <div class="logview" id="logview">loading…</div></div>`;
      }
      document.getElementById('content').innerHTML = list + viewer;
      if (window._logName) { await VIEWS.refreshLog(); if (window._logLive) VIEWS.startLive(); }
    };
    VIEWS.refreshLog = async () => {
      if (!window._logName) return;
      try {
        const l = await jget('/api/logs/' + encodeURIComponent(window._logName) + '?lines=600');
        const lv = document.getElementById('logview'); if (!lv) return;
        const atBottom = lv.scrollHeight - lv.scrollTop - lv.clientHeight < 50;
        lv.textContent = l.lines.join('\\n') || '(empty)';
        if (window._logLive || atBottom) lv.scrollTop = lv.scrollHeight;
      } catch (err) { /* ignore transient errors during live tail */ }
    };
    VIEWS.startLive = () => { clearInterval(window._logTimer); window._logTimer = setInterval(VIEWS.refreshLog, 2500); };
    VIEWS.toggleLive = (on) => { window._logLive = on; if (on) VIEWS.startLive(); else clearInterval(window._logTimer); };

    // ---------- Updates ----------
    VIEWS.updates = async () => {
      setActions('');
      const u = await jget('/api/updates');
      document.getElementById('content').innerHTML = `
        <div class="panel"><h3>Updates</h3>
          <div class="kv"><div>Installed version</div><div>${esc(u.current)}</div>
            <div>Channel</div><div>${esc(u.channel)}</div></div>
          <p class="muted" style="margin-top:1rem">${esc(u.note)}</p>
          <div class="logview">${u.how_to.map(esc).join('\\n')}</div>
        </div>`;
    };

    // ---------- About ----------
    VIEWS.about = async () => {
      setActions('');
      const a = await jget('/api/about');
      const ct = a.counts || {};
      document.getElementById('content').innerHTML = `
        <div class="panel"><h3>About Librarry</h3>
          <p>Hardcover-first ebook orchestrator — a small Python stack that replaces LazyLibrarian.</p>
          <div class="kv">
            <div>Version</div><div>${esc(a.version)}</div>
            <div>Python</div><div>${esc(a.python)}</div>
            <div>Platform</div><div>${esc(a.platform)}</div>
            <div>Library dir</div><div>${esc(a.library_dir)}</div>
            <div>Download dir</div><div>${esc(a.download_dir)}</div>
            <div>Database</div><div>${esc(a.database)}</div>
            <div>State dir</div><div>${esc(a.state_dir)}</div>
            <div>Log dir</div><div>${esc(a.log_dir)}</div>
            <div>Config</div><div>${esc(a.config_path)}</div>
          </div>
        </div>
        <div class="panel"><h3>Library totals</h3>
          <div class="cards">
            ${['wanted','snatched','imported','failed'].map(k =>
              `<div class="card ${k}"><div class="label">${k}</div><div class="value">${esc(ct[k]||0)}</div></div>`).join('')}
          </div></div>`;
    };

    // ---------- router ----------
    async function act(name) {
      toast('Running ' + name + '…');
      try {
        const j = await jpost('/api/actions/' + name);
        toast(name + ': ' + JSON.stringify(j.result ?? j));
        const cur = location.hash.replace('#','') || 'library';
        if (VIEWS[cur]) VIEWS[cur]();
      } catch (err) { toast(name + ' failed: ' + err.message); }
    }
    window.act = act; window.VIEWS = VIEWS;

    const TITLES = {};
    NAV.forEach(g => g.items.forEach(i => TITLES[i.id] = i.label));

    async function route() {
      clearInterval(window._logTimer);
      const id = location.hash.replace('#','') || 'library';
      let view = VIEWS[id] || VIEWS.library;
      let title = TITLES[id] || 'Library';
      if (id.startsWith('book:')) {
        const bookId = decodeURIComponent(id.slice(5));
        view = () => VIEWS.book(bookId);
        title = 'Book';
      } else if (id.startsWith('author:')) {
        const author = decodeURIComponent(id.slice(7));
        view = () => VIEWS.author(author);
        title = 'Author';
      }
      document.getElementById('view-title').textContent = title;
      document.getElementById('content').style.maxWidth = '';
      renderNav();
      try { await view(); }
      catch (err) { document.getElementById('content').innerHTML = `<div class="panel"><span style="color:var(--failed)">Error: ${esc(err.message)}</span></div>`; }
    }
    window.addEventListener('hashchange', route);
    route();
  </script>
</body>
</html>
"""

