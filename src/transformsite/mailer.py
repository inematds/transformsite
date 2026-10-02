"""Envio de e-mail: SMTP se configurado; senão grava .eml em data/outbox (modo seguro/offline)."""

from __future__ import annotations

import os
import smtplib
import time
import uuid
from email.message import EmailMessage
from pathlib import Path

from .config import ProjectConfig


def send_email(cfg: ProjectConfig, to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] | None = None, reply_to: str | None = None) -> dict:
    msg = EmailMessage()
    msg["From"] = cfg.email.smtp_from or f"{cfg.name}@localhost"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@transformsite>"
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    for name, data, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    pwd = os.environ.get(cfg.email.smtp_password_env, "")
    if cfg.email.smtp_host and pwd:
        with smtplib.SMTP(cfg.email.smtp_host, cfg.email.smtp_port, timeout=30) as s:
            s.starttls()
            s.login(cfg.email.smtp_user or cfg.email.smtp_from, pwd)
            s.send_message(msg)
        return {"sent": True, "via": "smtp", "message_id": msg["Message-ID"]}
    out = cfg.root / cfg.email.outbox_dir
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.eml"
    path.write_bytes(bytes(msg))
    return {"sent": False, "via": "outbox", "path": str(path), "message_id": msg["Message-ID"]}
