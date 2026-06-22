from __future__ import annotations

import logging
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from librarry.config import AppConfig

log = logging.getLogger(__name__)

KINDLE_MAX_BYTES = 49 * 1024 * 1024  # Amazon ~50MB limit


def send_to_kindle(cfg: AppConfig, book_path: Path, *, title: str, author: str) -> None:
    if not cfg.send_kindle:
        return
    if not cfg.kindle_to or not cfg.kindle_smtp_user or not cfg.kindle_smtp_password:
        log.warning("Kindle send enabled but SMTP credentials incomplete; skipping")
        return

    size = book_path.stat().st_size
    if size > KINDLE_MAX_BYTES:
        log.warning(
            "Skipping Kindle send for %r (%d MB exceeds limit)",
            title,
            size // (1024 * 1024),
        )
        return

    msg = EmailMessage()
    msg["Subject"] = f"{title} — {author}"
    msg["From"] = cfg.kindle_from or cfg.kindle_smtp_user
    msg["To"] = cfg.kindle_to
    msg.set_content(f"Librarry imported: {title} by {author}")

    mime, _ = mimetypes.guess_type(book_path.name)
    maintype, subtype = (mime or "application/octet-stream").split("/", 1)
    msg.add_attachment(
        book_path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=book_path.name,
    )

    if cfg.kindle_smtp_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg.kindle_smtp_server, cfg.kindle_smtp_port, context=context) as smtp:
            smtp.login(cfg.kindle_smtp_user, cfg.kindle_smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg.kindle_smtp_server, cfg.kindle_smtp_port) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(cfg.kindle_smtp_user, cfg.kindle_smtp_password)
            smtp.send_message(msg)

    log.info("Sent %r to Kindle (%s)", title, cfg.kindle_to)
