from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest


def make_eml(
    directory: Path,
    name: str,
    *,
    sender: str = "Shop <shop@example.com>",
    to: str = "me@example.org",
    subject: str = "Hello",
    body: str = "Body text",
    date: str = "Thu, 03 Sep 2026 14:42:55 +0300",
    msgid: str | None = None,
    in_reply_to: str | None = None,
    headers: dict[str, str] | None = None,
    pdf: bytes | None = None,
) -> Path:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = msgid or f"<{name}@example.com>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    if pdf is not None:
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="lasku.pdf")
    path = directory / f"{name}.eml"
    path.write_bytes(bytes(msg))
    return path


@pytest.fixture
def maildir(tmp_path: Path) -> Path:
    d = tmp_path / "mail"
    d.mkdir()
    return d
