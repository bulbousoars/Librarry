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
            for part in msg.iter_attachments():
                sent["attachment"] = part.get_filename()

    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
    # The on-disk name carries the messy "Title - Author" style; Kindle must not
    # show that — it should use the clean title for the attachment filename.
    book = tmp_path / "The Way of Kings - Brandon Sanderson.epub"
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

    status = send_to_kindle(cfg, book, title="The Way of Kings", author="Brandon Sanderson")

    assert status == "sent"
    assert sent["subject"] == "The Way of Kings"
    assert sent["to"] == "reader@kindle.com"
    assert sent["attachment"] == "The Way of Kings.epub"


def test_send_to_kindle_returns_skip_statuses(tmp_path):
    book = tmp_path / "book.epub"
    book.write_bytes(b"epub")
    disabled = types.SimpleNamespace(
        send_kindle=False, kindle_to="reader@kindle.com",
        kindle_smtp_user="u", kindle_smtp_password="p", kindle_from="f",
        kindle_smtp_ssl=True, kindle_smtp_server="s", kindle_smtp_port=465,
    )
    assert send_to_kindle(disabled, book, title="X", author="Y") == "skipped_disabled"

    no_creds = types.SimpleNamespace(
        send_kindle=True, kindle_to="reader@kindle.com",
        kindle_smtp_user="", kindle_smtp_password="", kindle_from="",
        kindle_smtp_ssl=True, kindle_smtp_server="s", kindle_smtp_port=465,
    )
    assert send_to_kindle(no_creds, book, title="X", author="Y") == "skipped_no_creds"
