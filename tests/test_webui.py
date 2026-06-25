import tempfile
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from librarry.db import Database
from librarry.webui.app import create_app


def _jwt_hs256(claims: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    parts = [
        base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("="),
        base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("="),
    ]
    sig = hmac.new(secret.encode(), ".".join(parts).encode(), hashlib.sha256).digest()
    parts.append(base64.urlsafe_b64encode(sig).decode().rstrip("="))
    return ".".join(parts)


def test_oidc_client_validates_standard_hs256_claims():
    from librarry.auth import OIDCClient, OIDCError
    from librarry.config import OIDCConfig

    cfg = OIDCConfig(
        enabled=True,
        issuer="https://issuer.example",
        client_id="librarry",
        client_secret="client-secret",
        redirect_uri="http://testserver/auth/oidc/callback",
    )
    client = OIDCClient(cfg)
    token = _jwt_hs256(
        {
            "iss": "https://issuer.example",
            "sub": "stable-sub",
            "aud": "librarry",
            "exp": int(time.time()) + 600,
            "nonce": "nonce-1",
            "email": "login@example.com",
        },
        "client-secret",
    )

    claims = client.validate_id_token(token, "nonce-1", {"issuer": "https://issuer.example"})
    assert claims["sub"] == "stable-sub"
    assert claims["email"] == "login@example.com"

    try:
        client.validate_id_token(token, "wrong", {"issuer": "https://issuer.example"})
    except OIDCError as exc:
        assert "nonce" in str(exc)
    else:
        raise AssertionError("nonce mismatch should fail")


def test_user_store_self_provisions_oidc_user_enabled_with_own_db_and_kindle_disabled():
    from librarry.users import UserStore

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = UserStore(root / "state" / "users.db", root / "state" / "users")
        store.init()

        user = store.upsert_oidc_user(
            issuer="https://issuer.example",
            subject="stable-sub-123",
            email="login@example.com",
            display_name="Reader One",
            preferred_username="reader1",
        )

        assert user.enabled is True
        assert user.is_admin is False
        assert user.email == "login@example.com"
        assert user.display_name == "Reader One"
        assert user.database_path == root / "state" / "users" / user.id / "librarry.db"
        assert user.database_path.exists()

        kindle = store.get_kindle_settings(user.id)
        assert kindle.send_kindle is False
        assert kindle.kindle_to == ""
        assert kindle.setup_complete is False

        same = store.upsert_oidc_user(
            issuer="https://issuer.example",
            subject="stable-sub-123",
            email="changed@example.com",
            display_name="Reader Renamed",
            preferred_username="reader1",
        )
        assert same.id == user.id
        assert same.database_path == user.database_path


def test_user_store_local_admin_password_hash_and_verify():
    from librarry.users import UserStore

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = UserStore(root / "state" / "users.db", root / "state" / "users")
        store.init()

        admin = store.upsert_local_admin("admin", "correct horse battery staple")

        assert admin.enabled is True
        assert admin.is_admin is True
        assert store.verify_local_admin("admin", "wrong") is None
        verified = store.verify_local_admin("admin", "correct horse battery staple")
        assert verified is not None
        assert verified.id == admin.id


def test_cli_admin_bootstrap_creates_local_admin(monkeypatch, capsys):
    from librarry.cli import main
    from librarry.users import UserStore

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config.yaml"
        config.write_text(
            f"""
database: {(root / 'legacy.db').as_posix()}
state_dir: {(root / 'state').as_posix()}
log_dir: {(root / 'logs').as_posix()}
library_dir: {(root / 'library').as_posix()}
download_dir: {(root / 'downloads').as_posix()}
download_subdir: books
webui:
  enabled: true
  port: 5300
auth:
  enabled: true
  session_secret: test-session-secret
secrets:
  vault: {(root / 'state' / 'secrets.vault').as_posix()}
  key_file: {(root / 'state' / 'secrets.key').as_posix()}
hardcover:
  token: ""
newznab_indexers: []
torznab_indexers: []
""",
            encoding="utf-8",
        )
        answers = iter(["pw", "pw"])
        monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))

        assert main(["--config", str(config), "admin", "bootstrap", "--username", "admin"]) == 0

        out = capsys.readouterr().out
        assert "Bootstrapped local admin: admin" in out
        store = UserStore(root / "state" / "users.db", root / "state" / "users")
        store.init()
        assert store.verify_local_admin("admin", "pw") is not None


def test_webui_status_and_books():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = root / "test.db"
        config = root / "config.yaml"
        config.write_text(
            f"""
database: {db_path.as_posix()}
state_dir: {(root / 'state').as_posix()}
log_dir: {(root / 'logs').as_posix()}
library_dir: {(root / 'library').as_posix()}
download_dir: {(root / 'downloads').as_posix()}
download_subdir: books
webui:
  enabled: true
  port: 5300
secrets:
  vault: {(root / 'state' / 'secrets.vault').as_posix()}
  key_file: {(root / 'state' / 'secrets.key').as_posix()}
hardcover:
  token: ""
newznab_indexers: []
torznab_indexers: []
""",
            encoding="utf-8",
        )
        app = create_app(str(config))
        client = TestClient(app)
        r = client.get("/")
        assert r.status_code == 200
        assert "Librarry" in r.text
        r2 = client.get("/api/status")
        assert r2.status_code == 200
        assert "counts" in r2.json()


def test_webui_auth_requires_session_and_local_admin_login_allows_access():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = root / "legacy.db"
        config = root / "config.yaml"
        config.write_text(
            f"""
database: {db_path.as_posix()}
state_dir: {(root / 'state').as_posix()}
log_dir: {(root / 'logs').as_posix()}
library_dir: {(root / 'library').as_posix()}
download_dir: {(root / 'downloads').as_posix()}
download_subdir: books
webui:
  enabled: true
  port: 5300
auth:
  enabled: true
  session_secret: test-session-secret
  local_admin:
    enabled: true
secrets:
  vault: {(root / 'state' / 'secrets.vault').as_posix()}
  key_file: {(root / 'state' / 'secrets.key').as_posix()}
hardcover:
  token: ""
newznab_indexers: []
torznab_indexers: []
""",
            encoding="utf-8",
        )
        app = create_app(str(config))
        app.state.users.upsert_local_admin("admin", "pw")
        client = TestClient(app)

        assert client.get("/api/status").status_code == 401
        bad = client.post("/auth/local", json={"username": "admin", "password": "bad"})
        assert bad.status_code == 401

        login = client.post("/auth/local", json={"username": "admin", "password": "pw"})
        assert login.status_code == 200
        assert login.json()["user"]["is_admin"] is True
        assert client.get("/api/status").status_code == 200


def test_webui_oidc_callback_self_provisions_enabled_user_without_kindle_email(monkeypatch):
    import librarry.webui.app as webapp

    class FakeOIDCClient:
        def __init__(self, cfg):
            self.cfg = cfg

        def authorization_url(self, state, nonce):
            return f"https://issuer.example/authorize?state={state}&nonce={nonce}"

        def callback_claims(self, code, nonce):
            assert code == "abc"
            assert nonce
            return {
                "iss": "https://issuer.example",
                "sub": "stable-sub",
                "email": "login@example.com",
                "name": "Reader One",
                "preferred_username": "reader1",
            }

    monkeypatch.setattr(webapp, "OIDCClient", FakeOIDCClient)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config.yaml"
        config.write_text(
            f"""
database: {(root / 'legacy.db').as_posix()}
state_dir: {(root / 'state').as_posix()}
log_dir: {(root / 'logs').as_posix()}
library_dir: {(root / 'library').as_posix()}
download_dir: {(root / 'downloads').as_posix()}
download_subdir: books
webui:
  enabled: true
  port: 5300
auth:
  enabled: true
  session_secret: test-session-secret
  oidc:
    enabled: true
    issuer: https://issuer.example
    client_id: librarry
    client_secret: secret
    redirect_uri: http://testserver/auth/oidc/callback
    scopes: [openid, email, profile]
secrets:
  vault: {(root / 'state' / 'secrets.vault').as_posix()}
  key_file: {(root / 'state' / 'secrets.key').as_posix()}
hardcover:
  token: ""
newznab_indexers: []
torznab_indexers: []
""",
            encoding="utf-8",
        )
        app = create_app(str(config))
        client = TestClient(app)

        start = client.get("/auth/oidc/start", follow_redirects=False)
        assert start.status_code == 307
        qs = parse_qs(urlparse(start.headers["location"]).query)
        state = qs["state"][0]

        callback = client.get(
            f"/auth/oidc/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        assert callback.status_code == 307
        assert client.get("/api/status").status_code == 200

        users = app.state.users.list_users()
        assert len(users) == 1
        assert users[0].enabled is True
        assert users[0].email == "login@example.com"
        kindle = app.state.users.get_kindle_settings(users[0].id)
        assert kindle.kindle_to == ""
        assert kindle.send_kindle is False


def test_webui_uses_authenticated_users_own_database():
    import librarry.webui.app as webapp

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config.yaml"
        config.write_text(
            f"""
database: {(root / 'legacy.db').as_posix()}
state_dir: {(root / 'state').as_posix()}
log_dir: {(root / 'logs').as_posix()}
library_dir: {(root / 'library').as_posix()}
download_dir: {(root / 'downloads').as_posix()}
download_subdir: books
webui:
  enabled: true
  port: 5300
auth:
  enabled: true
  session_secret: test-session-secret
secrets:
  vault: {(root / 'state' / 'secrets.vault').as_posix()}
  key_file: {(root / 'state' / 'secrets.key').as_posix()}
hardcover:
  token: ""
newznab_indexers: []
torznab_indexers: []
""",
            encoding="utf-8",
        )
        app = create_app(str(config))
        u1 = app.state.users.upsert_oidc_user(
            issuer="https://issuer.example",
            subject="sub-1",
            email="one@example.com",
            display_name="One",
        )
        u2 = app.state.users.upsert_oidc_user(
            issuer="https://issuer.example",
            subject="sub-2",
            email="two@example.com",
            display_name="Two",
        )
        Database(u1.database_path).add_manual("User One Book", "Author")
        Database(u2.database_path).add_manual("User Two Book", "Author")

        client = TestClient(app)
        client.cookies.set(
            webapp.SESSION_COOKIE,
            webapp._sign_session({"user_id": u1.id, "auth_type": "oidc"}, "test-session-secret"),
        )
        assert [b["title"] for b in client.get("/api/books").json()] == ["User One Book"]

        client.cookies.set(
            webapp.SESSION_COOKIE,
            webapp._sign_session({"user_id": u2.id, "auth_type": "oidc"}, "test-session-secret"),
        )
        assert [b["title"] for b in client.get("/api/books").json()] == ["User Two Book"]


def test_admin_can_select_user_database_but_normal_user_cannot():
    import librarry.webui.app as webapp

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config.yaml"
        config.write_text(
            f"""
database: {(root / 'legacy.db').as_posix()}
state_dir: {(root / 'state').as_posix()}
log_dir: {(root / 'logs').as_posix()}
library_dir: {(root / 'library').as_posix()}
download_dir: {(root / 'downloads').as_posix()}
download_subdir: books
webui:
  enabled: true
  port: 5300
auth:
  enabled: true
  session_secret: test-session-secret
secrets:
  vault: {(root / 'state' / 'secrets.vault').as_posix()}
  key_file: {(root / 'state' / 'secrets.key').as_posix()}
hardcover:
  token: ""
newznab_indexers: []
torznab_indexers: []
""",
            encoding="utf-8",
        )
        app = create_app(str(config))
        admin = app.state.users.upsert_local_admin("admin", "pw")
        reader = app.state.users.upsert_oidc_user(
            issuer="https://issuer.example",
            subject="reader",
            email="reader@example.com",
            display_name="Reader",
        )
        Database(reader.database_path).add_manual("Reader Book", "Author")

        client = TestClient(app)
        client.cookies.set(
            webapp.SESSION_COOKIE,
            webapp._sign_session({"user_id": reader.id, "auth_type": "oidc"}, "test-session-secret"),
        )
        assert client.post("/api/admin/effective-user", json={"user_id": reader.id}).status_code == 403

        client.cookies.set(
            webapp.SESSION_COOKIE,
            webapp._sign_session({"user_id": admin.id, "auth_type": "local"}, "test-session-secret"),
        )
        assert client.get("/api/admin/users").status_code == 200
        selected = client.post("/api/admin/effective-user", json={"user_id": reader.id})
        assert selected.status_code == 200
        assert [b["title"] for b in client.get("/api/books").json()] == ["Reader Book"]


def test_user_and_admin_can_manage_per_user_kindle_settings():
    import librarry.webui.app as webapp

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config.yaml"
        config.write_text(
            f"""
database: {(root / 'legacy.db').as_posix()}
state_dir: {(root / 'state').as_posix()}
log_dir: {(root / 'logs').as_posix()}
library_dir: {(root / 'library').as_posix()}
download_dir: {(root / 'downloads').as_posix()}
download_subdir: books
webui:
  enabled: true
  port: 5300
auth:
  enabled: true
  session_secret: test-session-secret
secrets:
  vault: {(root / 'state' / 'secrets.vault').as_posix()}
  key_file: {(root / 'state' / 'secrets.key').as_posix()}
hardcover:
  token: ""
kindle:
  smtp_server: smtp.gmail.com
  smtp_port: 465
  use_ssl: true
  from: secret:kindle_smtp_from
  user: secret:kindle_smtp_user
  password: secret:kindle_smtp_password
newznab_indexers: []
torznab_indexers: []
""",
            encoding="utf-8",
        )
        app = create_app(str(config))
        admin = app.state.users.upsert_local_admin("admin", "pw")
        reader = app.state.users.upsert_oidc_user(
            issuer="https://issuer.example",
            subject="reader",
            email="login@example.com",
            display_name="Reader",
        )

        client = TestClient(app)
        client.cookies.set(
            webapp.SESSION_COOKIE,
            webapp._sign_session({"user_id": reader.id, "auth_type": "oidc"}, "test-session-secret"),
        )
        settings = client.get("/api/kindle/settings").json()
        assert settings["kindle_to"] == ""
        assert settings["send_kindle"] is False

        updated = client.post(
            "/api/kindle/settings",
            json={"kindle_to": "reader@kindle.com", "send_kindle": True, "setup_complete": True},
        )
        assert updated.status_code == 200
        assert updated.json()["kindle_to"] == "reader@kindle.com"
        assert updated.json()["send_kindle"] is True

        client.cookies.set(
            webapp.SESSION_COOKIE,
            webapp._sign_session({"user_id": admin.id, "auth_type": "local"}, "test-session-secret"),
        )
        admin_update = client.post(
            f"/api/admin/users/{reader.id}/kindle",
            json={"kindle_to": "admin-set@kindle.com", "send_kindle": False},
        )
        assert admin_update.status_code == 200
        assert admin_update.json()["kindle_to"] == "admin-set@kindle.com"
        assert admin_update.json()["send_kindle"] is False


def test_user_can_send_kindle_test_document_with_per_user_recipient(monkeypatch):
    import librarry.webui.app as webapp

    sent = {}

    def fake_send(cfg, path, *, title, author, kindle_to=None, send_kindle=None):
        sent.update(
            {
                "path": path,
                "title": title,
                "author": author,
                "kindle_to": kindle_to,
                "send_kindle": send_kindle,
                "smtp_user": cfg.kindle_smtp_user,
            }
        )

    monkeypatch.setattr(webapp, "send_to_kindle", fake_send)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config.yaml"
        config.write_text(
            f"""
database: {(root / 'legacy.db').as_posix()}
state_dir: {(root / 'state').as_posix()}
log_dir: {(root / 'logs').as_posix()}
library_dir: {(root / 'library').as_posix()}
download_dir: {(root / 'downloads').as_posix()}
download_subdir: books
webui:
  enabled: true
  port: 5300
auth:
  enabled: true
  session_secret: test-session-secret
secrets:
  vault: {(root / 'state' / 'secrets.vault').as_posix()}
  key_file: {(root / 'state' / 'secrets.key').as_posix()}
hardcover:
  token: ""
kindle:
  smtp_server: smtp.example.com
  smtp_port: 465
  use_ssl: true
  from: sender@example.com
  user: smtp-user
  password: smtp-pass
newznab_indexers: []
torznab_indexers: []
""",
            encoding="utf-8",
        )
        app = create_app(str(config))
        reader = app.state.users.upsert_oidc_user(
            issuer="https://issuer.example",
            subject="reader",
            email="login@example.com",
            display_name="Reader",
        )
        app.state.users.set_kindle_settings(
            reader.id,
            kindle_to="reader@kindle.com",
            send_kindle=True,
        )

        client = TestClient(app)
        client.cookies.set(
            webapp.SESSION_COOKIE,
            webapp._sign_session({"user_id": reader.id, "auth_type": "oidc"}, "test-session-secret"),
        )
        result = client.post("/api/kindle/test")
        assert result.status_code == 200
        assert result.json()["last_test_status"] == "sent"
        assert sent["kindle_to"] == "reader@kindle.com"
        assert sent["send_kindle"] is True
        assert sent["smtp_user"] == "smtp-user"


def test_user_can_resend_imported_book_to_kindle(monkeypatch):
    import librarry.webui.app as webapp

    sent = {}

    def fake_send(cfg, path, *, title, author, kindle_to=None, send_kindle=None):
        sent.update(
            {
                "path": Path(path),
                "title": title,
                "author": author,
                "kindle_to": kindle_to,
                "send_kindle": send_kindle,
            }
        )

    monkeypatch.setattr(webapp, "send_to_kindle", fake_send)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config.yaml"
        config.write_text(
            f"""
database: {(root / 'legacy.db').as_posix()}
state_dir: {(root / 'state').as_posix()}
log_dir: {(root / 'logs').as_posix()}
library_dir: {(root / 'library').as_posix()}
download_dir: {(root / 'downloads').as_posix()}
download_subdir: books
webui:
  enabled: true
  port: 5300
auth:
  enabled: true
  session_secret: test-session-secret
secrets:
  vault: {(root / 'state' / 'secrets.vault').as_posix()}
  key_file: {(root / 'state' / 'secrets.key').as_posix()}
hardcover:
  token: ""
kindle:
  smtp_server: smtp.example.com
  smtp_port: 465
  use_ssl: true
  from: sender@example.com
  user: smtp-user
  password: smtp-pass
newznab_indexers: []
torznab_indexers: []
""",
            encoding="utf-8",
        )
        app = create_app(str(config))
        reader = app.state.users.upsert_oidc_user(
            issuer="https://issuer.example",
            subject="reader",
            email="login@example.com",
            display_name="Reader",
        )
        app.state.users.set_kindle_settings(
            reader.id,
            kindle_to="reader@kindle.com",
            send_kindle=True,
        )
        book_file = root / "library" / "Author" / "Book.epub"
        book_file.parent.mkdir(parents=True)
        book_file.write_bytes(b"epub")
        db = Database(reader.database_path)
        bid = db.add_manual("Book", "Author")
        db.mark_imported(bid, str(book_file), "epub")

        client = TestClient(app)
        client.cookies.set(
            webapp.SESSION_COOKIE,
            webapp._sign_session({"user_id": reader.id, "auth_type": "oidc"}, "test-session-secret"),
        )
        result = client.post(f"/api/books/{bid}/resend_kindle")

        assert result.status_code == 200
        assert result.json()["sent"] is True
        assert sent["path"] == book_file
        assert sent["title"] == "Book"
        assert sent["author"] == "Author"
        assert sent["kindle_to"] == "reader@kindle.com"
        assert sent["send_kindle"] is True

        # The resend is recorded in the Send-to-Kindle history.
        history = client.get("/api/kindle/history").json()["sends"]
        assert len(history) == 1
        entry = history[0]
        assert entry["title"] == "Book"
        assert entry["status"] == "sent"
        assert entry["source"] == "resend"
        assert entry["kindle_to"] == "reader@kindle.com"


def test_kindle_send_history_records_and_orders():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        db = create_app(str(config)).state.db
        db.log_kindle_send(title="First", author="A", book_id="b1",
                           kindle_to="x@kindle.com", status="sent", source="import")
        db.log_kindle_send(title="Second", author="B", book_id="b2",
                           kindle_to="x@kindle.com", status="failed",
                           detail="smtp boom", source="resend")
        sends = db.list_kindle_sends()
        assert len(sends) == 2
        assert {s["title"] for s in sends} == {"First", "Second"}
        failed = next(s for s in sends if s["title"] == "Second")
        assert failed["status"] == "failed"
        assert failed["detail"] == "smtp boom"
        assert failed["source"] == "resend"


def test_kindle_history_viewable_without_auth():
    # Single-user / no-auth deployments (auth fronted by a reverse proxy) must
    # still be able to load the Send-to-Kindle history page.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        app.state.db.log_kindle_send(
            title="Imported Book", book_id="b1", kindle_to="x@kindle.com",
            status="sent", source="import",
        )
        client = TestClient(app)
        r = client.get("/api/kindle/history")
        assert r.status_code == 200
        assert [s["title"] for s in r.json()["sends"]] == ["Imported Book"]


def test_from_address_is_editable_and_supersedes_secret():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        # Starts as a vault secret reference.
        assert client.get("/api/config").json()["email"]["from_set"] is True

        r = client.post("/api/config/email", json={"from": "sender@dugganco.com"})
        assert r.status_code == 200

        cfg = client.get("/api/config").json()["email"]
        assert cfg["from"] == "sender@dugganco.com"

        import yaml as _yaml
        raw = _yaml.safe_load(config.read_text(encoding="utf-8"))
        assert raw["kindle"]["from"] == "sender@dugganco.com"  # plain value, secret superseded


def test_resend_uses_global_config_without_auth(monkeypatch):
    import librarry.webui.app as webapp

    sent = {}

    def fake_send(cfg, path, *, title, author, kindle_to=None, send_kindle=None):
        sent.update({"title": title, "kindle_to": kindle_to, "send_kindle": send_kindle})

    monkeypatch.setattr(webapp, "send_to_kindle", fake_send)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        # No per-user settings, no auth — enable delivery via global config.
        assert client.post(
            "/api/config/email", json={"send_kindle": True, "to": "global@kindle.com"}
        ).status_code == 200

        book_file = root / "library" / "Author" / "Book.epub"
        book_file.parent.mkdir(parents=True)
        book_file.write_bytes(b"epub")
        db = app.state.db
        bid = db.add_manual("Global Book", "Author")
        db.mark_imported(bid, str(book_file), "epub")

        r = client.post(f"/api/books/{bid}/resend_kindle")
        assert r.status_code == 200 and r.json()["sent"] is True
        assert sent["kindle_to"] == "global@kindle.com" and sent["send_kindle"] is True

        hist = client.get("/api/kindle/history")
        assert hist.status_code == 200
        assert any(
            s["title"] == "Global Book" and s["source"] == "resend"
            for s in hist.json()["sends"]
        )


def test_book_json_exposes_date_added_and_last_kindle_send():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)
        db = app.state.db

        bid = client.post(
            "/api/books/add", json={"title": "Neuromancer", "author": "William Gibson"}
        ).json()["id"]

        # date_added mirrors the book's wanted_at (when it entered the library).
        book = db.get(bid)
        listed = next(b for b in client.get("/api/books").json() if b["id"] == bid)
        assert listed["date_added"] == book.wanted_at
        assert listed["last_kindle_send"] is None  # nothing sent yet

        # Only successful sends count, and the most recent one wins.
        db.log_kindle_send(title="Neuromancer", book_id=bid, status="failed", source="resend")
        db.log_kindle_send(
            title="Neuromancer", book_id=bid, status="sent",
            kindle_to="x@kindle.com", source="import",
        )
        first_sent = db.last_kindle_send(bid)
        assert first_sent
        db.log_kindle_send(title="Neuromancer", book_id=bid, status="sent", source="resend")
        latest = db.last_kindle_send(bid)
        assert latest >= first_sent

        # Both the list and detail endpoints surface the latest successful send.
        listed = next(b for b in client.get("/api/books").json() if b["id"] == bid)
        assert listed["last_kindle_send"] == latest
        detail = client.get("/api/books/" + bid).json()
        assert detail["last_kindle_send"] == latest
        assert detail["date_added"] == book.wanted_at


def _write_config(root: Path) -> Path:
    db_path = root / "test.db"
    config = root / "config.yaml"
    config.write_text(
        f"""
database: {db_path.as_posix()}
state_dir: {(root / 'state').as_posix()}
log_dir: {(root / 'logs').as_posix()}
library_dir: {(root / 'library').as_posix()}
download_dir: {(root / 'downloads').as_posix()}
download_subdir: books
webui:
  enabled: true
  port: 5300
secrets:
  vault: {(root / 'state' / 'secrets.vault').as_posix()}
  key_file: {(root / 'state' / 'secrets.key').as_posix()}
providers:
  libgen:
    enabled: false
hardcover:
  token: "secret:hardcover_token"
  want_status_id: 1
newznab_indexers:
  - name: NZBGeek
    host: https://api.nzbgeek.info
    api_key: secret:nzbgeek_api_key
    priority: 10
torznab_indexers: []
download_clients:
  usenet:
    type: sabnzbd
    host: 192.168.1.212
    port: 8081
    enabled: true
quality:
  ebook:
    required_extensions: [epub]
    acceptable_extensions: [pdf]
    reject_extensions: [mp3]
    reject_patterns: [audiobook]
    prefer_patterns: [retail]
kindle:
  smtp_server: smtp.gmail.com
  smtp_port: 465
  use_ssl: true
  to: you@kindle.com
  from: secret:kindle_smtp_from
import:
  send_kindle: false
""",
        encoding="utf-8",
    )
    return config


def test_config_read_and_views():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        cfg = client.get("/api/config").json()
        assert cfg["formats"]["required_extensions"] == ["epub"]
        assert cfg["email"]["to"] == "you@kindle.com"
        assert cfg["email"]["from_set"] is True
        assert cfg["indexers"]["newznab"][0]["name"] == "NZBGeek"
        assert cfg["indexers"]["newznab"][0]["has_key"] is True
        assert cfg["clients"][0]["type"] == "sabnzbd"
        assert cfg["importlists"][0]["configured"] is True

        assert client.get("/api/tasks").json()["tasks"]
        assert client.get("/api/about").json()["version"]
        assert client.get("/api/updates").json()["current"]


def test_config_writes_persist_and_keep_secrets():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        r = client.post(
            "/api/config/formats",
            json={
                "required_extensions": ["epub", "AZW3"],
                "acceptable_extensions": ["pdf"],
                "reject_extensions": [],
                "reject_patterns": ["summary"],
                "prefer_patterns": [],
            },
        )
        assert r.status_code == 200
        cfg = client.get("/api/config").json()
        assert cfg["formats"]["required_extensions"] == ["epub", "azw3"]

        r = client.post("/api/config/email", json={"to": "new@kindle.com", "send_kindle": True})
        assert r.status_code == 200
        cfg = client.get("/api/config").json()
        assert cfg["email"]["to"] == "new@kindle.com"
        assert cfg["email"]["send_kindle"] is True
        # secret reference must be preserved untouched on disk
        import yaml as _yaml

        raw = _yaml.safe_load(config.read_text(encoding="utf-8"))
        assert raw["kindle"]["from"] == "secret:kindle_smtp_from"
        assert raw["hardcover"]["token"] == "secret:hardcover_token"


def test_metadata_columns_and_favicon():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        bid = client.post("/api/books/add", json={"title": "The Way of Kings", "author": "Brandon Sanderson"}).json()["id"]
        app.state.db.set_metadata(bid, {
            "series": "The Stormlight Archive", "series_position": 1.0, "genres": "Fantasy",
            "rating": 4.63, "pages": 1007, "isbn_10": "0765326353", "language": "English",
            "release_year": 2010, "ratings_count": 3541, "publisher": "Tor Books",
        })
        books = client.get("/api/books").json()
        b = next(x for x in books if x["id"] == bid)
        assert b["series"] == "The Stormlight Archive"
        assert b["rating"] == 4.63 and b["pages"] == 1007
        assert b["isbn_10"] == "0765326353" and b["language"] == "English"

        # favicon served as SVG
        r = client.get("/favicon.svg")
        assert r.status_code == 200 and "svg" in r.headers["content-type"]
        assert client.get("/favicon.ico").status_code == 200


def test_manual_add_and_delete():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        r = client.post("/api/books/add", json={"title": "Dune", "author": "Frank Herbert"})
        assert r.status_code == 200
        bid = r.json()["id"]
        assert bid.startswith("manual:")

        books = client.get("/api/books?status=wanted").json()
        assert any(b["title"] == "Dune" and b["source"] == "manual" for b in books)
        assert client.get("/api/status").json()["counts"].get("wanted") == 1

        # re-adding same title is idempotent (same id, no duplicate)
        r2 = client.post("/api/books/add", json={"title": "Dune", "author": "Frank Herbert"})
        assert r2.json()["id"] == bid
        assert client.get("/api/status").json()["counts"].get("wanted") == 1

        # missing title rejected
        assert client.post("/api/books/add", json={"author": "x"}).status_code == 400

        d = client.request("DELETE", f"/api/books/{bid}")
        assert d.status_code == 200
        assert client.get("/api/books").json() == []


def test_release_search_and_grab(monkeypatch):
    import librarry.webui.app as webapp

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        bid = client.post("/api/books/add", json={"title": "Dune", "author": "Frank Herbert"}).json()["id"]

        fake = [
            {"title": "Dune.epub", "indexer": "NZBGeek", "protocol": "usenet",
             "size_bytes": 1234, "download_url": "http://x/nzb", "score": 0.9,
             "rejected": False, "reason": None},
        ]
        monkeypatch.setattr(webapp, "search_releases", lambda cfg, title, author: fake)
        rel = client.get(f"/api/releases?book_id={bid}").json()
        assert rel["releases"][0]["title"] == "Dune.epub"

        grabbed = {}
        def fake_snatch(cfg, db, **kw):
            grabbed.update(kw)
            db.mark_snatched(kw["book_id"], protocol=kw["protocol"], source="sabnzbd",
                             indexer=kw["indexer"], release_title=kw["title"], download_id="SAB1")
            return "SAB1"
        monkeypatch.setattr(webapp, "snatch_release", fake_snatch)
        g = client.post("/api/releases/grab", json={
            "book_id": bid, "title": "Dune.epub", "download_url": "http://x/nzb",
            "protocol": "usenet", "indexer": "NZBGeek"})
        assert g.status_code == 200 and g.json()["download_id"] == "SAB1"
        assert grabbed["book_id"] == bid
        assert client.get("/api/status").json()["counts"].get("snatched") == 1


def test_grab_libgen_routes_to_background(monkeypatch):
    import librarry.webui.app as webapp

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)
        bid = client.post("/api/books/add", json={"title": "Dune", "author": "Frank Herbert"}).json()["id"]

        called = {}
        monkeypatch.setattr(
            webapp.libgen_worker, "grab_md5",
            lambda cfg, db, b, host, md5, ext, title: called.update(
                {"book": b, "host": host, "md5": md5, "ext": ext}) or "name",
        )
        r = client.post("/api/releases/grab", json={
            "book_id": bid, "title": "Dune [epub]",
            "download_url": f"libgen:libgen.vg:{'a'*32}:epub",
            "protocol": "direct", "indexer": "LibGen"})
        assert r.status_code == 200 and r.json().get("source") == "libgen"
        # TestClient runs background tasks after the response
        assert called["book"] == bid and called["host"] == "libgen.vg" and called["ext"] == "epub"


def test_lookup_handles_network_error(monkeypatch):
    import librarry.webui.app as webapp

    def boom(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(webapp.requests, "get", boom)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))
        r = client.get("/api/lookup?q=dune")
        assert r.status_code == 200
        assert r.json()["results"] == []
        assert "offline" in r.json()["error"]


def test_add_and_delete_indexer_and_client():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        # add a torznab indexer (no api_key -> no vault write needed)
        r = client.post("/api/indexers", json={
            "kind": "torznab", "name": "MyJackett", "host": "http://jack:9117/api",
            "book_categories": "7020, 8000", "priority": 3, "enabled": True})
        assert r.status_code == 200
        cfg = client.get("/api/config").json()
        names = [i["name"] for i in cfg["indexers"]["torznab"]]
        assert "MyJackett" in names

        d = client.request("DELETE", "/api/indexers", params={"kind": "torznab", "name": "MyJackett"})
        assert d.status_code == 200
        cfg = client.get("/api/config").json()
        assert "MyJackett" not in [i["name"] for i in cfg["indexers"]["torznab"]]

        # add a download client (no secrets -> no vault write)
        r = client.post("/api/clients", json={
            "name": "torrent2", "type": "qbittorrent", "host": "1.2.3.4", "port": 9090,
            "category": "books", "save_path": "/dl/books", "enabled": True})
        assert r.status_code == 200
        cfg = client.get("/api/config").json()
        assert any(c["name"] == "torrent2" and c["type"] == "qbittorrent" for c in cfg["clients"])

        d = client.request("DELETE", "/api/clients", params={"name": "torrent2"})
        assert d.status_code == 200
        assert not any(c["name"] == "torrent2" for c in client.get("/api/config").json()["clients"])


def test_add_hardcover_with_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        r = client.post("/api/books/add_hardcover", json={
            "id": "386446", "title": "The Way of Kings", "author": "Brandon Sanderson",
            "meta": {"series": "The Stormlight Archive", "genres": "Fantasy", "rating": 4.63, "pages": 1007}})
        assert r.status_code == 200 and r.json()["id"] == "386446"
        b = next(x for x in client.get("/api/books").json() if x["id"] == "386446")
        assert b["series"] == "The Stormlight Archive" and b["rating"] == 4.63
        assert b["source"] == "hardcover"
        assert b["hardcover_url"] == "https://hardcover.app/books/the-way-of-kings"


def test_books_api_exposes_local_path_hardcover_url_and_progress():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        bid = app.state.db.add_manual("All the Pretty Horses", "Cormac McCarthy")
        app.state.db.set_metadata(bid, {"hardcover_slug": "all-the-pretty-horses"})
        app.state.db.mark_snatched(
            bid,
            protocol="direct",
            source="libgen",
            indexer="LibGen",
            release_title="All the Pretty Horses [epub]",
            download_id="local",
            file_format="epub",
        )
        with app.state.db.connect() as conn:
            conn.execute("UPDATE books SET download_path=? WHERE id=?", (str(root / "downloads" / "book"), bid))

        b = next(x for x in client.get("/api/books").json() if x["id"] == bid)
        assert b["local_path"].endswith("downloads\\book") or b["local_path"].endswith("downloads/book")
        assert b["hardcover_url"] == "https://hardcover.app/books/all-the-pretty-horses"
        assert b["progress_stage"] == "Downloaded"
        assert "ready for import" in b["progress_detail"]


def test_manual_book_without_hardcover_slug_does_not_guess_hardcover_url():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        bid = client.post(
            "/api/books/add",
            json={"title": "Between Two Fires", "author": "Christopher Buehlman"},
        ).json()["id"]

        b = client.get(f"/api/books/{bid}").json()
        assert b["hardcover_slug"] is None
        assert b["hardcover_url"] == ""


def test_book_detail_extras_notes_tags_and_cover_override():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        bid = app.state.db.add_manual("All the Pretty Horses", "Cormac McCarthy")
        app.state.db.set_metadata(bid, {
            "cover_url": "https://example.test/original.jpg",
            "hardcover_slug": "all-the-pretty-horses",
        })

        r = client.post(f"/api/books/{bid}/extras", json={
            "notes": "Read before The Crossing.",
            "tags": "western, border trilogy",
            "cover_override_url": "https://example.test/override.jpg",
        })
        assert r.status_code == 200

        detail = client.get(f"/api/books/{bid}").json()
        assert detail["id"] == bid
        assert detail["notes"] == "Read before The Crossing."
        assert detail["tags"] == "western, border trilogy"
        assert detail["cover_override_url"] == "https://example.test/override.jpg"
        assert detail["cover_display_url"] == "https://example.test/override.jpg"
        row = next(x for x in client.get("/api/books").json() if x["id"] == bid)
        assert row["cover_display_url"] == "https://example.test/override.jpg"


def test_author_profile_extras_and_books():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        app.state.db.add_manual("All the Pretty Horses", "Cormac McCarthy")
        app.state.db.add_manual("Blood Meridian", "Cormac McCarthy")
        app.state.db.set_author_profile(
            "Cormac McCarthy",
            profile="American novelist.",
            image_url="https://example.test/cormac.jpg",
        )

        r = client.post("/api/authors/Cormac%20McCarthy", json={
            "notes": "Prioritize Border Trilogy.",
            "tags": "western, literary",
        })
        assert r.status_code == 200

        detail = client.get("/api/authors/Cormac%20McCarthy").json()
        assert detail["author"] == "Cormac McCarthy"
        assert detail["profile"] == "American novelist."
        assert detail["notes"] == "Prioritize Border Trilogy."
        assert detail["tags"] == "western, literary"
        assert detail["image_url"] == "https://example.test/cormac.jpg"
        assert {b["title"] for b in detail["books"]} == {"All the Pretty Horses", "Blood Meridian"}


def test_authors_api_lists_library_authors_with_profile_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        cormac = app.state.db.add_manual("All the Pretty Horses", "Cormac McCarthy")
        app.state.db.set_metadata(cormac, {"rating": 4.1})
        blood = app.state.db.add_manual("Blood Meridian", "Cormac McCarthy")
        app.state.db.set_metadata(blood, {"rating": 4.08})
        app.state.db.mark_imported(cormac, str(root / "library" / "all.epub"), "epub")
        app.state.db.add_manual("Dune", "Frank Herbert")

        app.state.db.set_author_profile(
            "Cormac McCarthy",
            profile="American novelist.",
            image_url="https://example.test/cormac.jpg",
            total_books_written=12,
            nationality="American",
            hometown="Providence, Rhode Island",
            source_url="https://example.test/cormac",
        )
        client.post("/api/authors/Cormac%20McCarthy", json={
            "notes": "Read the Border Trilogy.",
            "tags": "western, literary",
        })

        authors = client.get("/api/authors").json()
        cm = next(a for a in authors if a["author"] == "Cormac McCarthy")
        assert cm["book_count"] == 2
        assert cm["owned_count"] == 1
        assert cm["wanted_count"] == 1
        assert cm["in_progress_count"] == 0
        assert cm["average_rating"] == 4.09
        assert cm["total_books_written"] == 12
        assert cm["nationality"] == "American"
        assert cm["hometown"] == "Providence, Rhode Island"
        assert cm["tags"] == "western, literary"


def test_author_profile_edits_only_tags_and_notes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)
        app.state.db.set_author_profile(
            "Mitchell Luthi",
            profile="Source bio",
            image_url="https://example.test/source.jpg",
            total_books_written=7,
            nationality="Swiss",
            hometown="Zurich",
            source_url="https://example.test/source",
        )

        r = client.post("/api/authors/Mitchell%20Luthi", json={
            "notes": "Imported from manual research.",
            "tags": "horror",
            "profile": "User should not overwrite bio",
            "total_books_written": 3,
            "nationality": "Manual",
            "hometown": "Manual",
        })
        assert r.status_code == 200

        detail = client.get("/api/authors/Mitchell%20Luthi").json()
        assert detail["profile"] == "Source bio"
        assert detail["image_url"] == "https://example.test/source.jpg"
        assert detail["total_books_written"] == 7
        assert detail["nationality"] == "Swiss"
        assert detail["hometown"] == "Zurich"
        assert detail["source_url"] == "https://example.test/source"
        assert detail["tags"] == "horror"
        assert detail["notes"] == "Imported from manual research."


def test_poll_author_bibliography_and_add_to_wanted(monkeypatch):
    import librarry.webui.app as webapp

    class FakeSearchResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "docs": [
                    {
                        "key": "/works/OL1W",
                        "title": "In the Name of the Worm",
                        "series": ["Worm Cycle"],
                        "first_publish_year": 2025,
                        "subject": ["Horror", "Fiction", "Dark fantasy"],
                        "author_key": ["OL123A"],
                    },
                    {
                        "key": "/works/OL2W",
                        "title": "A Brief History of Worms",
                        "first_publish_year": 2021,
                        "subject": ["Nonfiction", "Science", "series:Worm Studies"],
                        "author_key": ["OL123A"],
                    },
                ]
            }

    class FakeAuthorResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "bio": {"value": "Mitchell Luthi writes horror fiction."},
                "photos": [98765],
                "birth_date": "1991",
                "location": "Zurich, Switzerland",
                "personal_name": "Mitchell Luthi",
                "remote_ids": {"wikidata": "Q123"},
            }

    def fake_get(url, params=None, timeout=None):
        if url == "https://openlibrary.org/search.json":
            assert params["author"] == "Mitchell Luthi"
            return FakeSearchResponse()
        assert url == "https://openlibrary.org/authors/OL123A.json"
        return FakeAuthorResponse()

    monkeypatch.setattr(webapp.requests, "get", fake_get)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        r = client.post("/api/authors/Mitchell%20Luthi/poll-bibliography")
        assert r.status_code == 200
        rows = r.json()["bibliography"]
        assert rows[0]["title"] == "In the Name of the Worm"
        assert rows[0]["series"] == "Worm Cycle"
        assert rows[0]["release_year"] == 2025
        assert rows[0]["category"] == "Fiction"
        assert rows[0]["genre"] == "Horror, Dark fantasy"
        assert rows[1]["category"] == "Nonfiction"
        assert rows[1]["series"] == "Worm Studies"
        assert rows[1]["genre"] == "Science"

        detail = client.get("/api/authors/Mitchell%20Luthi").json()
        assert len(detail["bibliography"]) == 2
        assert detail["profile"] == "Mitchell Luthi writes horror fiction."
        assert detail["image_url"] == "https://covers.openlibrary.org/a/id/98765-L.jpg"
        assert detail["total_books_written"] == 2
        assert detail["hometown"] == "Zurich, Switzerland"
        assert detail["source_url"] == "https://openlibrary.org/authors/OL123A"

        add = client.post("/api/authors/Mitchell%20Luthi/bibliography/wanted", json={
            "title": "In the Name of the Worm",
        })
        assert add.status_code == 200
        wanted = client.get("/api/books?status=wanted").json()
        assert any(b["title"] == "In the Name of the Worm" and b["author"] == "Mitchell Luthi" for b in wanted)


def test_author_bibliography_marks_existing_library_books_owned(monkeypatch):
    import librarry.webui.app as webapp

    class FakeSearchResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "docs": [
                    {
                        "key": "/works/OL1W",
                        "title": "Red Rabbit",
                        "first_publish_year": 2023,
                        "subject": ["Fiction", "Fantasy"],
                        "author_key": ["OL123A"],
                    },
                    {
                        "key": "/works/OL2W",
                        "title": "Rose of Jericho",
                        "first_publish_year": 2025,
                        "subject": ["Fiction", "Horror"],
                        "author_key": ["OL123A"],
                    },
                ]
            }

    class FakeAuthorResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    def fake_get(url, params=None, timeout=None):
        if url == "https://openlibrary.org/search.json":
            return FakeSearchResponse()
        return FakeAuthorResponse()

    monkeypatch.setattr(webapp.requests, "get", fake_get)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        bid = app.state.db.add_manual("Red Rabbit", "Alex Grecian")
        app.state.db.mark_imported(bid, str(root / "library" / "red-rabbit.epub"), "epub")

        polled = client.post("/api/authors/Alex%20Grecian/poll-bibliography").json()
        red = next(row for row in polled["bibliography"] if row["title"] == "Red Rabbit")
        rose = next(row for row in polled["bibliography"] if row["title"] == "Rose of Jericho")
        assert red["library_status"] == "Owned"
        assert red["library_book_id"] == bid
        assert rose["library_status"] == ""
        assert rose["library_book_id"] == ""

        detail = client.get("/api/authors/Alex%20Grecian").json()
        red_detail = next(row for row in detail["bibliography"] if row["title"] == "Red Rabbit")
        assert red_detail["library_status"] == "Owned"


def test_author_bibliography_normalizes_cached_series_subjects():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        app.state.db.replace_author_bibliography("Brandon Sanderson", [{
            "title": "Wind and Truth",
            "release_year": 2024,
            "category": "",
            "genre": "series:Stormlight Archive",
            "source": "openlibrary",
            "source_id": "/works/OL37577930W",
            "source_url": "https://openlibrary.org/works/OL37577930W",
        }])

        detail = client.get("/api/authors/Brandon%20Sanderson").json()
        row = next(r for r in detail["bibliography"] if r["title"] == "Wind and Truth")
        assert row["series"] == "Stormlight Archive"
        assert row["genre"] == ""


def test_author_bibliography_date_added_persists_across_repoll():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        db = app.state.db

        row = {"title": "Book One", "source": "openlibrary", "category": "Fiction", "genre": ""}
        db.replace_author_bibliography("Date Author", [row])
        first = db.list_author_bibliography("Date Author")[0]
        assert first["date_added"]  # set on first insert

        # Simulate an older first-seen date, then re-poll (delete-then-insert).
        with db.connect() as conn:
            conn.execute(
                "UPDATE author_bibliography SET date_added=? WHERE author=? AND title=?",
                ("2020-01-01T00:00:00+00:00", "Date Author", "Book One"),
            )
        db.replace_author_bibliography("Date Author", [row])
        again = db.list_author_bibliography("Date Author")[0]
        assert again["date_added"] == "2020-01-01T00:00:00+00:00"  # preserved
        assert again["updated_at"] != "2020-01-01T00:00:00+00:00"  # refreshed

        # A genuinely new row added on a later poll gets its own date_added.
        db.replace_author_bibliography(
            "Date Author",
            [row, {"title": "Book Two", "source": "openlibrary", "category": "Fiction", "genre": ""}],
        )
        rows = {r["title"]: r for r in db.list_author_bibliography("Date Author")}
        assert rows["Book One"]["date_added"] == "2020-01-01T00:00:00+00:00"
        assert rows["Book Two"]["date_added"] and rows["Book Two"]["date_added"] != "2020-01-01T00:00:00+00:00"


def test_hardcover_search_handles_network_error(monkeypatch):
    import librarry.webui.app as webapp

    def boom(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(webapp.requests, "post", boom)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))
        # no hardcover token resolvable (vault not init) -> graceful empty
        r = client.get("/api/hardcover/search?q=dune")
        assert r.status_code == 200
        assert r.json()["results"] == []


def test_delete_file_keeps_request():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        lib = Path(app.state.cfg.library_dir)
        (lib / "Author").mkdir(parents=True, exist_ok=True)
        f = lib / "Author" / "Book.epub"
        f.write_text("data")
        bid = app.state.db.add_manual("Book", "Author")
        app.state.db.mark_imported(bid, str(f), "epub")

        r = client.post(f"/api/books/{bid}/delete_file")
        assert r.status_code == 200 and r.json()["file_deleted"] is True
        assert not f.exists()
        assert not (lib / "Author").exists()  # empty author dir tidied up

        b = next(x for x in client.get("/api/books").json() if x["id"] == bid)
        assert b["status"] == "wanted" and b["library_path"] is None
        assert app.state.db.get(bid) is not None  # row preserved for re-search


def test_remove_book_entirely_with_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        lib = Path(app.state.cfg.library_dir)
        lib.mkdir(parents=True, exist_ok=True)
        f = lib / "Solo.epub"
        f.write_text("data")
        bid = app.state.db.add_manual("Solo", "Nobody")
        app.state.db.mark_imported(bid, str(f), "epub")

        r = client.request("DELETE", f"/api/books/{bid}", params={"delete_file": "true"})
        assert r.status_code == 200 and r.json()["file_deleted"] is True
        assert not f.exists()
        assert app.state.db.get(bid) is None


def _owned_book(app, client, name="Book", author="Author", folder="Author"):
    lib = Path(app.state.cfg.library_dir)
    (lib / folder).mkdir(parents=True, exist_ok=True)
    f = lib / folder / f"{name}.epub"
    f.write_text("data")
    bid = app.state.db.add_manual(name, author)
    app.state.db.mark_imported(bid, str(f), "epub")
    return bid, f


def test_remove_endpoint_four_actions():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        # 1. delete_disk -> file gone, row kept, status wanted
        bid, f = _owned_book(app, client, "A", folder="AA")
        r = client.post(f"/api/books/{bid}/remove", json={"action": "delete_disk"})
        assert r.status_code == 200 and not f.exists()
        assert app.state.db.get(bid).status == "wanted"

        # 2. remove_list -> row gone, file kept
        bid, f = _owned_book(app, client, "B", folder="BB")
        r = client.post(f"/api/books/{bid}/remove", json={"action": "remove_list"})
        assert r.status_code == 200 and f.exists()
        assert app.state.db.get(bid) is None

        # 3. delete_and_remove -> both gone
        bid, f = _owned_book(app, client, "C", folder="CC")
        r = client.post(f"/api/books/{bid}/remove", json={"action": "delete_and_remove"})
        assert r.status_code == 200 and not f.exists()
        assert app.state.db.get(bid) is None

        # 4. delete_and_research -> file gone, row kept (wanted), background search scheduled
        bid, f = _owned_book(app, client, "D", folder="DD")
        r = client.post(f"/api/books/{bid}/remove", json={"action": "delete_and_research"})
        assert r.status_code == 200 and not f.exists()
        assert app.state.db.get(bid).status == "wanted"

        # bad action
        bid, _ = _owned_book(app, client, "E", folder="EE")
        assert client.post(f"/api/books/{bid}/remove", json={"action": "nope"}).status_code == 400


def test_delete_file_refuses_outside_library(tmp_path):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        client = TestClient(app)

        outside = root / "elsewhere.epub"
        outside.write_text("important")
        bid = app.state.db.add_manual("Evil", "Path")
        app.state.db.mark_imported(bid, str(outside), "epub")

        r = client.post(f"/api/books/{bid}/delete_file")
        assert r.status_code == 200 and r.json()["file_deleted"] is False
        assert outside.exists()  # never touched a file outside library_dir


def test_file_explorer_is_sandboxed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        (root / "library").mkdir(parents=True, exist_ok=True)
        (root / "library" / "Author").mkdir()
        client = TestClient(create_app(str(config)))

        roots = client.get("/api/files").json()
        assert {r["key"] for r in roots["roots"]} == {"library", "downloads"}

        listing = client.get("/api/files", params={"root": "library"}).json()
        assert any(e["name"] == "Author" and e["is_dir"] for e in listing["entries"])

        # path traversal must be blocked
        bad = client.get("/api/files", params={"root": "library", "path": "../../etc"})
        assert bad.status_code in (403, 404)


def _english_filter_docs():
    return [
        {
            "key": "/works/OLENG",
            "title": "English Original",
            "first_publish_year": 2020,
            "subject": ["Fiction"],
            "language": ["eng"],
            "author_key": ["OL777A"],
        },
        {
            "key": "/works/OLFRE",
            "title": "French Only",
            "first_publish_year": 2018,
            "subject": ["Fiction"],
            "language": ["fre"],
            "author_key": ["OL777A"],
        },
        {
            "key": "/works/OLMIX",
            "title": "Mixed Editions",
            "first_publish_year": 2019,
            "subject": ["Fiction"],
            "language": ["ger", "eng"],
            "author_key": ["OL777A"],
        },
        {
            "key": "/works/OLUNK",
            "title": "Unknown Language",
            "first_publish_year": 2021,
            "subject": ["Fiction"],
            "author_key": ["OL777A"],
        },
    ]


def test_poll_bibliography_filters_to_english_works(monkeypatch):
    import librarry.webui.app as webapp

    class FakeSearchResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"docs": _english_filter_docs()}

    class FakeAuthorResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    def fake_get(url, params=None, timeout=None):
        if url == "https://openlibrary.org/search.json":
            return FakeSearchResponse()
        return FakeAuthorResponse()

    monkeypatch.setattr(webapp.requests, "get", fake_get)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        polled = client.post("/api/authors/Polyglot%20Author/poll-bibliography").json()
        titles = {row["title"] for row in polled["bibliography"]}
        # English edition present -> kept; foreign original w/ English edition -> kept;
        # unknown language -> kept (don't hide real books); French-only -> dropped.
        assert titles == {"English Original", "Mixed Editions", "Unknown Language"}
        assert "French Only" not in titles


def test_poll_bibliography_all_languages_override(monkeypatch):
    import librarry.webui.app as webapp

    class FakeSearchResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"docs": _english_filter_docs()}

    class FakeAuthorResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    def fake_get(url, params=None, timeout=None):
        if url == "https://openlibrary.org/search.json":
            return FakeSearchResponse()
        return FakeAuthorResponse()

    monkeypatch.setattr(webapp.requests, "get", fake_get)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        polled = client.post(
            "/api/authors/Polyglot%20Author/poll-bibliography?languages=all"
        ).json()
        titles = {row["title"] for row in polled["bibliography"]}
        assert "French Only" in titles
        assert len(titles) == 4


def test_poll_bibliography_replace_removes_stale_rows(monkeypatch):
    import librarry.webui.app as webapp

    state = {"call": 0}

    class FakeSearchResponse:
        def __init__(self, docs):
            self._docs = docs

        def raise_for_status(self):
            return None

        def json(self):
            return {"docs": self._docs}

    class FakeAuthorResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    first_docs = [
        {
            "key": "/works/OLA",
            "title": "Book Alpha",
            "first_publish_year": 2020,
            "subject": ["Fiction"],
            "language": ["eng"],
            "author_key": ["OL888A"],
        },
        {
            "key": "/works/OLB",
            "title": "Book Beta",
            "first_publish_year": 2021,
            "subject": ["Fiction"],
            "language": ["eng"],
            "author_key": ["OL888A"],
        },
    ]
    second_docs = first_docs[:1]  # Book Beta disappears on re-poll

    def fake_get(url, params=None, timeout=None):
        if url == "https://openlibrary.org/search.json":
            state["call"] += 1
            return FakeSearchResponse(first_docs if state["call"] == 1 else second_docs)
        return FakeAuthorResponse()

    monkeypatch.setattr(webapp.requests, "get", fake_get)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        client = TestClient(create_app(str(config)))

        first = client.post("/api/authors/Stale%20Author/poll-bibliography").json()
        assert {r["title"] for r in first["bibliography"]} == {"Book Alpha", "Book Beta"}

        second = client.post("/api/authors/Stale%20Author/poll-bibliography").json()
        titles = {r["title"] for r in second["bibliography"]}
        assert titles == {"Book Alpha"}
        assert "Book Beta" not in titles


def _single_english_doc(title="Between Two Fires", author_key="OL999A"):
    return [
        {
            "key": "/works/OLX",
            "title": title,
            "first_publish_year": 2012,
            "subject": ["Fiction", "Horror"],
            "language": ["eng"],
            "author_key": [author_key],
        }
    ]


def test_poll_bibliography_sets_hardcover_author_url(monkeypatch):
    import librarry.webui.app as webapp

    class FakeSearchResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"docs": _single_english_doc()}

    class FakeAuthorResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    def fake_get(url, params=None, timeout=None):
        if url == "https://openlibrary.org/search.json":
            return FakeSearchResponse()
        return FakeAuthorResponse()

    def fake_hc_request(cfg, query, variables=None, *, block=True, timeout=30):
        assert variables == {"name": "Christopher Buehlman"}
        return {
            "data": {
                "authors": [
                    {"name": "Christopher Buehlman", "slug": "christopher-buehlman", "books_count": 12}
                ]
            }
        }

    monkeypatch.setattr(webapp.requests, "get", fake_get)
    monkeypatch.setattr(webapp.hardcover, "request", fake_hc_request)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        app.state.cfg.hardcover_token = "test-token"
        client = TestClient(app)

        client.post("/api/authors/Christopher%20Buehlman/poll-bibliography")
        detail = client.get("/api/authors/Christopher%20Buehlman").json()
        assert detail["hardcover_url"] == "https://hardcover.app/authors/christopher-buehlman"


def test_poll_bibliography_hardcover_lookup_failure_is_non_fatal(monkeypatch):
    import librarry.webui.app as webapp

    class FakeSearchResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"docs": _single_english_doc()}

    class FakeAuthorResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    def fake_get(url, params=None, timeout=None):
        if url == "https://openlibrary.org/search.json":
            return FakeSearchResponse()
        return FakeAuthorResponse()

    def boom(*a, **k):
        raise RuntimeError("hardcover down")

    monkeypatch.setattr(webapp.requests, "get", fake_get)
    monkeypatch.setattr(webapp.hardcover, "request", boom)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = _write_config(root)
        app = create_app(str(config))
        app.state.cfg.hardcover_token = "test-token"
        client = TestClient(app)

        polled = client.post("/api/authors/Christopher%20Buehlman/poll-bibliography")
        assert polled.status_code == 200
        assert polled.json()["bibliography"][0]["title"] == "Between Two Fires"
        detail = client.get("/api/authors/Christopher%20Buehlman").json()
        assert detail["hardcover_url"] == ""
