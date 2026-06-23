import types

from librarry.kindle import send_to_kindle


def test_send_to_kindle_uses_title_only_for_email_subject(tmp_path, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["subject"] = msg["Subject"]
            sent["to"] = msg["To"]

    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
    book = tmp_path / "book.epub"
    book.write_bytes(b"epub")
    cfg = types.SimpleNamespace(
        send_kindle=True,
        kindle_to="reader@kindle.com",
        kindle_smtp_user="smtp-user",
        kindle_smtp_password="smtp-pass",
        kindle_from="sender@example.com",
        kindle_smtp_ssl=True,
        kindle_smtp_server="smtp.example.com",
        kindle_smtp_port=465,
    )

    send_to_kindle(cfg, book, title="The Way of Kings", author="Brandon Sanderson")

    assert sent["subject"] == "The Way of Kings"
    assert sent["to"] == "reader@kindle.com"
