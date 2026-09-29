"""Read RFC 822 messages from disk into compact, model-friendly records.

Everything happens in memory. Attachments are never written to disk: PDF text
is extracted by piping the bytes through ``pdftotext`` on stdin.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import html
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

MAX_MESSAGE_BYTES = 25 * 1024 * 1024
BODY_CHARS = 7000
ATTACHMENT_TEXT_CHARS = 2500
MAX_ATTACHMENT_TEXTS = 2
PDF_MAX_BYTES = 10 * 1024 * 1024

_HEADER_LINE_RE = re.compile(rb"^[A-Za-z][A-Za-z0-9-]{0,60}:")
_MAIL_KEYS = {b"from:", b"subject:", b"date:", b"message-id:", b"received:", b"return-path:", b"delivered-to:"}
_ADDR_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_NOREPLY_RE = re.compile(r"(^|[._+-])(no-?reply|do-?not-?reply|donotreply|no-?responder|noreply|mailer-daemon|notifications?|bounce[s]?)([._+-]|@|$)", re.I)
_NOT_MAIL = {".txt", ".md", ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".html", ".json", ".csv", ".zip", ".db", ".sqlite"}
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)


@dataclass
class Attachment:
    filename: str
    content_type: str
    size: int
    text: str = ""


@dataclass
class Mail:
    path: Path
    sha256: str
    message_id: str
    in_reply_to: list[str]
    references: list[str]
    date: datetime | None
    from_name: str
    from_addr: str
    reply_to: str
    to: list[str]
    cc: list[str]
    delivered_to: list[str]
    subject: str
    body: str
    attachments: list[Attachment] = field(default_factory=list)
    signals: dict[str, str] = field(default_factory=dict)

    @property
    def from_domain(self) -> str:
        return self.from_addr.rsplit("@", 1)[-1].lower() if "@" in self.from_addr else ""

    @property
    def sort_key(self) -> float:
        return self.date.timestamp() if self.date else 0.0

    def date_str(self) -> str:
        return self.date.astimezone().strftime("%Y-%m-%d %H:%M") if self.date else "?"

    def recipients(self) -> list[str]:
        return self.to + self.cc + self.delivered_to


def looks_like_email(path: Path) -> bool:
    """.eml, or a Maildir-style file without extension that starts with mail headers."""
    if path.suffix.lower() == ".eml":
        return True
    # Maildir names have dots and flags (1695000000.M1P2.host:2,S): sniff the headers.
    if path.suffix.lower() in _NOT_MAIL:
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    lines = head.split(b"\n")
    if not lines or not _HEADER_LINE_RE.match(lines[0]):
        return False
    keys = {ln.split(b":", 1)[0].strip().lower() + b":" for ln in lines if _HEADER_LINE_RE.match(ln)}
    return len(keys & _MAIL_KEYS) >= 3


def find_mail_files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.is_file() and not p.is_symlink() and looks_like_email(p):
            out.append(p)
    return out


def _addrs(value: str | None) -> list[str]:
    if not value:
        return []
    return [a.lower() for _, a in email.utils.getaddresses([str(value)]) if "@" in a]


def _msgids(value: str | None) -> list[str]:
    return re.findall(r"<[^<>\s]+>", str(value or ""))


def _header(msg: EmailMessage, name: str) -> str:
    try:
        value = msg.get(name)
    except (ValueError, LookupError, IndexError, TypeError):
        return ""
    return " ".join(str(value).split()) if value is not None else ""


def _all_headers(msg: EmailMessage, name: str) -> list[str]:
    try:
        return [" ".join(str(v).split()) for v in (msg.get_all(name) or [])]
    except (ValueError, LookupError, IndexError, TypeError):
        return []


def html_to_text(src: str) -> str:
    s = re.sub(r"<(script|style|head|title)\b.*?</\1\s*>", " ", src, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<\s*(br|/p|/div|/tr|/li|/h\d|/table)\b[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<\s*(td|th)\b[^>]*>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def clean_text(text: str, limit: int) -> str:
    """Drop tracking URLs and zero-width junk, collapse whitespace, cut to *limit*."""
    text = text.replace("‌", "").replace("​", "").replace("­", "").replace("͏", "")
    text = _URL_RE.sub("[linkki]", text)
    text = re.sub(r"(\[linkki\]\s*){2,}", "[linkki] ", text)
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + " …[katkaistu]"
    return text


def _pdf_text(data: bytes) -> str:
    if not shutil.which("pdftotext") or len(data) > PDF_MAX_BYTES:
        return ""
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", "-l", "3", "-", "-"],
            input=data, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""


def _body_and_attachments(msg: EmailMessage) -> tuple[str, list[Attachment]]:
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = str(part.get_content())
            body = html_to_text(content) if part.get_content_subtype() == "html" else content
            # Some senders put a one-line stub in text/plain and the real mail in HTML.
            if part.get_content_subtype() == "plain" and len(body.strip()) < 40:
                hpart = msg.get_body(preferencelist=("html",))
                if hpart is not None:
                    body = html_to_text(str(hpart.get_content())) or body
    except (KeyError, LookupError, ValueError, AttributeError, TypeError):
        body = ""
    attachments: list[Attachment] = []
    texts = 0
    try:
        parts = list(msg.iter_attachments()) if msg.is_multipart() else []
        # multipart/related (HTML + inline PDF) hides "attachments" one level down
        for p in list(msg.walk()):
            if p.get_content_disposition() in ("attachment", "inline") or p.get_filename():
                if p not in parts and not p.is_multipart():
                    parts.append(p)
            elif p.get_content_type() == "application/pdf" and p not in parts:
                parts.append(p)
    except (KeyError, LookupError, ValueError, AttributeError, TypeError):
        parts = []
    for p in parts:
        ctype = p.get_content_type()
        if ctype.startswith("multipart/") or (ctype.startswith("image/") and not p.get_filename()):
            continue
        try:
            payload = p.get_payload(decode=True) or b""
        except (ValueError, LookupError, TypeError):
            payload = b""
        att = Attachment(filename=str(p.get_filename() or ""), content_type=ctype, size=len(payload))
        if texts < MAX_ATTACHMENT_TEXTS:
            text = ""
            if ctype == "application/pdf" or att.filename.lower().endswith(".pdf"):
                text = _pdf_text(payload)
            elif ctype in ("text/plain", "text/calendar", "text/csv"):
                text = payload.decode(p.get_content_charset() or "utf-8", errors="replace")
            elif ctype == "text/html" and p.get_content_disposition() == "attachment":
                text = html_to_text(payload.decode(p.get_content_charset() or "utf-8", errors="replace"))
            if text.strip():
                att.text = clean_text(text, ATTACHMENT_TEXT_CHARS)
                texts += 1
        attachments.append(att)
    return body, attachments


def _auth_results(values: list[str]) -> str:
    found = {}
    for v in values:
        for key in ("spf", "dkim", "dmarc"):
            m = re.search(rf"\b{key}=(\w+)", v, re.I)
            if m and key not in found:
                found[key] = m.group(1).lower()
    return " ".join(f"{k}={v}" for k, v in found.items())


def compute_signals(msg: EmailMessage, from_addr: str, from_name: str, reply_to: str) -> dict[str, str]:
    """Cheap header facts that tell a bulk or automated mail from a person."""
    s: dict[str, str] = {}
    if _header(msg, "List-Unsubscribe"):
        s["list_unsubscribe"] = "yes"
    for name in ("List-Id", "Precedence", "Auto-Submitted", "X-Auto-Response-Suppress", "Feedback-ID", "X-Campaign", "X-Mailer"):
        v = _header(msg, name)
        if v:
            s[name.lower()] = v[:80]
    if _NOREPLY_RE.search(from_addr.split("@")[0] + "@"):
        s["noreply_sender"] = "yes"
    auth = _auth_results(_all_headers(msg, "Authentication-Results") + _all_headers(msg, "ARC-Authentication-Results"))
    spf = _all_headers(msg, "Received-SPF")
    if spf and "spf=" not in auth:
        auth = (auth + f" spf={spf[0].split()[0].lower()}").strip()
    if auth:
        s["authentication"] = auth
    if reply_to and reply_to.lower() != from_addr.lower():
        s["reply_to_differs"] = reply_to
    return s


def read_mail(path: Path) -> Mail | None:
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_MESSAGE_BYTES)
    except OSError:
        return None
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except (ValueError, LookupError, TypeError):
        return None
    from_raw = _header(msg, "From")
    parsed = email.utils.getaddresses([from_raw]) if from_raw else []
    from_name, from_addr = (parsed[0] if parsed else ("", ""))
    reply_to = (_addrs(_header(msg, "Reply-To")) or [""])[0]
    date = None
    try:
        d = msg.get("Date")
        date = getattr(d, "datetime", None) or (email.utils.parsedate_to_datetime(str(d)) if d else None)
        if date is not None and date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
    except (ValueError, TypeError, IndexError):
        date = None
    if date is None:
        try:
            date = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            date = None
    body, attachments = _body_and_attachments(msg)
    return Mail(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        message_id=(_msgids(_header(msg, "Message-ID")) or [""])[0],
        in_reply_to=_msgids(_header(msg, "In-Reply-To")),
        references=_msgids(_header(msg, "References")),
        date=date,
        from_name=from_name.strip().strip('"'),
        from_addr=from_addr.lower(),
        reply_to=reply_to,
        to=_addrs(_header(msg, "To")),
        cc=_addrs(_header(msg, "Cc")),
        delivered_to=_addrs(_header(msg, "Delivered-To")) + _addrs(_header(msg, "X-Original-To")),
        subject=_header(msg, "Subject"),
        body=clean_text(body, BODY_CHARS),
        attachments=attachments,
        signals=compute_signals(msg, from_addr.lower(), from_name, reply_to),
    )
